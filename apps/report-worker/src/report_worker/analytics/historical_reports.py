"""Historical hourly/daily report rebuild for DB replay outputs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from report_worker.analytics.calculations import (
    build_funnel_metrics,
    classify_day_regimes,
    fill_ratio,
)
from report_worker.analytics.historical_counterfactual import (
    HistoricalCounterfactualConfig,
    HistoricalCounterfactualService,
)
from report_worker.analytics.models import PricePathPoint
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CounterfactualResult,
    DailyReport,
    HourlyReport,
    MarketCandle,
    OrderIntent,
    RiskEvent,
    SignalCandidate,
)

JsonPayload = dict[str, Any]
ZERO = Decimal("0")
SOURCE = "historical_report_rebuild"


@dataclass(frozen=True, slots=True)
class HistoricalReportRebuildConfig:
    from_date: date
    to_date: date
    strategy_id: str
    instrument: str | None = None
    timeframe: str | None = None
    session_type: str | None = None
    include_counterfactual: bool = False
    force_rebuild: bool = True
    skip_existing: bool = False
    chunk_days: int = 30
    progress_every: int = 0
    max_days: int | None = None
    dry_run: bool = False
    progress_callback: Callable[[JsonPayload], None] | None = None


@dataclass(frozen=True, slots=True)
class HistoricalReportRebuildResult:
    status: str
    hourly_reports_built: int
    daily_reports_built: int
    counterfactual_results_built: int
    days_processed: int
    skipped_existing: int
    failed_days: tuple[str, ...]
    elapsed_seconds: Decimal
    trading_dates: tuple[str, ...]
    session_types: tuple[str, ...]
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    dry_run: bool

    def as_payload(self) -> JsonPayload:
        return {
            "status": self.status,
            "hourly_reports_built": self.hourly_reports_built,
            "daily_reports_built": self.daily_reports_built,
            "counterfactual_results_built": self.counterfactual_results_built,
            "days_processed": self.days_processed,
            "skipped_existing": self.skipped_existing,
            "failed_days": list(self.failed_days),
            "elapsed_seconds": str(self.elapsed_seconds),
            "trading_dates": list(self.trading_dates),
            "session_types": list(self.session_types),
            "instruments": list(self.instruments),
            "timeframes": list(self.timeframes),
            "dry_run": self.dry_run,
            "source": SOURCE,
        }


class HistoricalReportRebuildService:
    """Build read reports from historical replay tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def rebuild(self, config: HistoricalReportRebuildConfig) -> HistoricalReportRebuildResult:
        started_at = monotonic()
        counterfactual_count = 0
        if config.include_counterfactual:
            counterfactual_count = HistoricalCounterfactualService(self._session).rebuild(
                HistoricalCounterfactualConfig(
                    from_date=config.from_date,
                    to_date=config.to_date,
                    strategy_id=config.strategy_id,
                    instruments=tuple(filter(None, (config.instrument,))),
                    timeframes=tuple(filter(None, (config.timeframe,))),
                    dry_run=config.dry_run,
                    force_rebuild=config.force_rebuild,
                )
            ).results_created
        candidates = self._load_candidates(config)
        dates = tuple(sorted({candidate.trading_date for candidate in candidates}))
        selected_dates = dates[: config.max_days] if config.max_days else dates
        selected_date_set = set(selected_dates)
        candidates = [
            candidate for candidate in candidates if candidate.trading_date in selected_date_set
        ]
        candidates_by_micro_session: dict[str, list[SignalCandidate]] = defaultdict(list)
        candidates_by_date: dict[date, list[SignalCandidate]] = defaultdict(list)
        for candidate in candidates:
            candidates_by_micro_session[candidate.micro_session_id].append(candidate)
            candidates_by_date[candidate.trading_date].append(candidate)
        micro_sessions = tuple(sorted(candidates_by_micro_session))
        if config.dry_run:
            return HistoricalReportRebuildResult(
                status="completed",
                hourly_reports_built=len(micro_sessions),
                daily_reports_built=len(selected_dates),
                counterfactual_results_built=counterfactual_count,
                days_processed=len(selected_dates),
                skipped_existing=0,
                failed_days=(),
                elapsed_seconds=_elapsed(started_at),
                trading_dates=tuple(day.isoformat() for day in dates),
                session_types=tuple(sorted({candidate.session_type for candidate in candidates})),
                instruments=tuple(sorted({candidate.instrument_id for candidate in candidates})),
                timeframes=tuple(sorted({candidate.timeframe for candidate in candidates})),
                dry_run=True,
            )
        hourly_count = 0
        skipped_existing = 0
        failed_days: list[str] = []
        for index, micro_session_id in enumerate(micro_sessions, start=1):
            if config.skip_existing and not config.force_rebuild and self._hourly_exists(
                config,
                micro_session_id,
            ):
                skipped_existing += 1
                continue
            self._build_hourly_report(
                config,
                micro_session_id,
                candidates_by_micro_session[micro_session_id],
            )
            hourly_count += 1
            if config.progress_every > 0 and index % config.progress_every == 0:
                _emit_progress(
                    config,
                    {
                        "stage": "hourly",
                        "items_processed": index,
                        "hourly_reports_built": hourly_count,
                        "skipped_existing": skipped_existing,
                    },
                )
        daily_count = 0
        chunk_size = max(1, config.chunk_days)
        for chunk_start in range(0, len(selected_dates), chunk_size):
            chunk = selected_dates[chunk_start : chunk_start + chunk_size]
            for trading_date in chunk:
                if config.skip_existing and not config.force_rebuild and self._daily_exists(
                    config,
                    trading_date,
                ):
                    skipped_existing += 1
                    continue
                try:
                    self._build_daily_report(
                        config,
                        trading_date,
                        candidates_by_date.get(trading_date, []),
                    )
                    daily_count += 1
                except Exception as exc:  # noqa: BLE001 - keep partial rebuild moving.
                    failed_days.append(f"{trading_date.isoformat()}:{type(exc).__name__}:{exc}")
            if config.progress_every > 0:
                _emit_progress(
                    config,
                    {
                        "stage": "daily",
                        "days_processed": min(chunk_start + len(chunk), len(selected_dates)),
                        "daily_reports_built": daily_count,
                        "failed_days": len(failed_days),
                        "skipped_existing": skipped_existing,
                    },
                )
        status = "completed" if not failed_days else "partial"
        return HistoricalReportRebuildResult(
            status=status,
            hourly_reports_built=hourly_count,
            daily_reports_built=daily_count,
            counterfactual_results_built=counterfactual_count,
            days_processed=len(selected_dates),
            skipped_existing=skipped_existing,
            failed_days=tuple(failed_days),
            elapsed_seconds=_elapsed(started_at),
            trading_dates=tuple(day.isoformat() for day in dates),
            session_types=tuple(sorted({candidate.session_type for candidate in candidates})),
            instruments=tuple(sorted({candidate.instrument_id for candidate in candidates})),
            timeframes=tuple(sorted({candidate.timeframe for candidate in candidates})),
            dry_run=False,
        )

    def _build_hourly_report(
        self,
        config: HistoricalReportRebuildConfig,
        micro_session_id: str,
        candidates: list[SignalCandidate],
    ) -> HourlyReport:
        if not candidates:
            msg = f"No candidates for micro_session_id={micro_session_id}"
            raise LookupError(msg)
        context = candidates[0]
        blockers = self._load_blockers(config, micro_session_id=micro_session_id)
        intents = self._load_intents(config, micro_session_id=micro_session_id)
        orders = self._load_broker_orders(config, micro_session_id=micro_session_id)
        risk_events = self._load_risk_events(config, micro_session_id=micro_session_id)
        if config.force_rebuild:
            self._session.execute(
                delete(HourlyReport).where(
                    HourlyReport.micro_session_id == micro_session_id,
                    HourlyReport.strategy_id == config.strategy_id,
                )
            )
        report = HourlyReport(
            calendar_date=context.calendar_date,
            trading_date=context.trading_date,
            session_type=context.session_type,
            session_phase=context.session_phase,
            micro_session_id=micro_session_id,
            broker_trading_status=context.broker_trading_status,
            run_id=None,
            strategy_id=config.strategy_id,
            instrument_id=None,
            timeframe=None,
            started_at=min(candidate.ts_utc for candidate in candidates),
            ended_at=max(candidate.ts_utc for candidate in candidates),
            realised_pnl=ZERO,
            unrealised_pnl=ZERO,
            commission=ZERO,
            commission_gross=ZERO,
            commission_net=ZERO,
            slippage_bp=ZERO,
            pnl_gross=_counterfactual_sum(config, self._session, micro_session_id, "pnl_gross"),
            pnl_net=_counterfactual_sum(config, self._session, micro_session_id, "pnl_net"),
            signal_count=len(candidates),
            entry_count=sum(1 for candidate in candidates if candidate.signal_type == "entry"),
            exit_count=sum(1 for candidate in candidates if candidate.signal_type == "exit"),
            blocked_count=sum(
                1 for candidate in candidates if candidate.candidate_status == "blocked"
            ),
            reject_count=sum(1 for order in orders if order.broker_status == "rejected"),
            cancel_count=sum(1 for intent in intents if intent.cancel_reason_code),
            reconnect_count=0,
            risk_event_count=len(risk_events),
            fill_ratio=fill_ratio(filled=0, posted=len(orders)),
            report_payload={
                "source": SOURCE,
                "funnel": build_funnel_metrics(
                    created=len(candidates),
                    passed_gates=max(0, len(candidates) - len(blockers)),
                    blockers=len(blockers),
                    order_intent=len(intents),
                    posted=len(orders),
                    filled=0,
                    exited=0,
                    profitable=0,
                ).as_payload(),
                "blocker_ranking": _reason_counts(blockers),
                "instruments": sorted({candidate.instrument_id for candidate in candidates}),
                "timeframes": sorted({candidate.timeframe for candidate in candidates}),
            },
            generated_at=datetime.now(tz=UTC),
        )
        self._session.add(report)
        self._session.flush()
        return report

    def _build_daily_report(
        self,
        config: HistoricalReportRebuildConfig,
        trading_date: date,
        candidates: list[SignalCandidate],
    ) -> DailyReport:
        blockers = self._load_blockers(config, trading_date=trading_date)
        intents = self._load_intents(config, trading_date=trading_date)
        orders = self._load_broker_orders(config, trading_date=trading_date)
        counterfactuals = self._load_counterfactuals(config, trading_date=trading_date)
        candles = self._load_candles(config, trading_date)
        trend = classify_day_regimes(_candles_by_scope(candles))
        if config.force_rebuild:
            self._session.execute(
                delete(DailyReport).where(
                    DailyReport.trading_date == trading_date,
                    DailyReport.strategy_id == config.strategy_id,
                    DailyReport.instrument_id.is_(None),
                    DailyReport.timeframe.is_(None),
                    DailyReport.session_type.is_(None),
                )
            )
        report = DailyReport(
            calendar_date=trading_date,
            trading_date=trading_date,
            session_type=None,
            session_phase=None,
            micro_session_id=None,
            broker_trading_status=None,
            strategy_id=config.strategy_id,
            instrument_id=None,
            timeframe=None,
            market_regime=trend.market_regime,
            realised_pnl=ZERO,
            commission=ZERO,
            commission_gross=ZERO,
            commission_net=ZERO,
            slippage_bp=ZERO,
            pnl_gross=_sum_decimal(result.pnl_gross for result in counterfactuals),
            pnl_net=_sum_decimal(result.pnl_net for result in counterfactuals),
            signal_count=len(candidates),
            blocked_count=sum(
                1 for candidate in candidates if candidate.candidate_status == "blocked"
            ),
            fill_ratio=fill_ratio(filled=0, posted=len(orders)),
            report_payload={
                "source": SOURCE,
                "trend": trend.as_payload(),
                "summary_by_session_type": _summary_by(candidates, "session_type"),
                "summary_by_instrument": _summary_by(candidates, "instrument_id"),
                "summary_by_timeframe": _summary_by(candidates, "timeframe"),
                "funnel": build_funnel_metrics(
                    created=len(candidates),
                    passed_gates=max(0, len(candidates) - len(blockers)),
                    blockers=len(blockers),
                    order_intent=len(intents),
                    posted=len(orders),
                    filled=0,
                    exited=0,
                    profitable=0,
                ).as_payload(),
                "blocker_ranking": _reason_counts(blockers),
                "missed_opportunity_summary": {
                    "would_profit_15m": sum(1 for item in counterfactuals if item.would_profit_15m),
                    "missed_net_pnl": str(
                        _positive_sum(item.pnl_net for item in counterfactuals)
                    ),
                    "avoided_loss": str(
                        abs(_negative_sum(item.pnl_net for item in counterfactuals))
                    ),
                },
            },
            generated_at=datetime.now(tz=UTC),
        )
        self._session.add(report)
        self._session.flush()
        return report

    def _load_candidates(self, config: HistoricalReportRebuildConfig) -> list[SignalCandidate]:
        stmt = select(SignalCandidate).where(
            SignalCandidate.trading_date >= config.from_date,
            SignalCandidate.trading_date <= config.to_date,
            SignalCandidate.strategy_id == config.strategy_id,
        )
        if config.instrument:
            stmt = stmt.where(SignalCandidate.instrument_id == config.instrument)
        if config.timeframe:
            stmt = stmt.where(SignalCandidate.timeframe == config.timeframe)
        if config.session_type:
            stmt = stmt.where(SignalCandidate.session_type == config.session_type)
        return list(self._session.execute(stmt).scalars())

    def _hourly_exists(
        self,
        config: HistoricalReportRebuildConfig,
        micro_session_id: str,
    ) -> bool:
        return bool(
            self._session.execute(
                select(HourlyReport.hourly_report_id).where(
                    HourlyReport.micro_session_id == micro_session_id,
                    HourlyReport.strategy_id == config.strategy_id,
                )
            ).first()
        )

    def _daily_exists(
        self,
        config: HistoricalReportRebuildConfig,
        trading_date: date,
    ) -> bool:
        return bool(
            self._session.execute(
                select(DailyReport.daily_report_id).where(
                    DailyReport.trading_date == trading_date,
                    DailyReport.strategy_id == config.strategy_id,
                    DailyReport.instrument_id.is_(None),
                    DailyReport.timeframe.is_(None),
                    DailyReport.session_type.is_(None),
                )
            ).first()
        )

    def _load_blockers(
        self,
        config: HistoricalReportRebuildConfig,
        *,
        micro_session_id: str | None = None,
        trading_date: date | None = None,
    ) -> list[BlockerEvent]:
        stmt = select(BlockerEvent).where(BlockerEvent.strategy_id == config.strategy_id)
        if micro_session_id:
            stmt = stmt.where(BlockerEvent.micro_session_id == micro_session_id)
        if trading_date:
            stmt = stmt.where(BlockerEvent.trading_date == trading_date)
        return list(self._session.execute(stmt).scalars())

    def _load_intents(
        self,
        config: HistoricalReportRebuildConfig,
        *,
        micro_session_id: str | None = None,
        trading_date: date | None = None,
    ) -> list[OrderIntent]:
        stmt = select(OrderIntent).where(OrderIntent.strategy_id == config.strategy_id)
        if micro_session_id:
            stmt = stmt.where(OrderIntent.micro_session_id == micro_session_id)
        if trading_date:
            stmt = stmt.where(OrderIntent.trading_date == trading_date)
        return list(self._session.execute(stmt).scalars())

    def _load_broker_orders(
        self,
        config: HistoricalReportRebuildConfig,
        *,
        micro_session_id: str | None = None,
        trading_date: date | None = None,
    ) -> list[BrokerOrder]:
        stmt = select(BrokerOrder)
        if micro_session_id:
            stmt = stmt.where(BrokerOrder.micro_session_id == micro_session_id)
        if trading_date:
            stmt = stmt.where(BrokerOrder.trading_date == trading_date)
        return list(self._session.execute(stmt).scalars())

    def _load_risk_events(
        self,
        _config: HistoricalReportRebuildConfig,
        *,
        micro_session_id: str,
    ) -> list[RiskEvent]:
        stmt = select(RiskEvent).where(RiskEvent.micro_session_id == micro_session_id)
        return list(self._session.execute(stmt).scalars())

    def _load_counterfactuals(
        self,
        config: HistoricalReportRebuildConfig,
        *,
        trading_date: date,
    ) -> list[CounterfactualResult]:
        stmt = select(CounterfactualResult).where(
            CounterfactualResult.trading_date == trading_date,
            CounterfactualResult.strategy_id == config.strategy_id,
        )
        return list(self._session.execute(stmt).scalars())

    def _load_candles(
        self,
        config: HistoricalReportRebuildConfig,
        trading_date: date,
    ) -> list[MarketCandle]:
        stmt = select(MarketCandle).where(
            MarketCandle.trading_date == trading_date,
            MarketCandle.is_closed.is_(True),
        )
        if config.instrument:
            stmt = stmt.where(MarketCandle.instrument_id == config.instrument)
        if config.timeframe:
            stmt = stmt.where(MarketCandle.timeframe == config.timeframe)
        return list(self._session.execute(stmt).scalars())


def default_report_window(
    *,
    from_date: date | None,
    to_date: date | None,
    lookback_days: int,
) -> tuple[date, date]:
    end = to_date or datetime.now(tz=UTC).date()
    start = from_date or (end - timedelta(days=lookback_days - 1))
    if start > end:
        msg = "from_date must be <= to_date"
        raise ValueError(msg)
    return start, end


def _counterfactual_sum(
    config: HistoricalReportRebuildConfig,
    session: Session,
    micro_session_id: str,
    field: str,
) -> Decimal:
    stmt = select(func.sum(getattr(CounterfactualResult, field))).where(
        CounterfactualResult.micro_session_id == micro_session_id,
        CounterfactualResult.strategy_id == config.strategy_id,
    )
    return session.execute(stmt).scalar_one_or_none() or ZERO


def _reason_counts(blockers: list[BlockerEvent]) -> list[JsonPayload]:
    counts: dict[str, int] = defaultdict(int)
    for blocker in blockers:
        if not blocker.passed:
            counts[blocker.reason_code] += 1
    return [
        {"blocker_code": code, "count": count}
        for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _summary_by(candidates: list[SignalCandidate], key: str) -> dict[str, JsonPayload]:
    grouped: dict[str, list[SignalCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[str(getattr(candidate, key))].append(candidate)
    return {
        group_key: {
            "candidate_count": len(items),
            "blocked_count": sum(1 for item in items if item.candidate_status == "blocked"),
        }
        for group_key, items in sorted(grouped.items())
    }


def _candles_by_scope(candles: list[MarketCandle]) -> dict[str, list[PricePathPoint]]:
    grouped: dict[str, list[PricePathPoint]] = defaultdict(list)
    for candle in candles:
        grouped[f"{candle.instrument_id}|{candle.timeframe}"].append(
            PricePathPoint(
                ts_utc=candle.close_ts_utc,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
            )
        )
    return dict(grouped)


def _sum_decimal(values: Any) -> Decimal:
    return sum((value for value in values if value is not None), ZERO).quantize(Decimal("0.0001"))


def _positive_sum(values: Any) -> Decimal:
    return sum((value for value in values if value is not None and value > ZERO), ZERO).quantize(
        Decimal("0.0001")
    )


def _negative_sum(values: Any) -> Decimal:
    return sum((value for value in values if value is not None and value < ZERO), ZERO).quantize(
        Decimal("0.0001")
    )


def _elapsed(started_at: float) -> Decimal:
    return Decimal(str(round(monotonic() - started_at, 4)))


def _emit_progress(
    config: HistoricalReportRebuildConfig,
    payload: JsonPayload,
) -> None:
    if config.progress_callback is not None:
        config.progress_callback({**payload, "source": "historical_report_rebuild_progress"})

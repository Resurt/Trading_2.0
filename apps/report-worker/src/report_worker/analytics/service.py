"""SQLAlchemy-backed report analytics service."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from report_worker.analytics.calculations import (
    analyze_counterfactual,
    build_funnel_metrics,
    classify_day_trend,
    counts_by,
    fill_ratio,
    realised_pnl_from_fills,
    state_time_distribution,
)
from report_worker.analytics.models import (
    AnalyticsAssumptions,
    CounterfactualAnalysis,
    CounterfactualSource,
    PricePathPoint,
)
from report_worker.analytics.read_models import (
    counterfactual_read_model,
    daily_report_read_model,
    hourly_report_read_model,
)
from trading_common.db.models import (
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CounterfactualResult,
    DailyReport,
    FillEvent,
    HourlyReport,
    MarketCandle,
    OrderIntent,
    PositionSnapshot,
    RiskEvent,
    SessionRun,
    SignalCandidate,
    StrategyStateEvent,
)

JsonPayload = dict[str, object]
ZERO = Decimal("0")


class ReportAnalyticsService:
    """Builds hourly/daily reports and counterfactual results outside FastAPI."""

    def __init__(
        self,
        session: Session,
        *,
        assumptions: AnalyticsAssumptions | None = None,
    ) -> None:
        self._session = session
        self._assumptions = assumptions or AnalyticsAssumptions()

    def build_hourly_report(
        self,
        *,
        micro_session_id: str,
        strategy_id: str,
    ) -> HourlyReport:
        run = self._session.execute(
            select(SessionRun).where(
                SessionRun.micro_session_id == micro_session_id,
                SessionRun.strategy_id == strategy_id,
            )
        ).scalar_one_or_none()
        if run is None:
            msg = f"SessionRun not found: {micro_session_id} / {strategy_id}"
            raise LookupError(msg)

        candidates = self._list(
            select(SignalCandidate).where(
                SignalCandidate.micro_session_id == micro_session_id,
                SignalCandidate.strategy_id == strategy_id,
            )
        )
        blockers = self._list(
            select(BlockerEvent).where(
                BlockerEvent.micro_session_id == micro_session_id,
                BlockerEvent.strategy_id == strategy_id,
            )
        )
        intents = self._list(
            select(OrderIntent).where(
                OrderIntent.micro_session_id == micro_session_id,
                OrderIntent.strategy_id == strategy_id,
            )
        )
        broker_orders = self._list(
            select(BrokerOrder).where(BrokerOrder.micro_session_id == micro_session_id)
        )
        fills = self._list(select(FillEvent).where(FillEvent.micro_session_id == micro_session_id))
        risk_events = self._list(
            select(RiskEvent).where(RiskEvent.micro_session_id == micro_session_id)
        )
        positions = self._list(
            select(PositionSnapshot).where(PositionSnapshot.micro_session_id == micro_session_id)
        )
        audit_events = self._list(
            select(AuditEvent).where(AuditEvent.micro_session_id == micro_session_id)
        )

        posted_count = _posted_count(broker_orders)
        filled_count = len(fills)
        reject_count = _reject_count(broker_orders, intents)
        cancel_count = _cancel_count(broker_orders, intents)
        replace_count = sum(1 for intent in intents if intent.order_action == "replace")
        blocked_count = _blocked_count(candidates, blockers)
        commission = sum((fill.commission for fill in fills), ZERO)
        realised_pnl = realised_pnl_from_fills(
            (fill.side, fill.lot_qty, fill.price, fill.commission) for fill in fills
        )
        unrealised_pnl = _latest_unrealised_pnl(positions)
        latency_ms = _latency_values_ms(broker_orders)
        stale_incidents = sum(
            1
            for event in (*blockers, *risk_events)
            if getattr(event, "reason_code", None) == "stale_market_data"
        )
        reconnect_count = sum(1 for event in audit_events if "reconnect" in event.action.lower())
        broker_error_count = sum(
            1
            for event in audit_events
            if event.severity == "error" or "error" in event.action.lower()
        )

        report_payload: JsonPayload = {
            "format": "hourly_report_v1",
            "estimated_slippage": str(_estimated_slippage(fills)),
            "replace_count": replace_count,
            "posted_count": posted_count,
            "filled_count": filled_count,
            "broker_error_count": broker_error_count,
            "risk_blockers": counts_by(
                blocker.reason_code for blocker in blockers if not blocker.passed
            ),
            "stale_market_data_incidents": stale_incidents,
            "latency_ms": _distribution(latency_ms),
            "funnel": build_funnel_metrics(
                candidates=len(candidates),
                blockers=blocked_count,
                approved=max(0, len(candidates) - blocked_count),
                posted=posted_count,
                filled=filled_count,
                profitable=sum(1 for fill in fills if fill.side == "sell"),
            ).as_payload(),
        }

        self._session.execute(
            delete(HourlyReport).where(
                HourlyReport.micro_session_id == micro_session_id,
                HourlyReport.strategy_id == strategy_id,
            )
        )
        report = HourlyReport(
            calendar_date=run.calendar_date,
            trading_date=run.trading_date,
            session_type=run.session_type,
            session_phase=run.session_phase,
            micro_session_id=run.micro_session_id,
            broker_trading_status=run.broker_trading_status,
            run_id=run.run_id,
            strategy_id=strategy_id,
            instrument_id=None,
            started_at=run.started_at,
            ended_at=run.ended_at or datetime.now(tz=UTC),
            realised_pnl=realised_pnl,
            unrealised_pnl=unrealised_pnl,
            commission=commission,
            signal_count=len(candidates),
            entry_count=sum(1 for candidate in candidates if candidate.signal_type == "entry"),
            exit_count=sum(1 for candidate in candidates if candidate.signal_type == "exit"),
            blocked_count=blocked_count,
            reject_count=reject_count,
            cancel_count=cancel_count,
            reconnect_count=reconnect_count,
            risk_event_count=len(risk_events),
            fill_ratio=fill_ratio(filled=filled_count, posted=posted_count),
            report_payload=report_payload,
            generated_at=datetime.now(tz=UTC),
        )
        self._session.add(report)
        self._session.flush()
        return report

    def build_daily_report(self, *, trading_date: date, strategy_id: str) -> DailyReport:
        candidates = self._list(
            select(SignalCandidate).where(
                SignalCandidate.trading_date == trading_date,
                SignalCandidate.strategy_id == strategy_id,
            )
        )
        blockers = self._list(
            select(BlockerEvent).where(
                BlockerEvent.trading_date == trading_date,
                BlockerEvent.strategy_id == strategy_id,
            )
        )
        intents = self._list(
            select(OrderIntent).where(
                OrderIntent.trading_date == trading_date,
                OrderIntent.strategy_id == strategy_id,
            )
        )
        broker_orders = self._list(
            select(BrokerOrder).where(BrokerOrder.trading_date == trading_date)
        )
        fills = self._list(select(FillEvent).where(FillEvent.trading_date == trading_date))
        state_events = self._list(
            select(StrategyStateEvent).where(
                StrategyStateEvent.trading_date == trading_date,
                StrategyStateEvent.strategy_id == strategy_id,
            )
        )
        counterfactuals = self._list(
            select(CounterfactualResult).where(
                CounterfactualResult.trading_date == trading_date,
                CounterfactualResult.strategy_id == strategy_id,
            )
        )
        candles = self._list(
            select(MarketCandle).where(
                MarketCandle.trading_date == trading_date,
                MarketCandle.is_closed.is_(True),
            )
        )

        trend = classify_day_trend(_candles_by_instrument(candles))
        posted_count = _posted_count(broker_orders)
        filled_count = len(fills)
        blocked_count = _blocked_count(candidates, blockers)
        commission = sum((fill.commission for fill in fills), ZERO)
        realised_pnl = realised_pnl_from_fills(
            (fill.side, fill.lot_qty, fill.price, fill.commission) for fill in fills
        )
        report_payload: JsonPayload = {
            "format": "daily_report_v1",
            "trend": trend.as_payload(),
            "summary_by_session_type": _summary_by(candidates, key="session_type"),
            "summary_by_instrument": _summary_by(candidates, key="instrument_id"),
            "summary_by_timeframe": _summary_by(candidates, key="timeframe"),
            "blocker_ranking": _ranking(
                blocker.reason_code for blocker in blockers if not blocker.passed
            ),
            "execution_quality": {
                "posted_count": posted_count,
                "filled_count": filled_count,
                "reject_count": _reject_count(broker_orders, intents),
                "cancel_count": _cancel_count(broker_orders, intents),
                "replace_count": sum(1 for intent in intents if intent.order_action == "replace"),
                "fill_ratio": str(fill_ratio(filled=filled_count, posted=posted_count)),
            },
            "missed_opportunity_summary": _missed_opportunity_summary(counterfactuals),
            "strategy_state_time_distribution_seconds": state_time_distribution(
                (event.ts_utc, event.new_state) for event in state_events
            ),
            "funnel": build_funnel_metrics(
                candidates=len(candidates),
                blockers=blocked_count,
                approved=max(0, len(candidates) - blocked_count),
                posted=posted_count,
                filled=filled_count,
                profitable=sum(1 for fill in fills if fill.side == "sell"),
            ).as_payload(),
        }

        self._session.execute(
            delete(DailyReport).where(
                DailyReport.trading_date == trading_date,
                DailyReport.strategy_id == strategy_id,
                DailyReport.session_type.is_(None),
                DailyReport.instrument_id.is_(None),
            )
        )
        report = DailyReport(
            calendar_date=trading_date,
            trading_date=trading_date,
            session_type=None,
            session_phase=None,
            micro_session_id=None,
            broker_trading_status=None,
            strategy_id=strategy_id,
            instrument_id=None,
            market_regime=trend.market_regime,
            realised_pnl=realised_pnl,
            commission=commission,
            signal_count=len(candidates),
            blocked_count=blocked_count,
            fill_ratio=fill_ratio(filled=filled_count, posted=posted_count),
            report_payload=report_payload,
            generated_at=datetime.now(tz=UTC),
        )
        self._session.add(report)
        self._session.flush()
        return report

    def rebuild_reports_for_date(
        self,
        *,
        trading_date: date,
        strategy_id: str,
        include_counterfactual: bool = True,
    ) -> DailyReport:
        runs = self._list(
            select(SessionRun).where(
                SessionRun.trading_date == trading_date,
                SessionRun.strategy_id == strategy_id,
            )
        )
        for run in runs:
            self.build_hourly_report(
                micro_session_id=run.micro_session_id,
                strategy_id=strategy_id,
            )
        if include_counterfactual:
            self.run_counterfactual_analysis_for_date(
                trading_date=trading_date,
                strategy_id=strategy_id,
            )
        return self.build_daily_report(trading_date=trading_date, strategy_id=strategy_id)

    def run_counterfactual_analysis_for_date(
        self,
        *,
        trading_date: date,
        strategy_id: str,
    ) -> list[CounterfactualResult]:
        self._session.execute(
            delete(CounterfactualResult).where(
                CounterfactualResult.trading_date == trading_date,
                CounterfactualResult.strategy_id == strategy_id,
            )
        )
        sources = self._counterfactual_sources(trading_date=trading_date, strategy_id=strategy_id)
        results: list[CounterfactualResult] = []
        for source, context in sources:
            analysis = analyze_counterfactual(
                source=source,
                price_path=self._price_path(source=source),
                assumptions=self._assumptions,
            )
            result = self._counterfactual_result_from_analysis(
                analysis=analysis,
                context=context,
            )
            self._session.add(result)
            results.append(result)
        self._session.flush()
        return results

    def hourly_read_model(self, report: HourlyReport) -> dict[str, object]:
        return hourly_report_read_model(report)

    def daily_read_model(self, report: DailyReport) -> dict[str, object]:
        return daily_report_read_model(report)

    def counterfactual_read_models(
        self,
        results: list[CounterfactualResult],
    ) -> list[dict[str, object]]:
        return [counterfactual_read_model(result) for result in results]

    def _counterfactual_sources(
        self,
        *,
        trading_date: date,
        strategy_id: str,
    ) -> list[tuple[CounterfactualSource, JsonPayload]]:
        candidates = self._list(
            select(SignalCandidate).where(
                SignalCandidate.trading_date == trading_date,
                SignalCandidate.strategy_id == strategy_id,
            )
        )
        final_blockers = {
            blocker.candidate_id: blocker.reason_code
            for blocker in self._list(
                select(BlockerEvent).where(
                    BlockerEvent.trading_date == trading_date,
                    BlockerEvent.strategy_id == strategy_id,
                    BlockerEvent.is_final_blocker.is_(True),
                )
            )
            if blocker.candidate_id is not None
        }
        sources: list[tuple[CounterfactualSource, JsonPayload]] = []
        for candidate in candidates:
            blocker_code = final_blockers.get(candidate.candidate_id)
            if candidate.candidate_status != "blocked" and blocker_code is None:
                continue
            entry_price = candidate.mid_price or candidate.last_price
            if entry_price is None:
                continue
            sources.append(
                (
                    CounterfactualSource(
                        candidate_id=candidate.candidate_id,
                        order_intent_id=None,
                        source_event_type="blocked_candidate",
                        instrument_id=candidate.instrument_id,
                        strategy_id=candidate.strategy_id,
                        side=candidate.side,
                        event_ts=candidate.ts_utc,
                        entry_price=entry_price,
                        lot_qty=_payload_int(candidate.signal_payload, "lot_qty", default=1),
                        blocker_code=blocker_code,
                        cancel_reason_code=None,
                    ),
                    _session_context_from(candidate),
                )
            )

        cancelled_intents = self._list(
            select(OrderIntent).where(
                OrderIntent.trading_date == trading_date,
                OrderIntent.strategy_id == strategy_id,
                OrderIntent.cancel_reason_code.is_not(None),
            )
        )
        for intent in cancelled_intents:
            if intent.intended_price is None:
                continue
            sources.append(
                (
                    CounterfactualSource(
                        candidate_id=intent.candidate_id,
                        order_intent_id=intent.order_intent_id,
                        source_event_type="cancelled_order",
                        instrument_id=intent.instrument_id,
                        strategy_id=intent.strategy_id,
                        side=intent.side,
                        event_ts=intent.terminal_ts or intent.submitted_ts or intent.created_ts,
                        entry_price=intent.intended_price,
                        lot_qty=intent.lot_qty,
                        blocker_code=None,
                        cancel_reason_code=intent.cancel_reason_code,
                    ),
                    _session_context_from(intent),
                )
            )
        return sources

    def _price_path(self, *, source: CounterfactualSource) -> list[PricePathPoint]:
        candles = self._list(
            select(MarketCandle)
            .where(
                MarketCandle.instrument_id == source.instrument_id,
                MarketCandle.trading_date == source.event_ts.date(),
                MarketCandle.close_ts_utc > source.event_ts,
                MarketCandle.close_ts_utc <= source.event_ts + timedelta(minutes=15),
                MarketCandle.is_closed.is_(True),
            )
            .order_by(MarketCandle.close_ts_utc)
        )
        return [
            PricePathPoint(
                ts_utc=candle.close_ts_utc,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
            )
            for candle in candles
        ]

    def _counterfactual_result_from_analysis(
        self,
        *,
        analysis: CounterfactualAnalysis,
        context: JsonPayload,
    ) -> CounterfactualResult:
        window_5 = analysis.windows[5]
        window_10 = analysis.windows[10]
        window_15 = analysis.windows[15]
        return CounterfactualResult(
            calendar_date=context["calendar_date"],
            trading_date=context["trading_date"],
            session_type=str(context["session_type"]),
            session_phase=str(context["session_phase"]),
            micro_session_id=str(context["micro_session_id"]),
            broker_trading_status=str(context["broker_trading_status"]),
            candidate_id=analysis.source.candidate_id,
            order_intent_id=analysis.source.order_intent_id,
            source_event_type=analysis.source.source_event_type,
            instrument_id=analysis.source.instrument_id,
            strategy_id=analysis.source.strategy_id,
            blocker_code=analysis.source.blocker_code,
            cancel_reason_code=analysis.source.cancel_reason_code,
            fee_bps_assumed=analysis.assumptions.fee_bps,
            slippage_bps_assumed=analysis.assumptions.slippage_bps,
            mfe_5m_bps=window_5.mfe_bps,
            mae_5m_bps=window_5.mae_bps,
            mfe_10m_bps=window_10.mfe_bps,
            mae_10m_bps=window_10.mae_bps,
            mfe_15m_bps=window_15.mfe_bps,
            mae_15m_bps=window_15.mae_bps,
            would_profit_5m=window_5.would_profit,
            would_profit_10m=window_10.would_profit,
            would_profit_15m=window_15.would_profit,
            result_payload=analysis.as_payload(),
            generated_at=datetime.now(tz=UTC),
        )

    def _list(self, statement: Any) -> list[Any]:
        return list(self._session.execute(statement).scalars())


def _posted_count(broker_orders: list[Any]) -> int:
    return sum(
        1
        for order in broker_orders
        if order.posted_at is not None
        or order.broker_status in {"posted", "working", "partially_filled", "filled"}
    )


def _reject_count(broker_orders: list[Any], intents: list[Any]) -> int:
    broker_rejects = sum(1 for order in broker_orders if order.broker_status == "rejected")
    intent_rejects = sum(1 for intent in intents if intent.reject_reason_code is not None)
    return max(broker_rejects, intent_rejects)


def _cancel_count(broker_orders: list[Any], intents: list[Any]) -> int:
    broker_cancels = sum(1 for order in broker_orders if order.broker_status == "cancelled")
    intent_cancels = sum(1 for intent in intents if intent.cancel_reason_code is not None)
    return max(broker_cancels, intent_cancels)


def _blocked_count(candidates: list[Any], blockers: list[Any]) -> int:
    candidate_count = sum(1 for candidate in candidates if candidate.candidate_status == "blocked")
    final_blockers = {
        blocker.candidate_id
        for blocker in blockers
        if not blocker.passed and blocker.is_final_blocker and blocker.candidate_id is not None
    }
    return max(candidate_count, len(final_blockers))


def _latest_unrealised_pnl(positions: list[Any]) -> Decimal:
    if not positions:
        return ZERO
    latest = max(positions, key=lambda position: position.snapshot_ts)
    return latest.unrealized_pnl or ZERO


def _estimated_slippage(fills: list[Any]) -> Decimal:
    values: list[Decimal] = []
    for fill in fills:
        raw = fill.fill_payload.get("estimated_slippage")
        if raw is not None:
            values.append(Decimal(str(raw)))
    return sum(values, ZERO).quantize(Decimal("0.0001"))


def _latency_values_ms(broker_orders: list[Any]) -> list[Decimal]:
    values: list[Decimal] = []
    for order in broker_orders:
        raw = _nested_payload_value(order.broker_payload, "latency_ms")
        if raw is not None:
            values.append(Decimal(str(raw)))
    return values


def _distribution(values: list[Decimal]) -> JsonPayload:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None}
    return {
        "count": len(ordered),
        "p50": str(_percentile(ordered, Decimal("0.50"))),
        "p95": str(_percentile(ordered, Decimal("0.95"))),
    }


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal:
    index = int((Decimal(len(values) - 1) * percentile).to_integral_value())
    return values[index].quantize(Decimal("0.0001"))


def _nested_payload_value(payload: dict[str, object], key: str) -> object | None:
    if key in payload:
        return payload[key]
    for value in payload.values():
        if isinstance(value, dict) and key in value:
            return cast(object, value[key])
    return None


def _summary_by(candidates: list[Any], *, key: str) -> dict[str, JsonPayload]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for candidate in candidates:
        grouped[str(getattr(candidate, key))].append(candidate)
    return {
        group_key: {
            "signal_count": len(items),
            "entry_count": sum(1 for item in items if item.signal_type == "entry"),
            "exit_count": sum(1 for item in items if item.signal_type == "exit"),
            "blocked_count": sum(1 for item in items if item.candidate_status == "blocked"),
        }
        for group_key, items in sorted(grouped.items())
    }


def _ranking(values: Iterable[str | None]) -> list[JsonPayload]:
    return [
        {"reason_code": reason_code, "count": count}
        for reason_code, count in counts_by(values).items()
    ]


def _missed_opportunity_summary(results: list[Any]) -> JsonPayload:
    return {
        "would_profit_5m": sum(1 for result in results if result.would_profit_5m),
        "would_profit_10m": sum(1 for result in results if result.would_profit_10m),
        "would_profit_15m": sum(1 for result in results if result.would_profit_15m),
        "total_counterfactuals": len(results),
    }


def _candles_by_instrument(candles: list[Any]) -> dict[str, list[PricePathPoint]]:
    grouped: dict[str, list[PricePathPoint]] = defaultdict(list)
    for candle in candles:
        grouped[candle.instrument_id].append(
            PricePathPoint(
                ts_utc=candle.close_ts_utc,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
            )
        )
    return dict(grouped)


def _payload_int(payload: dict[str, object], key: str, *, default: int) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _session_context_from(row: Any) -> JsonPayload:
    return {
        "calendar_date": row.calendar_date,
        "trading_date": row.trading_date,
        "session_type": row.session_type,
        "session_phase": row.session_phase,
        "micro_session_id": row.micro_session_id,
        "broker_trading_status": row.broker_trading_status,
    }

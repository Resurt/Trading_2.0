"""Calibration aggregates for historical replay outputs."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CalibrationReport,
    CounterfactualResult,
    InstrumentRegistry,
    OrderIntent,
    SignalCandidate,
)

JsonPayload = dict[str, Any]
ZERO = Decimal("0")
SOURCE = "historical_calibration_report"


@dataclass(frozen=True, slots=True)
class CalibrationReportConfig:
    from_date: date
    to_date: date
    strategy_id: str
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    group_by: tuple[str, ...]
    force_rebuild: bool = True


@dataclass(frozen=True, slots=True)
class CalibrationReportResult:
    calibration_report_id: str | None
    report_payload: JsonPayload

    def as_payload(self) -> JsonPayload:
        return {
            "calibration_report_id": self.calibration_report_id,
            **self.report_payload,
        }


class CalibrationReportService:
    """Build persisted calibration report from historical replay domain facts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build(self, config: CalibrationReportConfig) -> CalibrationReportResult:
        instrument_ids = self._resolve_instrument_ids(config.instruments)
        candidates = self._load_candidates(config, instrument_ids)
        blockers = self._load_blockers(config, instrument_ids)
        intents = self._load_order_intents(config, instrument_ids)
        broker_orders = self._load_broker_orders(config, instrument_ids)
        counterfactuals = self._load_counterfactuals(config, instrument_ids)
        payload = self._build_payload(
            config=config,
            candidates=candidates,
            blockers=blockers,
            intents=intents,
            broker_orders=broker_orders,
            counterfactuals=counterfactuals,
        )
        if config.force_rebuild:
            self._session.execute(
                delete(CalibrationReport).where(
                    CalibrationReport.from_date == config.from_date,
                    CalibrationReport.to_date == config.to_date,
                    CalibrationReport.strategy_id == config.strategy_id,
                )
            )
        row = CalibrationReport(
            generated_at=datetime.now(tz=UTC),
            from_date=config.from_date,
            to_date=config.to_date,
            strategy_id=config.strategy_id,
            instruments={"values": list(instrument_ids)},
            timeframes={"values": list(config.timeframes)},
            group_by={"values": list(config.group_by)},
            report_payload=payload,
        )
        self._session.add(row)
        self._session.flush()
        return CalibrationReportResult(str(row.calibration_report_id), payload)

    def read_latest(
        self,
        *,
        strategy_id: str = "baseline",
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> CalibrationReportResult | None:
        stmt = select(CalibrationReport).where(CalibrationReport.strategy_id == strategy_id)
        if from_date is not None:
            stmt = stmt.where(CalibrationReport.from_date >= from_date)
        if to_date is not None:
            stmt = stmt.where(CalibrationReport.to_date <= to_date)
        row = self._session.execute(
            stmt.order_by(CalibrationReport.generated_at.desc())
        ).scalars().first()
        if row is None:
            return None
        return CalibrationReportResult(str(row.calibration_report_id), dict(row.report_payload))

    def _build_payload(
        self,
        *,
        config: CalibrationReportConfig,
        candidates: list[SignalCandidate],
        blockers: list[BlockerEvent],
        intents: list[OrderIntent],
        broker_orders: list[BrokerOrder],
        counterfactuals: list[CounterfactualResult],
    ) -> JsonPayload:
        blocked_ids = {
            blocker.candidate_id
            for blocker in blockers
            if not blocker.passed and blocker.candidate_id is not None
        }
        pseudo_orders = [
            order for order in broker_orders if order.broker_status.startswith("pseudo")
        ]
        net_pnl = _sum_decimal(result.pnl_net for result in counterfactuals)
        gross_pnl = _sum_decimal(result.pnl_gross for result in counterfactuals)
        fees = _sum_decimal(
            result.fee_bps_assumed or ZERO for result in counterfactuals
        )
        slippage = _sum_decimal(
            result.slippage_bps_assumed or ZERO for result in counterfactuals
        )
        return {
            "source": SOURCE,
            "from_date": config.from_date.isoformat(),
            "to_date": config.to_date.isoformat(),
            "strategy_id": config.strategy_id,
            "group_by": list(config.group_by),
            "candidate_count": len(candidates),
            "approved_count": len(intents),
            "blocked_count": len(blocked_ids),
            "pseudo_order_count": len(pseudo_orders),
            "candidate_funnel": {
                "created": len(candidates),
                "passed_gates": max(0, len(candidates) - len(blocked_ids)),
                "blocked": len(blocked_ids),
                "order_intent": len(intents),
                "posted": len(broker_orders),
                "pseudo_orders": len(pseudo_orders),
            },
            "blocker_ranking": _blocker_ranking(blockers, counterfactuals),
            "final_blocker_ranking": _final_blocker_ranking(blockers),
            "missed_opportunity_summary": _missed_opportunity_summary(counterfactuals),
            "avoided_loss_summary": _avoided_loss_summary(counterfactuals),
            "gross_simulated_pnl": str(gross_pnl),
            "net_simulated_pnl": str(net_pnl),
            "total_assumed_fees": str(fees),
            "total_assumed_slippage": str(slippage),
            "long_candidate_count": sum(1 for candidate in candidates if candidate.side == "buy"),
            "short_candidate_count": sum(1 for candidate in candidates if candidate.side == "sell"),
            "long_vs_short_net_pnl_proxy": _side_net_pnl(candidates, counterfactuals),
            "best_session_type": _best_scope(candidates, counterfactuals, "session_type"),
            "worst_session_type": _worst_scope(candidates, counterfactuals, "session_type"),
            "best_timeframe": _best_scope(candidates, counterfactuals, "timeframe"),
            "worst_timeframe": _worst_scope(candidates, counterfactuals, "timeframe"),
            "best_instrument": _best_scope(candidates, counterfactuals, "instrument_id"),
            "worst_instrument": _worst_scope(candidates, counterfactuals, "instrument_id"),
            "cost_sensitivity": _cost_sensitivity(counterfactuals),
            "recommended_threshold_changes": _recommended_threshold_changes(
                blockers=blockers,
                counterfactuals=counterfactuals,
            ),
            "explainability": {
                "note": "Recommendations are payload-only and never mutate strategy_config.",
                "false_positive_proxy": (
                    "share of blocked rows whose 15m historical counterfactual was net profitable"
                ),
            },
        }

    def _resolve_instrument_ids(self, instruments: tuple[str, ...]) -> tuple[str, ...]:
        resolved: list[str] = []
        for item in instruments:
            raw = item.strip()
            if not raw:
                continue
            registry = self._session.execute(
                select(InstrumentRegistry).where(InstrumentRegistry.ticker == raw.upper())
            ).scalars().first()
            if registry is not None:
                resolved.append(registry.instrument_id)
            elif ":" in raw:
                resolved.append(raw)
            else:
                resolved.append(f"MOEX:{raw.upper()}")
        return tuple(dict.fromkeys(resolved))

    def _load_candidates(
        self,
        config: CalibrationReportConfig,
        instrument_ids: tuple[str, ...],
    ) -> list[SignalCandidate]:
        stmt = select(SignalCandidate).where(
            SignalCandidate.trading_date >= config.from_date,
            SignalCandidate.trading_date <= config.to_date,
            SignalCandidate.strategy_id == config.strategy_id,
        )
        if instrument_ids:
            stmt = stmt.where(SignalCandidate.instrument_id.in_(instrument_ids))
        if config.timeframes:
            stmt = stmt.where(SignalCandidate.timeframe.in_(config.timeframes))
        return list(self._session.execute(stmt).scalars())

    def _load_blockers(
        self,
        config: CalibrationReportConfig,
        instrument_ids: tuple[str, ...],
    ) -> list[BlockerEvent]:
        stmt = select(BlockerEvent).where(
            BlockerEvent.trading_date >= config.from_date,
            BlockerEvent.trading_date <= config.to_date,
            BlockerEvent.strategy_id == config.strategy_id,
        )
        if instrument_ids:
            stmt = stmt.where(BlockerEvent.instrument_id.in_(instrument_ids))
        if config.timeframes:
            stmt = stmt.where(BlockerEvent.timeframe.in_(config.timeframes))
        return list(self._session.execute(stmt).scalars())

    def _load_order_intents(
        self,
        config: CalibrationReportConfig,
        instrument_ids: tuple[str, ...],
    ) -> list[OrderIntent]:
        stmt = select(OrderIntent).where(
            OrderIntent.trading_date >= config.from_date,
            OrderIntent.trading_date <= config.to_date,
            OrderIntent.strategy_id == config.strategy_id,
        )
        if instrument_ids:
            stmt = stmt.where(OrderIntent.instrument_id.in_(instrument_ids))
        if config.timeframes:
            stmt = stmt.where(OrderIntent.timeframe.in_(config.timeframes))
        return list(self._session.execute(stmt).scalars())

    def _load_broker_orders(
        self,
        config: CalibrationReportConfig,
        instrument_ids: tuple[str, ...],
    ) -> list[BrokerOrder]:
        stmt = select(BrokerOrder).where(
            BrokerOrder.trading_date >= config.from_date,
            BrokerOrder.trading_date <= config.to_date,
        )
        if instrument_ids:
            stmt = stmt.where(BrokerOrder.instrument_id.in_(instrument_ids))
        if config.timeframes:
            stmt = stmt.where(BrokerOrder.timeframe.in_(config.timeframes))
        return list(self._session.execute(stmt).scalars())

    def _load_counterfactuals(
        self,
        config: CalibrationReportConfig,
        instrument_ids: tuple[str, ...],
    ) -> list[CounterfactualResult]:
        stmt = select(CounterfactualResult).where(
            CounterfactualResult.trading_date >= config.from_date,
            CounterfactualResult.trading_date <= config.to_date,
            CounterfactualResult.strategy_id == config.strategy_id,
        )
        if instrument_ids:
            stmt = stmt.where(CounterfactualResult.instrument_id.in_(instrument_ids))
        if config.timeframes:
            stmt = stmt.where(CounterfactualResult.timeframe.in_(config.timeframes))
        return list(self._session.execute(stmt).scalars())


def default_calibration_window(
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


def _blocker_ranking(
    blockers: list[BlockerEvent],
    counterfactuals: list[CounterfactualResult],
) -> list[JsonPayload]:
    by_code = Counter(blocker.reason_code for blocker in blockers if not blocker.passed)
    facts_by_code: dict[str, list[CounterfactualResult]] = defaultdict(list)
    for result in counterfactuals:
        if result.blocker_code:
            facts_by_code[result.blocker_code].append(result)
    rows: list[JsonPayload] = []
    for code, count in by_code.items():
        facts = facts_by_code.get(code, [])
        false_positive = sum(1 for result in facts if result.would_profit_15m)
        rows.append(
            {
                "blocker_code": code,
                "count": count,
                "missed_gross_pnl": str(_positive_sum(result.pnl_gross for result in facts)),
                "missed_net_pnl": str(_positive_sum(result.pnl_net for result in facts)),
                "avoided_loss": str(abs(_negative_sum(result.pnl_net for result in facts))),
                "false_positive_proxy": (
                    str((Decimal(false_positive) / Decimal(len(facts))).quantize(Decimal("0.0001")))
                    if facts
                    else "0"
                ),
            }
        )
    return sorted(rows, key=lambda row: (-int(row["count"]), str(row["blocker_code"])))


def _final_blocker_ranking(blockers: list[BlockerEvent]) -> list[JsonPayload]:
    counts = Counter(
        blocker.reason_code
        for blocker in blockers
        if not blocker.passed and blocker.is_final_blocker
    )
    return [
        {"blocker_code": blocker_code, "count": count}
        for blocker_code, count in counts.most_common()
    ]


def _missed_opportunity_summary(results: list[CounterfactualResult]) -> JsonPayload:
    return {
        "would_profit_5m": sum(1 for result in results if result.would_profit_5m),
        "would_profit_10m": sum(1 for result in results if result.would_profit_10m),
        "would_profit_15m": sum(1 for result in results if result.would_profit_15m),
        "missed_net_pnl": str(_positive_sum(result.pnl_net for result in results)),
    }


def _avoided_loss_summary(results: list[CounterfactualResult]) -> JsonPayload:
    return {
        "avoided_loss": str(abs(_negative_sum(result.pnl_net for result in results))),
        "avoided_loss_count": sum(1 for result in results if (result.pnl_net or ZERO) < ZERO),
    }


def _side_net_pnl(
    candidates: list[SignalCandidate],
    counterfactuals: list[CounterfactualResult],
) -> JsonPayload:
    side_by_candidate = {candidate.candidate_id: candidate.side for candidate in candidates}
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for result in counterfactuals:
        if result.candidate_id is None:
            continue
        side = side_by_candidate.get(result.candidate_id, "unknown")
        totals[str(side)] += result.pnl_net or ZERO
    return {
        side: str(value.quantize(Decimal("0.0001")))
        for side, value in sorted(totals.items())
    }


def _best_scope(
    candidates: list[SignalCandidate],
    counterfactuals: list[CounterfactualResult],
    key: str,
) -> str | None:
    values = _scope_net_pnl(candidates, counterfactuals, key)
    return max(values, key=lambda scope: values[scope]) if values else None


def _worst_scope(
    candidates: list[SignalCandidate],
    counterfactuals: list[CounterfactualResult],
    key: str,
) -> str | None:
    values = _scope_net_pnl(candidates, counterfactuals, key)
    return min(values, key=lambda scope: values[scope]) if values else None


def _scope_net_pnl(
    candidates: list[SignalCandidate],
    counterfactuals: list[CounterfactualResult],
    key: str,
) -> dict[str, Decimal]:
    scope_by_candidate = {
        candidate.candidate_id: str(getattr(candidate, key)) for candidate in candidates
    }
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for result in counterfactuals:
        if result.candidate_id is None:
            continue
        scope = scope_by_candidate.get(result.candidate_id)
        if scope:
            totals[scope] += result.pnl_net or ZERO
    return dict(totals)


def _cost_sensitivity(results: list[CounterfactualResult]) -> JsonPayload:
    gross_bps = _average_decimal(_window_value(result, "gross_pnl_bps") for result in results)
    return {
        "fee_10_bps": str((gross_bps - Decimal("10")).quantize(Decimal("0.0001"))),
        "fee_15_bps": str((gross_bps - Decimal("15")).quantize(Decimal("0.0001"))),
        "slippage_0_bps": str(gross_bps.quantize(Decimal("0.0001"))),
        "slippage_2_bps": str((gross_bps - Decimal("2")).quantize(Decimal("0.0001"))),
        "slippage_5_bps": str((gross_bps - Decimal("5")).quantize(Decimal("0.0001"))),
        "slippage_10_bps": str((gross_bps - Decimal("10")).quantize(Decimal("0.0001"))),
    }


def _recommended_threshold_changes(
    *,
    blockers: list[BlockerEvent],
    counterfactuals: list[CounterfactualResult],
) -> JsonPayload:
    blocker_counts = Counter(blocker.reason_code for blocker in blockers if not blocker.passed)
    profitable_blocked = sum(1 for result in counterfactuals if result.would_profit_15m)
    return {
        "max_spread_bps": "review_down" if blocker_counts.get("spread_too_wide", 0) else "keep",
        "min_market_quality_score": (
            "review_down" if blocker_counts.get("market_quality_low", 0) else "keep"
        ),
        "min_edge_after_total_costs_bps": (
            "review_up"
            if profitable_blocked < max(1, len(counterfactuals) // 10)
            else "review_down"
        ),
        "max_data_age_ms": "review_up" if blocker_counts.get("stale_market_data", 0) else "keep",
        "allow_short": "review_only_never_auto_change",
    }


def _window_value(result: CounterfactualResult, field: str) -> Decimal:
    windows = result.result_payload.get("windows")
    if not isinstance(windows, dict):
        return ZERO
    window = windows.get("15")
    if not isinstance(window, dict):
        return ZERO
    raw = window.get(field)
    return Decimal(str(raw)) if raw is not None else ZERO


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


def _average_decimal(values: Any) -> Decimal:
    present = [value for value in values if value is not None]
    if not present:
        return ZERO
    return (sum(present, ZERO) / Decimal(len(present))).quantize(Decimal("0.0001"))

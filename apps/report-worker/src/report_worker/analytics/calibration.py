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
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CalibrationReport,
    CorporateActionEvent,
    CounterfactualResult,
    InstrumentRegistry,
    MarketSpecialDay,
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
    calibration_scope: str = "primary_normal_days"
    include_dividend_gap_days: bool = False
    include_corporate_action_days: bool = False
    include_abnormal_gap_days: bool = False
    require_special_day_classification: bool = False
    allow_manual_corporate_actions: bool = False
    max_dividend_sync_age_hours: int = 24


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
        all_candidates = list(candidates)
        all_counterfactuals = list(counterfactuals)
        special_context = self._special_day_context(config, instrument_ids)
        if (
            config.require_special_day_classification
            and special_context["classification_status"] == "missing"
        ):
            msg = "market special day classification is required before final calibration"
            raise RuntimeError(msg)
        candidates = _filter_scope(candidates, config=config, special_context=special_context)
        blockers = _filter_scope(blockers, config=config, special_context=special_context)
        intents = _filter_scope(intents, config=config, special_context=special_context)
        broker_orders = _filter_scope(
            broker_orders,
            config=config,
            special_context=special_context,
        )
        counterfactuals = _filter_scope(
            counterfactuals,
            config=config,
            special_context=special_context,
        )
        payload = self._build_payload(
            config=config,
            candidates=candidates,
            blockers=blockers,
            intents=intents,
            broker_orders=broker_orders,
            counterfactuals=counterfactuals,
            special_context=special_context,
            all_candidates=all_candidates,
            all_counterfactuals=all_counterfactuals,
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
        special_context: JsonPayload,
        all_candidates: list[SignalCandidate],
        all_counterfactuals: list[CounterfactualResult],
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
        classification_missing = special_context["classification_status"] == "missing"
        dividend_sync_status = str(special_context["dividend_sync_status"])
        dividend_sync_ok = (
            bool(special_context["dividend_sync_clean"])
            and dividend_sync_status == "completed"
            and bool(special_context["ready_for_shadow"])
        ) or (
            dividend_sync_status == "manual_only"
            and config.allow_manual_corporate_actions
        )
        calibration_clean = (
            not classification_missing
            and dividend_sync_ok
            and config.calibration_scope == "primary_normal_days"
            and not config.include_dividend_gap_days
            and not config.include_corporate_action_days
        )
        threshold_changes = _recommended_threshold_changes(
            blockers=blockers,
            counterfactuals=counterfactuals,
        )
        warnings = []
        if classification_missing:
            warnings.append("corporate_action_classification_missing")
        if dividend_sync_status == "manual_only":
            warnings.append("manual_corporate_actions_only")
        if dividend_sync_status == "missing":
            warnings.append("dividend_sync_missing")
        if dividend_sync_status == "dry_run":
            warnings.append("dividend_sync_dry_run_not_final")
        if dividend_sync_status == "completed_with_errors":
            warnings.append("dividend_sync_completed_with_errors")
        if dividend_sync_status == "failed":
            warnings.append("dividend_sync_failed")
        if (
            dividend_sync_status == "completed"
            and not bool(special_context["dividend_sync_clean"])
        ):
            warnings.append("dividend_sync_not_clean")
        if (
            dividend_sync_status == "completed"
            and special_context["dividend_sync_age_hours"] is not None
            and not special_context["ready_for_shadow"]
        ):
            warnings.append("dividend_sync_stale")
        if special_context["future_dividend_windows_count"]:
            warnings.append("future_dividend_window_present")
        if config.calibration_scope != "primary_normal_days":
            warnings.append("non_primary_calibration_scope")
        all_candidate_keys = _candidate_keys(all_candidates)
        special_candidate_keys = all_candidate_keys & _context_key_set(
            special_context,
            "special_keys",
        )
        return {
            "source": SOURCE,
            "from_date": config.from_date.isoformat(),
            "to_date": config.to_date.isoformat(),
            "strategy_id": config.strategy_id,
            "group_by": list(config.group_by),
            "calibration_scope": config.calibration_scope,
            "calibration_clean": calibration_clean,
            "calibration_warnings": warnings,
            "dividend_sync_status": dividend_sync_status,
            "dividend_sync_clean": special_context["dividend_sync_clean"],
            "dividend_sync_age_hours": special_context["dividend_sync_age_hours"],
            "dividend_sync_failed_instruments": (
                special_context["dividend_sync_failed_instruments"]
            ),
            "dividend_sync_error_count": special_context["dividend_sync_error_count"],
            "ready_for_shadow": special_context["ready_for_shadow"],
            "ready_for_production": special_context["ready_for_production"],
            "api_import_dividend_events_count": (
                special_context["api_import_dividend_events_count"]
            ),
            "allow_manual_corporate_actions": config.allow_manual_corporate_actions,
            "calibration_data_mode": "historical_candles_only",
            "not_calibrated_from_history": [
                "real_spread",
                "order_book_depth",
                "book_imbalance",
                "market_quality_score",
                "real_slippage",
                "broker_rejects",
                "partial_fills",
                "latency",
            ],
            "requires_shadow_live_calibration": True,
            "normal_days_count": len(all_candidate_keys - special_candidate_keys),
            "special_days_count": len(special_candidate_keys),
            "dividend_gap_days_count": special_context["dividend_gap_days_count"],
            "corporate_action_days_count": special_context["corporate_action_days_count"],
            "abnormal_gap_days_count": special_context["abnormal_gap_days_count"],
            "future_dividend_windows_count": (
                special_context["future_dividend_windows_count"]
            ),
            "excluded_days_count": special_context["excluded_days_count"],
            "included_days_count": special_context["included_days_count"],
            "excluded_from_primary_calibration_count": (
                special_context["excluded_from_primary_calibration_count"]
            ),
            "normal_days_stats": _scope_stats(
                all_candidates,
                all_counterfactuals,
                include_special=False,
                special_context=special_context,
            ),
            "dividend_gap_days_stats": _scope_stats(
                all_candidates,
                all_counterfactuals,
                special_type="dividend_gap_day",
                special_context=special_context,
            ),
            "abnormal_gap_days_stats": _scope_stats(
                all_candidates,
                all_counterfactuals,
                special_type="abnormal_gap_day",
                special_context=special_context,
            ),
            "corporate_action_days_stats": _scope_stats(
                all_candidates,
                all_counterfactuals,
                special_type="corporate_action_day",
                special_context=special_context,
            ),
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
            "recommended_threshold_changes": threshold_changes,
            "recommendations": _split_recommendations(threshold_changes),
            "explainability": {
                "note": "Recommendations are payload-only and never mutate strategy_config.",
                "scope_note": (
                    "Recommendations apply only to the selected calibration_scope and must be "
                    "confirmed by an operator before any strategy_config change."
                ),
                "false_positive_proxy": (
                    "share of blocked rows whose 15m historical counterfactual was net profitable"
                ),
                "recommendation": (
                    "run_market_special_day_classification_before_final_calibration"
                    if classification_missing
                    else "review_payload_only_no_auto_apply"
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

    def _special_day_context(
        self,
        config: CalibrationReportConfig,
        instrument_ids: tuple[str, ...],
    ) -> JsonPayload:
        stmt = select(MarketSpecialDay).where(
            MarketSpecialDay.trading_date >= config.from_date,
            MarketSpecialDay.trading_date <= config.to_date,
        )
        if instrument_ids:
            stmt = stmt.where(MarketSpecialDay.instrument_id.in_(instrument_ids))
        rows = list(self._session.execute(stmt).scalars())
        keys_by_type: dict[str, set[tuple[date, str]]] = defaultdict(set)
        excluded_keys: set[tuple[date, str]] = set()
        included_keys: set[tuple[date, str]] = set()
        for row in rows:
            key = (row.trading_date, row.instrument_id)
            keys_by_type[row.special_day_type].add(key)
            if row.exclude_from_primary_calibration:
                excluded_keys.add(key)
            else:
                included_keys.add(key)
        special_keys = set().union(*keys_by_type.values()) if keys_by_type else set()
        status = (
            "completed"
            if rows
            or _classification_audit_exists(
                self._session,
                from_date=config.from_date,
                to_date=config.to_date,
            )
            else "missing"
        )
        dividend_sync = _dividend_sync_context(
            self._session,
            from_date=config.from_date,
            to_date=config.to_date,
            instrument_ids=instrument_ids,
        )
        return {
            "classification_status": status,
            **dividend_sync,
            "special_keys": special_keys,
            "excluded_keys": excluded_keys,
            "included_keys": included_keys,
            "keys_by_type": keys_by_type,
            "normal_days_count": 0,
            "special_days_count": len(special_keys),
            "dividend_gap_days_count": len(keys_by_type.get("dividend_gap_day", set())),
            "corporate_action_days_count": len(keys_by_type.get("corporate_action_day", set())),
            "abnormal_gap_days_count": len(keys_by_type.get("abnormal_gap_day", set())),
            "future_dividend_windows_count": len(
                keys_by_type.get("future_dividend_risk_window", set())
            ),
            "excluded_days_count": len(excluded_keys),
            "included_days_count": len(included_keys),
            "excluded_from_primary_calibration_count": len(excluded_keys),
        }


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


def _filter_scope[T](
    rows: list[T],
    *,
    config: CalibrationReportConfig,
    special_context: JsonPayload,
) -> list[T]:
    if config.calibration_scope == "all_days":
        return rows
    special_keys = _context_key_set(special_context, "special_keys")
    excluded_keys = _context_key_set(special_context, "excluded_keys")
    dividend_keys = _keys_for_type(special_context, "dividend_gap_day")
    corporate_keys = _keys_for_type(special_context, "corporate_action_day")
    abnormal_keys = _keys_for_type(special_context, "abnormal_gap_day")
    filtered: list[T] = []
    for row in rows:
        key = _row_key(row)
        if key is None:
            filtered.append(row)
            continue
        is_special = key in special_keys
        if config.calibration_scope == "special_days_only":
            if is_special:
                filtered.append(row)
            continue
        if not is_special:
            filtered.append(row)
            continue
        if key in dividend_keys and not config.include_dividend_gap_days:
            continue
        if key in corporate_keys and not config.include_corporate_action_days:
            continue
        if key in abnormal_keys and not config.include_abnormal_gap_days:
            continue
        if key in excluded_keys:
            continue
        filtered.append(row)
    return filtered


def _classification_audit_exists(
    session: Session,
    *,
    from_date: date,
    to_date: date,
) -> bool:
    return (
        session.execute(
            select(AuditEvent.audit_event_id).where(
                AuditEvent.trading_date >= from_date,
                AuditEvent.trading_date <= to_date,
                AuditEvent.action == "market_special_day_classification_completed",
            )
        ).first()
        is not None
    )


def _dividend_sync_context(
    session: Session,
    *,
    from_date: date,
    to_date: date,
    instrument_ids: tuple[str, ...],
) -> JsonPayload:
    from trade_core.corporate_actions import dividend_sync_status_payload

    stmt = select(CorporateActionEvent).where(
        CorporateActionEvent.action_type == "dividend",
        CorporateActionEvent.ex_date >= from_date,
        CorporateActionEvent.ex_date <= to_date,
    )
    if instrument_ids:
        stmt = stmt.where(CorporateActionEvent.instrument_id.in_(instrument_ids))
    rows = list(session.execute(stmt).scalars())
    api_count = sum(1 for row in rows if row.source == "api_import")
    manual_count = sum(1 for row in rows if row.source != "api_import")
    latest = dividend_sync_status_payload(session)
    status = str(latest["status"])
    if status == "missing" and manual_count:
        status = "manual_only"
    return {
        "dividend_sync_status": status,
        "dividend_sync_clean": bool(latest["clean"]),
        "dividend_sync_age_hours": latest["age_hours"],
        "dividend_sync_failed_instruments": int(latest["failed_instruments"]),
        "dividend_sync_error_count": int(latest["error_count"]),
        "ready_for_shadow": bool(latest["ready_for_shadow"]),
        "ready_for_production": bool(latest["ready_for_production"]),
        "api_import_dividend_events_count": api_count,
        "manual_dividend_events_count": manual_count,
    }


def _scope_stats(
    candidates: list[SignalCandidate],
    counterfactuals: list[CounterfactualResult],
    *,
    special_context: JsonPayload,
    include_special: bool | None = None,
    special_type: str | None = None,
) -> JsonPayload:
    if special_type is not None:
        allowed_keys = _keys_for_type(special_context, special_type)
    elif include_special is False:
        allowed_keys = _candidate_keys(candidates) - _context_key_set(
            special_context,
            "special_keys",
        )
    else:
        allowed_keys = _candidate_keys(candidates)
    scoped_candidates = [
        candidate
        for candidate in candidates
        if (candidate.trading_date, candidate.instrument_id) in allowed_keys
    ]
    candidate_ids = {candidate.candidate_id for candidate in scoped_candidates}
    scoped_counterfactuals = [
        result for result in counterfactuals if result.candidate_id in candidate_ids
    ]
    return {
        "candidate_count": len(scoped_candidates),
        "counterfactual_count": len(scoped_counterfactuals),
        "gross_simulated_pnl": str(
            _sum_decimal(result.pnl_gross for result in scoped_counterfactuals)
        ),
        "net_simulated_pnl": str(
            _sum_decimal(result.pnl_net for result in scoped_counterfactuals)
        ),
    }


def _split_recommendations(threshold_changes: JsonPayload) -> JsonPayload:
    return {
        "safe_from_historical_candles": {
            "timeframe_enable_disable": "review_by_candidate_funnel",
            "session_enable_disable": "review_by_session_net_pnl_proxy",
            "instrument_ranking": "review_best_worst_instrument",
            "min_move_bps_preliminary": "review_by_counterfactual_distribution",
            "holding_horizon_preliminary": "review_5m_10m_15m_windows",
            "long_short_preliminary": threshold_changes.get("allow_short"),
        },
        "requires_shadow_confirmation": {
            "max_spread_bps": threshold_changes.get("max_spread_bps"),
            "min_market_quality_score": threshold_changes.get("min_market_quality_score"),
            "slippage_assumptions": "requires_live_execution_observation",
            "execution_thresholds": "requires_order_book_and_latency_data",
            "live_order_policy": "operator_approval_required",
        },
    }


def _row_key(row: object) -> tuple[date, str] | None:
    trading_date = getattr(row, "trading_date", None)
    instrument_id = getattr(row, "instrument_id", None)
    if isinstance(trading_date, date) and isinstance(instrument_id, str):
        return trading_date, instrument_id
    return None


def _candidate_keys(candidates: list[SignalCandidate]) -> set[tuple[date, str]]:
    return {(candidate.trading_date, candidate.instrument_id) for candidate in candidates}


def _context_key_set(context: JsonPayload, key: str) -> set[tuple[date, str]]:
    value = context.get(key)
    result: set[tuple[date, str]] = set()
    if isinstance(value, set):
        for item in value:
            if _is_key(item):
                result.add(item)
    return result


def _keys_for_type(context: JsonPayload, special_type: str) -> set[tuple[date, str]]:
    value = context.get("keys_by_type")
    if not isinstance(value, dict):
        return set()
    raw = value.get(special_type, set())
    result: set[tuple[date, str]] = set()
    if not isinstance(raw, set):
        return result
    for item in raw:
        if _is_key(item):
            result.add(item)
    return result


def _is_key(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], date)
        and isinstance(value[1], str)
    )


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

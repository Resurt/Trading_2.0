"""Intraday Analytics and Calibration Center services.

The services in this module are diagnostic/reporting only. They read persisted
market/decision facts and write analytics snapshots or draft config proposals.
They never call broker order APIs and never mutate active ``strategy_config``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CalibrationDiagnosticRun,
    CounterfactualResult,
    IntradaySessionAnalytics,
    MarketCandle,
    MarketMicrostructureSnapshot,
    MarketRegimeSnapshot,
    OrderIntent,
    RollingPerformanceCube,
    SessionRun,
    SignalCandidate,
    StrategyConfigCandidate,
)

JsonPayload = dict[str, Any]
ZERO = Decimal("0")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
WINDOW_DAYS = {
    "7d": 7,
    "20d": 20,
    "60d": 60,
    "90d": 90,
    "180d": 180,
    "365d": 365,
}
SESSION_TYPES = ("weekday_morning", "weekday_main", "weekday_evening", "weekend")
VALID_MODES = {"data_shadow", "historical", "strategy_shadow", "sandbox", "live", "all"}


@dataclass(frozen=True, slots=True)
class _SessionFacts:
    trading_date: date
    session_type: str
    candidates: list[SignalCandidate]
    blockers: list[BlockerEvent]
    intents: list[OrderIntent]
    broker_orders: list[BrokerOrder]
    counterfactuals: list[CounterfactualResult]
    microstructure: list[MarketMicrostructureSnapshot]
    candles: list[MarketCandle]
    session_runs: list[SessionRun]


class IntradayAnalyticsService:
    """Build session/hour/instrument analytics for a trading day."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_for_trading_date(
        self,
        trading_date: date,
        *,
        mode: str = "all",
    ) -> JsonPayload:
        session_payloads = [
            self.build_for_session(trading_date, session_type, mode=mode)
            for session_type in SESSION_TYPES
            if self._session_has_data(trading_date, session_type, mode=mode)
        ]
        if not session_payloads:
            session_payloads = [
                self.build_for_session(trading_date, "weekday_main", mode=mode)
            ]
        payload = _combine_intraday_payloads(
            trading_date=trading_date,
            session_payloads=session_payloads,
        )
        return payload

    def build_for_session(
        self,
        trading_date: date,
        session_type: str,
        *,
        mode: str = "all",
    ) -> JsonPayload:
        _validate_mode(mode)
        facts = self._load_session_facts(trading_date, session_type, mode=mode)
        generated_at = datetime.now(tz=UTC)
        summary = _intraday_summary_from_facts(facts, generated_at=generated_at, mode=mode)
        rows = [summary]
        rows.extend(_scope_summaries(facts, generated_at=generated_at, mode=mode))
        rows.extend(_hour_summaries(facts, generated_at=generated_at, mode=mode))
        for row_payload in rows:
            self._session.add(_intraday_row_from_payload(row_payload))
        self._session.flush()
        return _session_payload_from_rows(rows)

    def build_for_micro_session(self, micro_session_id: str) -> JsonPayload:
        run = self._session.execute(
            select(SessionRun).where(SessionRun.micro_session_id == micro_session_id)
        ).scalars().first()
        if run is not None:
            trading_date = run.trading_date
            session_type = run.session_type
        else:
            candidate = self._session.execute(
                select(SignalCandidate).where(
                    SignalCandidate.micro_session_id == micro_session_id
                )
            ).scalars().first()
            micro = self._session.execute(
                select(MarketMicrostructureSnapshot).where(
                    MarketMicrostructureSnapshot.micro_session_id == micro_session_id
                )
            ).scalars().first()
            row = candidate or micro
            if row is None:
                return {
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "micro_session_id": micro_session_id,
                    "warnings": ["micro_session_not_found"],
                    "rows": [],
                }
            trading_date = row.trading_date
            session_type = row.session_type
        facts = self._load_session_facts(
            trading_date,
            session_type,
            micro_session_id=micro_session_id,
            mode="all",
        )
        generated_at = datetime.now(tz=UTC)
        summary = _intraday_summary_from_facts(
            facts,
            generated_at=generated_at,
            mode="all",
            micro_session_id=micro_session_id,
        )
        self._session.add(_intraday_row_from_payload(summary))
        self._session.flush()
        return _session_payload_from_rows([summary])

    def build_current_day_snapshot(self) -> JsonPayload:
        today = datetime.now(tz=MOSCOW_TZ).date()
        return self.build_for_trading_date(today)

    def _session_has_data(self, trading_date: date, session_type: str, *, mode: str) -> bool:
        facts = self._load_session_facts(trading_date, session_type, mode=mode)
        return any(
            (
                facts.candidates,
                facts.blockers,
                facts.intents,
                facts.broker_orders,
                facts.microstructure,
                facts.candles,
                facts.session_runs,
            )
        )

    def _load_session_facts(
        self,
        trading_date: date,
        session_type: str,
        *,
        micro_session_id: str | None = None,
        mode: str,
    ) -> _SessionFacts:
        def by_micro(statement: Any, model: Any) -> Any:
            statement = statement.where(
                model.trading_date == trading_date,
                model.session_type == session_type,
            )
            if micro_session_id is not None:
                statement = statement.where(model.micro_session_id == micro_session_id)
            return statement

        candidates = list(
            self._session.execute(by_micro(select(SignalCandidate), SignalCandidate)).scalars()
        )
        blockers = list(
            self._session.execute(by_micro(select(BlockerEvent), BlockerEvent)).scalars()
        )
        intents = list(self._session.execute(by_micro(select(OrderIntent), OrderIntent)).scalars())
        broker_orders = list(
            self._session.execute(by_micro(select(BrokerOrder), BrokerOrder)).scalars()
        )
        counterfactuals = list(
            self._session.execute(
                by_micro(select(CounterfactualResult), CounterfactualResult)
            ).scalars()
        )
        microstructure = list(
            self._session.execute(
                by_micro(select(MarketMicrostructureSnapshot), MarketMicrostructureSnapshot)
            ).scalars()
        )
        candles = list(
            self._session.execute(by_micro(select(MarketCandle), MarketCandle)).scalars()
        )
        session_runs = list(
            self._session.execute(by_micro(select(SessionRun), SessionRun)).scalars()
        )

        if mode != "all":
            candidates = [row for row in candidates if _row_mode(row) == mode]
            blockers = [row for row in blockers if _row_mode(row) == mode]
            intents = [row for row in intents if _row_mode(row) == mode]
            broker_orders = [row for row in broker_orders if _row_mode(row) == mode]
            counterfactuals = [row for row in counterfactuals if _row_mode(row) == mode]
            microstructure = [row for row in microstructure if _row_mode(row) == mode]
            candles = [row for row in candles if _row_mode(row) == mode]

        return _SessionFacts(
            trading_date=trading_date,
            session_type=session_type,
            candidates=candidates,
            blockers=blockers,
            intents=intents,
            broker_orders=broker_orders,
            counterfactuals=counterfactuals,
            microstructure=microstructure,
            candles=candles,
            session_runs=session_runs,
        )


class RollingPerformanceCubeService:
    """Build rolling contour statistics without hard-disabling low-sample scopes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_rolling_cube(
        self,
        window_names: Sequence[str] = ("7d", "20d", "60d", "90d", "180d", "365d"),
        *,
        universe: Sequence[str] | None = None,
        mode: str = "all",
    ) -> list[JsonPayload]:
        _validate_mode(mode)
        now = datetime.now(tz=UTC)
        instrument_ids = _normalize_universe(universe)
        rows: list[JsonPayload] = []
        for window_name in window_names:
            days = WINDOW_DAYS.get(window_name)
            if days is None:
                continue
            window_start = now - timedelta(days=days)
            rows.extend(
                self._build_window(
                    window_name=window_name,
                    window_start=window_start,
                    window_end=now,
                    universe=instrument_ids,
                    mode=mode,
                )
            )
        for row_payload in rows:
            self._session.add(_rolling_cube_row_from_payload(row_payload))
        self._session.flush()
        return rows

    def _build_window(
        self,
        *,
        window_name: str,
        window_start: datetime,
        window_end: datetime,
        universe: tuple[str, ...],
        mode: str,
    ) -> list[JsonPayload]:
        candidates = _filter_mode(
            _load_since(
                self._session,
                SignalCandidate,
                SignalCandidate.ts_utc,
                window_start,
                window_end,
                universe,
            ),
            mode,
        )
        blockers = _filter_mode(
            _load_since(
                self._session,
                BlockerEvent,
                BlockerEvent.ts_utc,
                window_start,
                window_end,
                universe,
            ),
            mode,
        )
        intents = _filter_mode(
            _load_since(
                self._session,
                OrderIntent,
                OrderIntent.created_ts,
                window_start,
                window_end,
                universe,
            ),
            mode,
        )
        broker_orders = _filter_mode(
            _load_since(
                self._session,
                BrokerOrder,
                BrokerOrder.last_observed_at,
                window_start,
                window_end,
                universe,
            ),
            mode,
        )
        counterfactuals = _filter_mode(
            _load_since(
                self._session,
                CounterfactualResult,
                CounterfactualResult.generated_at,
                window_start,
                window_end,
                universe,
            ),
            mode,
        )
        microstructure = _filter_mode(
            _load_since(
                self._session,
                MarketMicrostructureSnapshot,
                MarketMicrostructureSnapshot.ts_utc,
                window_start,
                window_end,
                universe,
            ),
            mode,
        )
        grouped_candidates: dict[tuple[str, str, str, str, str], list[SignalCandidate]] = (
            defaultdict(list)
        )
        for candidate in candidates:
            key = (
                candidate.instrument_id,
                candidate.session_type,
                candidate.timeframe,
                _normalize_side(candidate.side),
                _row_mode(candidate),
            )
            grouped_candidates[key].append(candidate)
        grouped_micro: dict[tuple[str, str, str, str, str], list[MarketMicrostructureSnapshot]] = (
            defaultdict(list)
        )
        for snapshot in microstructure:
            key = (
                snapshot.instrument_id,
                snapshot.session_type,
                "all",
                "all",
                _row_mode(snapshot),
            )
            grouped_micro[key].append(snapshot)

        keys = set(grouped_candidates) | set(grouped_micro)
        rows: list[JsonPayload] = []
        for key in sorted(keys):
            instrument_id, session_type, timeframe, side, row_mode = key
            scope_candidates = grouped_candidates.get(key, [])
            scope_micro = grouped_micro.get(key, [])
            if not scope_micro:
                scope_micro = [
                    snapshot
                    for snapshot in microstructure
                    if snapshot.instrument_id == instrument_id
                    and snapshot.session_type == session_type
                ]
            candidate_ids = {candidate.candidate_id for candidate in scope_candidates}
            scope_blockers = [
                blocker for blocker in blockers if blocker.candidate_id in candidate_ids
            ]
            scope_intents = [
                intent for intent in intents if intent.candidate_id in candidate_ids
            ]
            scope_orders = [
                order
                for order in broker_orders
                if order.candidate_id in candidate_ids
                or order.order_intent_id in {intent.order_intent_id for intent in scope_intents}
            ]
            scope_counterfactuals = [
                result for result in counterfactuals if result.candidate_id in candidate_ids
            ]
            rows.append(
                _rolling_cube_payload(
                    generated_at=datetime.now(tz=UTC),
                    window_name=window_name,
                    window_start=window_start,
                    window_end=window_end,
                    instrument_id=instrument_id,
                    session_type=session_type,
                    timeframe=timeframe,
                    side=side,
                    mode=row_mode,
                    candidates=scope_candidates,
                    blockers=scope_blockers,
                    intents=scope_intents,
                    broker_orders=scope_orders,
                    counterfactuals=scope_counterfactuals,
                    microstructure=scope_micro,
                )
            )
        return rows


class CalibrationDiagnosticService:
    """Run Calibration Center diagnostics and persist diagnostic runs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def diagnose_no_trade_period(
        self,
        universe: Sequence[str],
        lookback_days: int,
        *,
        mode: str = "all",
    ) -> JsonPayload:
        return self._diagnostic_payload(universe, lookback_days, mode=mode)

    def diagnose_robot_health(
        self,
        universe: Sequence[str],
        lookback_days: int,
        *,
        mode: str = "all",
    ) -> JsonPayload:
        return self._diagnostic_payload(universe, lookback_days, mode=mode)

    def run_diagnostics(
        self,
        universe: Sequence[str],
        lookback_days: int,
        *,
        trigger_type: str = "manual",
        requested_by: str | None = None,
        mode: str = "all",
    ) -> JsonPayload:
        _validate_mode(mode)
        now = datetime.now(tz=UTC)
        from_ts = now - timedelta(days=max(1, lookback_days))
        row = CalibrationDiagnosticRun(
            created_at=now,
            completed_at=None,
            requested_by=requested_by,
            trigger_type=trigger_type,
            status="running",
            from_ts=from_ts,
            to_ts=now,
            universe={"values": list(_normalize_universe(universe))},
            diagnosis="not_enough_data",
            confidence="low",
            blocking_issues={},
            warnings={},
            diagnostic_payload={},
        )
        self._session.add(row)
        self._session.flush()
        payload = self._diagnostic_payload(universe, lookback_days, mode=mode)
        row.completed_at = datetime.now(tz=UTC)
        row.status = "completed"
        row.diagnosis = str(payload["diagnosis"])
        row.confidence = str(payload["confidence"])
        row.blocking_issues = {"values": payload["blocking_issues"]}
        row.warnings = {"values": payload["warnings"]}
        row.diagnostic_payload = payload
        self._session.flush()
        return {"diagnostic_run_id": str(row.diagnostic_run_id), **payload}

    def _diagnostic_payload(
        self,
        universe: Sequence[str],
        lookback_days: int,
        *,
        mode: str,
    ) -> JsonPayload:
        now = datetime.now(tz=UTC)
        from_ts = now - timedelta(days=max(1, lookback_days))
        instrument_ids = _normalize_universe(universe)
        candidates = _filter_mode(
            _load_since(
                self._session,
                SignalCandidate,
                SignalCandidate.ts_utc,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        blockers = _filter_mode(
            _load_since(
                self._session,
                BlockerEvent,
                BlockerEvent.ts_utc,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        broker_orders = _filter_mode(
            _load_since(
                self._session,
                BrokerOrder,
                BrokerOrder.last_observed_at,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        microstructure = _filter_mode(
            _load_since(
                self._session,
                MarketMicrostructureSnapshot,
                MarketMicrostructureSnapshot.ts_utc,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        candles = _filter_mode(
            _load_since(
                self._session,
                MarketCandle,
                MarketCandle.close_ts_utc,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        regime_payload = MarketRegimeDiagnosticService(self._session).diagnose_market_regime(
            instrument_ids,
            lookback_days,
            mode=mode,
        )
        stats = _diagnostic_stats(
            candidates=candidates,
            blockers=blockers,
            broker_orders=broker_orders,
            microstructure=microstructure,
            candles=candles,
            regime_payload=regime_payload,
        )
        diagnosis, blocking_issues, warnings = _classify_diagnosis(stats)
        confidence = _confidence(
            candidate_count=int(stats["candidate_count"]),
            microstructure_count=int(stats["microstructure_count"]),
            active_days=int(stats["active_days"]),
        )
        calibration_recommended = diagnosis == "calibration_recommended"
        return {
            "generated_at": now.isoformat(),
            "from_ts": from_ts.isoformat(),
            "to_ts": now.isoformat(),
            "universe": list(instrument_ids),
            "mode": mode,
            "diagnosis": diagnosis,
            "confidence": confidence,
            "calibration_recommended": calibration_recommended,
            "stats": stats,
            "regime_summary": regime_payload,
            "warnings": warnings,
            "blocking_issues": blocking_issues,
            "explainability": _diagnostic_explanation(diagnosis),
        }


class MarketRegimeDiagnosticService:
    """Build market regime snapshots from microstructure and candles."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def diagnose_market_regime(
        self,
        universe: Sequence[str],
        lookback_days: int,
        *,
        mode: str = "all",
    ) -> JsonPayload:
        _validate_mode(mode)
        now = datetime.now(tz=UTC)
        from_ts = now - timedelta(days=max(1, lookback_days))
        instrument_ids = _normalize_universe(universe)
        microstructure = _filter_mode(
            _load_since(
                self._session,
                MarketMicrostructureSnapshot,
                MarketMicrostructureSnapshot.ts_utc,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        candles = _filter_mode(
            _load_since(
                self._session,
                MarketCandle,
                MarketCandle.close_ts_utc,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        candidates = _filter_mode(
            _load_since(
                self._session,
                SignalCandidate,
                SignalCandidate.ts_utc,
                from_ts,
                now,
                instrument_ids,
            ),
            mode,
        )
        rows: list[JsonPayload] = []
        grouped_keys = {
            (row.instrument_id, row.session_type)
            for row in [*microstructure, *candles, *candidates]
        }
        if not grouped_keys:
            grouped_keys = {(None, None)}
        for instrument_id, session_type in sorted(
            grouped_keys,
            key=lambda item: (item[0] or "", item[1] or ""),
        ):
            scope_micro = [
                row
                for row in microstructure
                if (instrument_id is None or row.instrument_id == instrument_id)
                and (session_type is None or row.session_type == session_type)
            ]
            scope_candles = [
                row
                for row in candles
                if (instrument_id is None or row.instrument_id == instrument_id)
                and (session_type is None or row.session_type == session_type)
            ]
            scope_candidates = [
                row
                for row in candidates
                if (instrument_id is None or row.instrument_id == instrument_id)
                and (session_type is None or row.session_type == session_type)
            ]
            payload = _market_regime_payload(
                generated_at=now,
                window_start=from_ts,
                window_end=now,
                instrument_id=instrument_id,
                session_type=session_type,
                microstructure=scope_micro,
                candles=scope_candles,
                candidates=scope_candidates,
            )
            rows.append(payload)
            self._session.add(_market_regime_row_from_payload(payload))
        self._session.flush()
        regime_counts = Counter(str(row["market_regime"]) for row in rows)
        return {
            "generated_at": now.isoformat(),
            "window_start": from_ts.isoformat(),
            "window_end": now.isoformat(),
            "rows": rows,
            "regime_counts": dict(sorted(regime_counts.items())),
            "dominant_regime": regime_counts.most_common(1)[0][0] if regime_counts else "unknown",
        }


class StrategyConfigProposalService:
    """Create draft config proposals only; never apply to active runtime."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_strategy_config_candidate(
        self,
        *,
        base_strategy_id: str,
        proposed_strategy_id: str | None = None,
        source_diagnostic_run_id: UUID | None = None,
        proposal_payload: JsonPayload | None = None,
        validation_payload: JsonPayload | None = None,
        caveats: JsonPayload | None = None,
        proposed_by: str = "system",
    ) -> JsonPayload:
        row = StrategyConfigCandidate(
            created_at=datetime.now(tz=UTC),
            source_diagnostic_run_id=source_diagnostic_run_id,
            base_strategy_id=base_strategy_id,
            proposed_strategy_id=proposed_strategy_id or f"{base_strategy_id}_candidate",
            status="draft",
            proposed_by=proposed_by,
            approval_required=True,
            approved_by=None,
            approved_at=None,
            proposal_payload={
                "apply_automatically": False,
                "runtime_config_changed": False,
                **(proposal_payload or {}),
            },
            validation_payload=validation_payload or {},
            caveats={
                "operator_approval_required": True,
                "no_live_config_auto_apply": True,
                "small_sample_not_final_truth": True,
                **(caveats or {}),
            },
            rejection_reason=None,
        )
        self._session.add(row)
        self._session.flush()
        return _candidate_payload(row)


def _intraday_summary_from_facts(
    facts: _SessionFacts,
    *,
    generated_at: datetime,
    mode: str,
    micro_session_id: str | None = None,
) -> JsonPayload:
    row_mode = _summary_mode(facts, requested_mode=mode)
    market_bias, trend_strength = _market_bias(facts.candles, facts.candidates)
    market_activity = _market_activity(facts.microstructure, facts.candidates, facts.candles)
    blocked_count = _blocked_count(facts.candidates, facts.blockers)
    near_miss_count = _near_miss_count(facts.counterfactuals)
    spread_values = _decimal_values(
        [row.spread_bps for row in facts.microstructure]
        + [row.spread_bps for row in facts.candidates]
        + [row.spread_bps for row in facts.blockers]
    )
    depth_values = _depth_values(facts.microstructure)
    imbalance_values = _decimal_values(row.book_imbalance for row in facts.microstructure)
    quality_values = _decimal_values(row.market_quality_score for row in facts.microstructure)
    no_trade_reason = _no_trade_reason(
        market_activity=market_activity,
        candidate_count=len(facts.candidates),
        blocked_count=blocked_count,
        stale_incidents=sum(1 for row in facts.microstructure if row.is_stale),
    )
    warnings = _small_sample_warnings(
        candidate_count=len(facts.candidates),
        microstructure_count=len(facts.microstructure),
    )
    session_status = _session_status(facts.session_runs)
    payload = {
        "generated_at": generated_at.isoformat(),
        "trading_date": facts.trading_date.isoformat(),
        "calendar_date": facts.trading_date.isoformat(),
        "session_type": facts.session_type,
        "session_phase": _session_phase(facts),
        "session_status": session_status,
        "micro_session_id": micro_session_id,
        "hour_bucket": None,
        "instrument_id": None,
        "timeframe": None,
        "side": "all",
        "mode": row_mode,
        "market_bias": market_bias,
        "market_activity": market_activity,
        "trend_strength": _decimal_or_none(trend_strength),
        "candidate_count": len(facts.candidates),
        "pseudo_order_count": _pseudo_order_count(facts.broker_orders),
        "real_order_count": _real_order_count(facts.broker_orders),
        "blocked_count": blocked_count,
        "near_miss_count": near_miss_count,
        "avg_spread_bps": _decimal_or_none(_avg(spread_values)),
        "p95_spread_bps": _decimal_or_none(_percentile(spread_values, Decimal("0.95"))),
        "avg_depth": _decimal_or_none(_avg(depth_values)),
        "avg_imbalance": _decimal_or_none(_avg(imbalance_values)),
        "avg_market_quality": _decimal_or_none(_avg(quality_values)),
        "stale_incidents": sum(1 for row in facts.microstructure if row.is_stale),
        "candle_lag_p95_seconds": _decimal_or_none(_candle_lag_p95(facts.candles)),
        "gross_pnl_proxy": _decimal_or_none(
            _sum_decimal(row.pnl_gross for row in facts.counterfactuals)
        ),
        "net_pnl_proxy": _decimal_or_none(
            _sum_decimal(row.pnl_net for row in facts.counterfactuals)
        ),
        "no_trade_reason": no_trade_reason,
        "closest_to_entry": _closest_to_entry(facts.blockers, facts.counterfactuals),
        "warnings": warnings,
        "spread_depth_imbalance_summary": {
            "avg_spread_bps": _string_or_none(_avg(spread_values)),
            "p95_spread_bps": _string_or_none(_percentile(spread_values, Decimal("0.95"))),
            "avg_depth": _string_or_none(_avg(depth_values)),
            "avg_imbalance": _string_or_none(_avg(imbalance_values)),
            "avg_market_quality": _string_or_none(_avg(quality_values)),
        },
        "analytics_payload": {
            "diagnostic_only": True,
            "does_not_enable_trading": True,
            "requested_mode": mode,
            "no_trade_reason": no_trade_reason,
            "closest_to_entry": _closest_to_entry(facts.blockers, facts.counterfactuals),
            "warnings": warnings,
            "session_status": session_status,
        },
    }
    return payload


def _scope_summaries(
    facts: _SessionFacts,
    *,
    generated_at: datetime,
    mode: str,
) -> list[JsonPayload]:
    rows: list[JsonPayload] = []
    by_key: dict[tuple[str | None, str | None, str], _SessionFacts] = {}
    keys: set[tuple[str | None, str | None, str]] = set()
    for candidate in facts.candidates:
        keys.add((candidate.instrument_id, candidate.timeframe, _normalize_side(candidate.side)))
        keys.add((candidate.instrument_id, candidate.timeframe, "all"))
    for snapshot in facts.microstructure:
        keys.add((snapshot.instrument_id, None, "all"))
    for candle in facts.candles:
        keys.add((candle.instrument_id, candle.timeframe, "all"))
    for instrument_id, timeframe, side in sorted(
        keys, key=lambda item: tuple(str(v) for v in item)
    ):
        scoped = _filter_facts(facts, instrument_id=instrument_id, timeframe=timeframe, side=side)
        by_key[(instrument_id, timeframe, side)] = scoped
    for (instrument_id, timeframe, side), scoped in by_key.items():
        row = _intraday_summary_from_facts(scoped, generated_at=generated_at, mode=mode)
        row["instrument_id"] = instrument_id
        row["timeframe"] = timeframe
        row["side"] = side
        rows.append(row)
    return rows


def _hour_summaries(
    facts: _SessionFacts,
    *,
    generated_at: datetime,
    mode: str,
) -> list[JsonPayload]:
    buckets: list[datetime] = []
    for fact_row in [*facts.candidates, *facts.blockers, *facts.microstructure]:
        ts = getattr(fact_row, "ts_utc", None)
        if isinstance(ts, datetime):
            buckets.append(_hour_bucket(ts))
    buckets = sorted(set(buckets))
    rows: list[JsonPayload] = []
    for bucket in buckets:
        scoped = _filter_facts(facts, hour_bucket=bucket)
        payload = _intraday_summary_from_facts(scoped, generated_at=generated_at, mode=mode)
        payload["hour_bucket"] = bucket.isoformat()
        payload["micro_session_id"] = None
        payload["analytics_payload"] = {
            **payload["analytics_payload"],
            "micro_session_summary": True,
        }
        rows.append(payload)
    return rows


def _filter_facts(
    facts: _SessionFacts,
    *,
    instrument_id: str | None = None,
    timeframe: str | None = None,
    side: str | None = None,
    hour_bucket: datetime | None = None,
) -> _SessionFacts:
    def matches(row: Any) -> bool:
        if instrument_id is not None and getattr(row, "instrument_id", None) != instrument_id:
            return False
        if timeframe is not None and getattr(row, "timeframe", None) != timeframe:
            return False
        if side not in {None, "all"} and _normalize_side(getattr(row, "side", None)) != side:
            return False
        if hour_bucket is not None:
            ts = getattr(row, "ts_utc", None)
            if ts is None or _hour_bucket(ts) != hour_bucket:
                return False
        return True

    candidate_ids = {row.candidate_id for row in facts.candidates if matches(row)}
    intent_ids = {
        row.order_intent_id
        for row in facts.intents
        if row.candidate_id in candidate_ids or matches(row)
    }
    return _SessionFacts(
        trading_date=facts.trading_date,
        session_type=facts.session_type,
        candidates=[row for row in facts.candidates if matches(row)],
        blockers=[
            row
            for row in facts.blockers
            if row.candidate_id in candidate_ids or matches(row)
        ],
        intents=[row for row in facts.intents if row.order_intent_id in intent_ids],
        broker_orders=[
            row
            for row in facts.broker_orders
            if (
                row.order_intent_id in intent_ids
                or row.candidate_id in candidate_ids
                or matches(row)
            )
        ],
        counterfactuals=[
            row
            for row in facts.counterfactuals
            if row.candidate_id in candidate_ids or row.order_intent_id in intent_ids
        ],
        microstructure=[row for row in facts.microstructure if matches(row)],
        candles=[row for row in facts.candles if matches(row)],
        session_runs=facts.session_runs,
    )


def _rolling_cube_payload(
    *,
    generated_at: datetime,
    window_name: str,
    window_start: datetime,
    window_end: datetime,
    instrument_id: str,
    session_type: str,
    timeframe: str,
    side: str,
    mode: str,
    candidates: list[SignalCandidate],
    blockers: list[BlockerEvent],
    intents: list[OrderIntent],
    broker_orders: list[BrokerOrder],
    counterfactuals: list[CounterfactualResult],
    microstructure: list[MarketMicrostructureSnapshot],
) -> JsonPayload:
    candidate_count = len(candidates)
    active_days = len({candidate.trading_date for candidate in candidates}) or len(
        {snapshot.trading_date for snapshot in microstructure}
    )
    net_pnl = _sum_decimal(row.pnl_net for row in counterfactuals)
    gross_pnl = _sum_decimal(row.pnl_gross for row in counterfactuals)
    sample_warning = (
        "small_sample_early_evidence_only"
        if candidate_count < 20 or active_days < 10
        else None
    )
    confidence = _confidence(
        candidate_count=candidate_count,
        microstructure_count=len(microstructure),
        active_days=active_days,
    )
    contour_status = _contour_status(
        mode=mode,
        candidate_count=candidate_count,
        sample_warning=sample_warning,
        microstructure_count=len(microstructure),
    )
    depth_values = _depth_values(microstructure)
    spread_values = _decimal_values(row.spread_bps for row in microstructure)
    return {
        "generated_at": generated_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_name": window_name,
        "instrument_id": instrument_id,
        "session_type": session_type,
        "timeframe": timeframe,
        "side": side,
        "mode": mode,
        "candidate_count": candidate_count,
        "approved_count": len(intents),
        "blocked_count": _blocked_count(candidates, blockers),
        "pseudo_order_count": _pseudo_order_count(broker_orders),
        "real_order_count": _real_order_count(broker_orders),
        "gross_pnl_proxy": str(gross_pnl),
        "net_pnl_proxy": str(net_pnl),
        "avg_net_pnl_proxy": str(
            (net_pnl / Decimal(candidate_count)).quantize(Decimal("0.0001"))
            if candidate_count
            else ZERO
        ),
        "win_proxy": _string_or_none(_win_proxy(counterfactuals)),
        "avg_spread_bps": _string_or_none(_avg(spread_values)),
        "p95_spread_bps": _string_or_none(_percentile(spread_values, Decimal("0.95"))),
        "avg_depth": _string_or_none(_avg(depth_values)),
        "p95_depth": _string_or_none(_percentile(depth_values, Decimal("0.95"))),
        "avg_imbalance": _string_or_none(
            _avg(_decimal_values(row.book_imbalance for row in microstructure))
        ),
        "avg_market_quality": _string_or_none(
            _avg(_decimal_values(row.market_quality_score for row in microstructure))
        ),
        "stale_incidents": sum(1 for row in microstructure if row.is_stale),
        "stream_gap_count": _stream_gap_count(microstructure),
        "active_days": active_days,
        "last_signal_at": max((row.ts_utc for row in candidates), default=None),
        "sample_warning": sample_warning,
        "confidence": confidence,
        "contour_status": contour_status,
        "cube_payload": {
            "low_sample_does_not_disable_contour": sample_warning is not None,
            "diagnostic_only": True,
            "candidate_ids": [str(row.candidate_id) for row in candidates[:20]],
        },
    }


def _market_regime_payload(
    *,
    generated_at: datetime,
    window_start: datetime,
    window_end: datetime,
    instrument_id: str | None,
    session_type: str | None,
    microstructure: list[MarketMicrostructureSnapshot],
    candles: list[MarketCandle],
    candidates: list[SignalCandidate],
) -> JsonPayload:
    spread_values = _decimal_values(row.spread_bps for row in microstructure)
    depth_values = _depth_values(microstructure)
    imbalance_values = _decimal_values(row.book_imbalance for row in microstructure)
    quality_values = _decimal_values(row.market_quality_score for row in microstructure)
    volatility = _candle_volatility_bps(candles)
    volume = _sum_decimal(row.volume_lots for row in candles)
    p95_spread = _percentile(spread_values, Decimal("0.95"))
    avg_depth = _avg(depth_values)
    avg_quality = _avg(quality_values)
    regime = "normal"
    if not microstructure and not candles and not candidates:
        regime = "unknown"
    elif len(microstructure) < 5 and len(candidates) == 0 and volume <= ZERO:
        regime = "low_activity"
    elif p95_spread is not None and p95_spread >= Decimal("25"):
        regime = "wide_spread"
    elif avg_depth is not None and avg_depth <= Decimal("5"):
        regime = "thin_book"
    elif volatility is not None and volatility >= Decimal("150"):
        regime = "high_volatility"
    return {
        "generated_at": generated_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "instrument_id": instrument_id,
        "session_type": session_type,
        "market_regime": regime,
        "volume_score": str(volume),
        "volatility_score": _string_or_none(volatility),
        "spread_score": _string_or_none(p95_spread),
        "depth_score": _string_or_none(avg_depth),
        "imbalance_score": _string_or_none(_avg(imbalance_values)),
        "candidate_frequency_score": str(len(candidates)),
        "regime_payload": {
            "microstructure_count": len(microstructure),
            "candle_count": len(candles),
            "candidate_count": len(candidates),
            "avg_market_quality": _string_or_none(avg_quality),
            "diagnostic_only": True,
        },
    }


def _diagnostic_stats(
    *,
    candidates: list[SignalCandidate],
    blockers: list[BlockerEvent],
    broker_orders: list[BrokerOrder],
    microstructure: list[MarketMicrostructureSnapshot],
    candles: list[MarketCandle],
    regime_payload: JsonPayload,
) -> JsonPayload:
    stale_incidents = sum(1 for row in microstructure if row.is_stale)
    microstructure_count = len(microstructure)
    blocker_count = _blocked_count(candidates, blockers)
    active_days = len(
        {row.trading_date for row in candidates}
        | {row.trading_date for row in microstructure}
        | {row.trading_date for row in candles}
    )
    first_half_blockers, second_half_blockers = _split_blocker_counts(blockers)
    spread_values = _decimal_values(row.spread_bps for row in microstructure)
    depth_values = _depth_values(microstructure)
    return {
        "candidate_count": len(candidates),
        "blocker_count": blocker_count,
        "blocker_rate": _ratio_string(blocker_count, len(candidates)),
        "pseudo_order_count": _pseudo_order_count(broker_orders),
        "real_order_count": _real_order_count(broker_orders),
        "microstructure_count": microstructure_count,
        "candle_count": len(candles),
        "active_days": active_days,
        "stale_incidents": stale_incidents,
        "stale_rate": _ratio_string(stale_incidents, microstructure_count),
        "avg_spread_bps": _string_or_none(_avg(spread_values)),
        "p95_spread_bps": _string_or_none(_percentile(spread_values, Decimal("0.95"))),
        "avg_depth": _string_or_none(_avg(depth_values)),
        "volatility_bps": _string_or_none(_candle_volatility_bps(candles)),
        "dominant_regime": regime_payload.get("dominant_regime", "unknown"),
        "blockers_first_half": first_half_blockers,
        "blockers_second_half": second_half_blockers,
        "blocker_drift": second_half_blockers >= max(5, first_half_blockers * 2),
        "regime_changed": _regime_changed(microstructure, candles),
    }


def _classify_diagnosis(stats: JsonPayload) -> tuple[str, list[str], list[str]]:
    warnings: list[str] = []
    blocking_issues: list[str] = []
    candidate_count = int(stats["candidate_count"])
    microstructure_count = int(stats["microstructure_count"])
    candle_count = int(stats["candle_count"])
    stale_rate = Decimal(str(stats["stale_rate"]))
    blocker_drift = bool(stats["blocker_drift"])
    dominant_regime = str(stats["dominant_regime"])
    if stale_rate >= Decimal("0.25"):
        blocking_issues.append("stale_microstructure_high")
        return "data_quality_problem", blocking_issues, warnings
    if microstructure_count == 0 and candle_count == 0:
        warnings.append("no_market_data_in_window")
        return "not_enough_data", blocking_issues, warnings
    if candidate_count == 0 and dominant_regime == "low_activity":
        return "market_dead", blocking_issues, warnings
    if candidate_count < 5 and microstructure_count < 20 and candle_count < 20:
        warnings.append("small_sample_early_evidence_only")
        return "not_enough_data", blocking_issues, warnings
    if candidate_count == 0 and dominant_regime in {"normal", "wide_spread", "thin_book"}:
        warnings.append("no_signal_candidates_with_market_activity")
        return "calibration_recommended", blocking_issues, warnings
    if blocker_drift and dominant_regime == "normal":
        blocking_issues.append("blocker_drift_material")
        return "robot_too_strict", blocking_issues, warnings
    if bool(stats["regime_changed"]) or dominant_regime in {
        "wide_spread",
        "thin_book",
        "high_volatility",
    }:
        blocking_issues.append("market_regime_or_microstructure_drift")
        return "regime_changed", blocking_issues, warnings
    if Decimal(str(stats["blocker_rate"])) >= Decimal("0.75") and candidate_count >= 10:
        warnings.append("high_blocker_rate_review_thresholds")
        return "calibration_recommended", blocking_issues, warnings
    return "normal_no_action_needed", blocking_issues, warnings


def _diagnostic_explanation(diagnosis: str) -> str:
    return {
        "market_dead": "No signals/trades and market activity is low across the selected universe.",
        "robot_too_strict": "Market activity is normal but blockers increased materially.",
        "data_quality_problem": "Missing, stale or gapped data dominates the diagnostic window.",
        "regime_changed": "Spread, depth or volatility changed materially.",
        "not_enough_data": "The sample is too small; collect more data-only shadow evidence.",
        "normal_no_action_needed": "No material market or robot-health issue was detected.",
        "calibration_recommended": (
            "Calibration should be reviewed as a proposal, not auto-applied."
        ),
    }.get(diagnosis, "Diagnostic outcome is stored in the payload.")


def _intraday_row_from_payload(payload: JsonPayload) -> IntradaySessionAnalytics:
    return IntradaySessionAnalytics(
        generated_at=_parse_dt(str(payload["generated_at"])),
        trading_date=date.fromisoformat(str(payload["trading_date"])),
        calendar_date=date.fromisoformat(str(payload["calendar_date"])),
        session_type=str(payload["session_type"]),
        session_phase=str(payload["session_phase"]),
        micro_session_id=_optional_str(payload.get("micro_session_id")),
        hour_bucket=_parse_optional_dt(payload.get("hour_bucket")),
        instrument_id=_optional_str(payload.get("instrument_id")),
        timeframe=_optional_str(payload.get("timeframe")),
        side=_optional_str(payload.get("side")),
        mode=str(payload["mode"]),
        market_bias=str(payload["market_bias"]),
        market_activity=str(payload["market_activity"]),
        trend_strength=_optional_decimal(payload.get("trend_strength")),
        candidate_count=int(payload["candidate_count"]),
        pseudo_order_count=int(payload["pseudo_order_count"]),
        real_order_count=int(payload["real_order_count"]),
        blocked_count=int(payload["blocked_count"]),
        near_miss_count=int(payload["near_miss_count"]),
        avg_spread_bps=_optional_decimal(payload.get("avg_spread_bps")),
        p95_spread_bps=_optional_decimal(payload.get("p95_spread_bps")),
        avg_depth=_optional_decimal(payload.get("avg_depth")),
        avg_imbalance=_optional_decimal(payload.get("avg_imbalance")),
        avg_market_quality=_optional_decimal(payload.get("avg_market_quality")),
        stale_incidents=int(payload["stale_incidents"]),
        candle_lag_p95_seconds=_optional_decimal(payload.get("candle_lag_p95_seconds")),
        gross_pnl_proxy=_optional_decimal(payload.get("gross_pnl_proxy")),
        net_pnl_proxy=_optional_decimal(payload.get("net_pnl_proxy")),
        analytics_payload=dict(payload["analytics_payload"]),
    )


def _rolling_cube_row_from_payload(payload: JsonPayload) -> RollingPerformanceCube:
    return RollingPerformanceCube(
        generated_at=_parse_dt(str(payload["generated_at"])),
        window_start=_parse_dt(str(payload["window_start"])),
        window_end=_parse_dt(str(payload["window_end"])),
        window_name=str(payload["window_name"]),
        instrument_id=str(payload["instrument_id"]),
        session_type=str(payload["session_type"]),
        timeframe=str(payload["timeframe"]),
        side=str(payload["side"]),
        mode=str(payload["mode"]),
        candidate_count=int(payload["candidate_count"]),
        approved_count=int(payload["approved_count"]),
        blocked_count=int(payload["blocked_count"]),
        pseudo_order_count=int(payload["pseudo_order_count"]),
        real_order_count=int(payload["real_order_count"]),
        gross_pnl_proxy=Decimal(str(payload["gross_pnl_proxy"])),
        net_pnl_proxy=Decimal(str(payload["net_pnl_proxy"])),
        avg_net_pnl_proxy=Decimal(str(payload["avg_net_pnl_proxy"])),
        win_proxy=_optional_decimal(payload.get("win_proxy")),
        avg_spread_bps=_optional_decimal(payload.get("avg_spread_bps")),
        p95_spread_bps=_optional_decimal(payload.get("p95_spread_bps")),
        avg_depth=_optional_decimal(payload.get("avg_depth")),
        p95_depth=_optional_decimal(payload.get("p95_depth")),
        avg_imbalance=_optional_decimal(payload.get("avg_imbalance")),
        avg_market_quality=_optional_decimal(payload.get("avg_market_quality")),
        stale_incidents=int(payload["stale_incidents"]),
        stream_gap_count=int(payload["stream_gap_count"]),
        active_days=int(payload["active_days"]),
        last_signal_at=_parse_optional_dt(payload.get("last_signal_at")),
        sample_warning=_optional_str(payload.get("sample_warning")),
        confidence=str(payload["confidence"]),
        contour_status=str(payload["contour_status"]),
        cube_payload=dict(payload["cube_payload"]),
    )


def _market_regime_row_from_payload(payload: JsonPayload) -> MarketRegimeSnapshot:
    return MarketRegimeSnapshot(
        generated_at=_parse_dt(str(payload["generated_at"])),
        window_start=_parse_dt(str(payload["window_start"])),
        window_end=_parse_dt(str(payload["window_end"])),
        instrument_id=_optional_str(payload.get("instrument_id")),
        session_type=_optional_str(payload.get("session_type")),
        market_regime=str(payload["market_regime"]),
        volume_score=_optional_decimal(payload.get("volume_score")),
        volatility_score=_optional_decimal(payload.get("volatility_score")),
        spread_score=_optional_decimal(payload.get("spread_score")),
        depth_score=_optional_decimal(payload.get("depth_score")),
        imbalance_score=_optional_decimal(payload.get("imbalance_score")),
        candidate_frequency_score=_optional_decimal(payload.get("candidate_frequency_score")),
        regime_payload=dict(payload["regime_payload"]),
    )


def _session_payload_from_rows(rows: list[JsonPayload]) -> JsonPayload:
    summary = rows[0] if rows else {}
    return {
        "generated_at": summary.get("generated_at", datetime.now(tz=UTC).isoformat()),
        "trading_date": summary.get("trading_date"),
        "session_summaries": [row for row in rows if row.get("instrument_id") is None],
        "instrument_summaries": _latest_by(rows, "instrument_id"),
        "timeframe_summaries": _latest_by(rows, "timeframe"),
        "side_summaries": _latest_by(rows, "side"),
        "market_bias": summary.get("market_bias", "unknown"),
        "market_activity": summary.get("market_activity", "unknown"),
        "near_miss_count": summary.get("near_miss_count", 0),
        "spread_depth_imbalance_summary": summary.get("spread_depth_imbalance_summary", {}),
        "warnings": summary.get("warnings", []),
        "rows": rows,
    }


def _combine_intraday_payloads(
    *,
    trading_date: date,
    session_payloads: list[JsonPayload],
) -> JsonPayload:
    rows = [row for payload in session_payloads for row in payload.get("rows", [])]
    summary_rows = [row for row in rows if row.get("instrument_id") is None]
    warnings = sorted({warning for row in summary_rows for warning in row.get("warnings", [])})
    bias = _combine_bias([str(row.get("market_bias", "unknown")) for row in summary_rows])
    activity = _combine_activity(
        [str(row.get("market_activity", "unknown")) for row in summary_rows]
    )
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "trading_date": trading_date.isoformat(),
        "session_summaries": summary_rows,
        "instrument_summaries": _latest_by(rows, "instrument_id"),
        "timeframe_summaries": _latest_by(rows, "timeframe"),
        "side_summaries": _latest_by(rows, "side"),
        "market_bias": bias,
        "market_activity": activity,
        "near_miss_count": sum(int(row.get("near_miss_count", 0)) for row in summary_rows),
        "spread_depth_imbalance_summary": _combined_spread_summary(summary_rows),
        "warnings": warnings,
        "rows": rows,
    }


def _latest_by(rows: list[JsonPayload], key: str) -> list[JsonPayload]:
    grouped: dict[str, JsonPayload] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        grouped[str(value)] = row
    return [grouped[item] for item in sorted(grouped)]


def _combined_spread_summary(rows: list[JsonPayload]) -> JsonPayload:
    return {
        "avg_spread_bps": _string_or_none(
            _avg(_decimal_values(row.get("avg_spread_bps") for row in rows))
        ),
        "p95_spread_bps": _string_or_none(
            _percentile(_decimal_values(row.get("p95_spread_bps") for row in rows), Decimal("0.95"))
        ),
        "avg_depth": _string_or_none(_avg(_decimal_values(row.get("avg_depth") for row in rows))),
        "avg_imbalance": _string_or_none(
            _avg(_decimal_values(row.get("avg_imbalance") for row in rows))
        ),
    }


def _candidate_payload(row: StrategyConfigCandidate) -> JsonPayload:
    return {
        "candidate_config_id": str(row.candidate_config_id),
        "created_at": row.created_at.isoformat(),
        "source_diagnostic_run_id": (
            str(row.source_diagnostic_run_id) if row.source_diagnostic_run_id else None
        ),
        "base_strategy_id": row.base_strategy_id,
        "proposed_strategy_id": row.proposed_strategy_id,
        "status": row.status,
        "proposed_by": row.proposed_by,
        "approval_required": row.approval_required,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "proposal_payload": row.proposal_payload,
        "validation_payload": row.validation_payload,
        "caveats": row.caveats,
        "rejection_reason": row.rejection_reason,
    }


def _load_since(
    session: Session,
    model: Any,
    ts_column: Any,
    start: datetime,
    end: datetime,
    universe: tuple[str, ...],
) -> list[Any]:
    stmt = select(model).where(ts_column >= start, ts_column <= end)
    if universe and hasattr(model, "instrument_id"):
        stmt = stmt.where(model.instrument_id.in_(universe))
    return list(session.execute(stmt).scalars())


def _filter_mode(rows: list[Any], mode: str) -> list[Any]:
    if mode == "all":
        return rows
    return [row for row in rows if _row_mode(row) == mode]


def _row_mode(row: Any) -> str:
    payload: dict[str, Any] = {}
    for attr in (
        "signal_payload",
        "reason_payload",
        "intent_payload",
        "broker_payload",
        "result_payload",
        "snapshot_payload",
        "candle_payload",
    ):
        value = getattr(row, attr, None)
        if isinstance(value, dict):
            payload.update(value)
    source = str(getattr(row, "source", "") or payload.get("source", "")).lower()
    launch_mode = str(payload.get("launch_mode", "") or payload.get("runtime_mode", "")).lower()
    mode = f"{source} {launch_mode}"
    if "data_only_shadow" in mode or "data-shadow" in mode:
        return "data_shadow"
    if "historical" in mode or "replay" in mode:
        return "historical"
    if "sandbox" in mode:
        return "sandbox"
    if "production" in mode or "live" in mode:
        return "live"
    if "shadow" in mode:
        return "strategy_shadow"
    if isinstance(row, MarketMicrostructureSnapshot):
        return "data_shadow"
    return "historical"


def _summary_mode(facts: _SessionFacts, *, requested_mode: str) -> str:
    if requested_mode != "all":
        return requested_mode
    modes = [
        _row_mode(row)
        for row in [
            *facts.candidates,
            *facts.blockers,
            *facts.intents,
            *facts.broker_orders,
            *facts.counterfactuals,
            *facts.microstructure,
            *facts.candles,
        ]
    ]
    if not modes:
        return "historical"
    return Counter(modes).most_common(1)[0][0]


def _normalize_universe(universe: Sequence[str] | None) -> tuple[str, ...]:
    values: list[str] = []
    for item in universe or ():
        raw = item.strip()
        if not raw:
            continue
        values.append(raw if ":" in raw else f"MOEX:{raw.upper()}")
    return tuple(dict.fromkeys(values))


def _validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        msg = f"Unsupported analytics mode: {mode}"
        raise ValueError(msg)


def _normalize_side(side: object) -> str:
    value = str(side or "all").lower()
    if value in {"buy", "long"}:
        return "long"
    if value in {"sell", "short"}:
        return "short"
    return "all"


def _market_bias(
    candles: list[MarketCandle],
    candidates: list[SignalCandidate],
) -> tuple[str, Decimal | None]:
    returns: list[Decimal] = []
    by_scope: dict[tuple[str, str], list[MarketCandle]] = defaultdict(list)
    for candle in candles:
        if candle.is_closed:
            by_scope[(candle.instrument_id, candle.timeframe)].append(candle)
    for values in by_scope.values():
        ordered = sorted(values, key=lambda item: item.close_ts_utc)
        first = ordered[0]
        last = ordered[-1]
        if first.open_price > ZERO:
            returns.append(
                ((last.close_price - first.open_price) / first.open_price * Decimal("10000"))
                .quantize(Decimal("0.0001"))
            )
    if returns:
        avg_return = _avg(returns) or ZERO
        positive = sum(1 for value in returns if value >= Decimal("25"))
        negative = sum(1 for value in returns if value <= Decimal("-25"))
        if positive and negative:
            return "mixed", avg_return
        if avg_return >= Decimal("25"):
            return "long_bias", avg_return
        if avg_return <= Decimal("-25"):
            return "short_bias", avg_return
        return "sideways", avg_return
    sides = Counter(_normalize_side(candidate.side) for candidate in candidates)
    if sides["long"] and sides["short"]:
        return "mixed", None
    if sides["long"]:
        return "long_bias", None
    if sides["short"]:
        return "short_bias", None
    return "unknown", None


def _market_activity(
    microstructure: list[MarketMicrostructureSnapshot],
    candidates: list[SignalCandidate],
    candles: list[MarketCandle],
) -> str:
    volume = _sum_decimal(candle.volume_lots for candle in candles)
    if not microstructure and not candidates and not candles:
        return "unknown"
    if len(microstructure) >= 300 or len(candidates) >= 50:
        return "high"
    if len(microstructure) < 5 and len(candidates) == 0 and volume <= ZERO:
        return "low"
    if len(microstructure) < 10 and len(candidates) == 0 and volume < Decimal("10"):
        return "low"
    return "normal"


def _blocked_count(candidates: list[Any], blockers: list[BlockerEvent]) -> int:
    candidate_blocked = {
        candidate.candidate_id
        for candidate in candidates
        if getattr(candidate, "candidate_status", None) == "blocked"
    }
    blocker_blocked = {
        blocker.candidate_id
        for blocker in blockers
        if not blocker.passed and blocker.candidate_id is not None
    }
    return len(candidate_blocked | blocker_blocked)


def _near_miss_count(counterfactuals: list[CounterfactualResult]) -> int:
    return sum(1 for row in counterfactuals if (row.pnl_net or ZERO) > ZERO)


def _pseudo_order_count(orders: list[BrokerOrder]) -> int:
    return sum(1 for row in orders if _is_pseudo_order(row))


def _real_order_count(orders: list[BrokerOrder]) -> int:
    return sum(1 for row in orders if _payload_bool(row.broker_payload, "real_broker_call") is True)


def _is_pseudo_order(row: BrokerOrder) -> bool:
    if row.broker_status.startswith("pseudo"):
        return True
    real_call = _payload_bool(row.broker_payload, "real_broker_call")
    return real_call is False


def _payload_bool(payload: dict[str, object], key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    for nested in payload.values():
        if isinstance(nested, dict):
            nested_value = nested.get(key)
            if isinstance(nested_value, bool):
                return nested_value
    return None


def _depth_values(rows: Iterable[MarketMicrostructureSnapshot]) -> list[Decimal]:
    values: list[Decimal] = []
    for row in rows:
        if row.bid_depth_lots is not None and row.ask_depth_lots is not None:
            values.append(((row.bid_depth_lots + row.ask_depth_lots) / Decimal("2")).quantize(
                Decimal("0.0001")
            ))
    return values


def _decimal_values(values: Iterable[Any]) -> list[Decimal]:
    result: list[Decimal] = []
    for value in values:
        if value is not None:
            result.append(Decimal(str(value)))
    return result


def _avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, ZERO) / Decimal(len(values))).quantize(Decimal("0.0001"))


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * percentile).to_integral_value())
    return ordered[index].quantize(Decimal("0.0001"))


def _sum_decimal(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None), ZERO).quantize(Decimal("0.0001"))


def _win_proxy(counterfactuals: list[CounterfactualResult]) -> Decimal | None:
    if not counterfactuals:
        return None
    wins = sum(1 for row in counterfactuals if (row.pnl_net or ZERO) > ZERO)
    return (Decimal(wins) / Decimal(len(counterfactuals))).quantize(Decimal("0.0001"))


def _small_sample_warnings(*, candidate_count: int, microstructure_count: int) -> list[str]:
    warnings: list[str] = []
    if candidate_count < 10:
        warnings.append("small_candidate_sample")
    if microstructure_count < 30:
        warnings.append("small_microstructure_sample")
    if warnings:
        warnings.append("small_sample_is_early_evidence_not_final_truth")
    return warnings


def _no_trade_reason(
    *,
    market_activity: str,
    candidate_count: int,
    blocked_count: int,
    stale_incidents: int,
) -> str:
    if stale_incidents > 0 and candidate_count == 0:
        return "data_quality_problem"
    if candidate_count == 0 and market_activity == "low":
        return "market_dead"
    if candidate_count == 0:
        return "no_signal_candidates"
    if blocked_count >= candidate_count:
        return "robot_too_strict_or_risk_blocked"
    return "signals_present_no_real_trade_expected"


def _closest_to_entry(
    blockers: list[BlockerEvent],
    counterfactuals: list[CounterfactualResult],
) -> list[JsonPayload]:
    near = [
        {
            "candidate_id": str(row.candidate_id) if row.candidate_id else None,
            "reason": row.blocker_code or row.cancel_reason_code,
            "net_pnl_proxy": str(row.pnl_net or ZERO),
        }
        for row in sorted(
            counterfactuals,
            key=lambda item: item.pnl_net or ZERO,
            reverse=True,
        )
        if (row.pnl_net or ZERO) > ZERO
    ][:5]
    if near:
        return near
    return [
        {
            "candidate_id": str(row.candidate_id) if row.candidate_id else None,
            "reason": row.reason_code,
            "measured_value": str(row.measured_value)
            if row.measured_value is not None
            else None,
            "threshold_value": str(row.threshold_value)
            if row.threshold_value is not None
            else None,
        }
        for row in blockers
        if not row.passed
    ][:5]


def _session_phase(facts: _SessionFacts) -> str:
    if facts.session_runs:
        latest = max(facts.session_runs, key=lambda item: item.started_at)
        return latest.session_phase
    for rows in (facts.microstructure, facts.candidates, facts.candles):
        if rows:
            return rows[0].session_phase
    return "closed"


def _session_status(runs: list[SessionRun]) -> str:
    if not runs:
        return "not_started"
    if any(run.status in {"open", "running"} for run in runs):
        return "running"
    return "completed"


def _hour_bucket(value: datetime) -> datetime:
    ts = _ensure_utc(value)
    return ts.replace(minute=0, second=0, microsecond=0)


def _candle_lag_p95(candles: list[MarketCandle]) -> Decimal | None:
    values = []
    for candle in candles:
        if candle.exchange_close_ts is not None:
            values.append(
                Decimal(
                    str(
                        max(
                            0.0,
                            (
                                candle.close_ts_utc - candle.exchange_close_ts
                            ).total_seconds(),
                        )
                    )
                )
            )
    return _percentile(values, Decimal("0.95"))


def _candle_volatility_bps(candles: list[MarketCandle]) -> Decimal | None:
    returns: list[Decimal] = []
    for candle in candles:
        if candle.open_price > ZERO:
            returns.append(
                abs(
                    (
                        (candle.close_price - candle.open_price)
                        / candle.open_price
                        * Decimal("10000")
                    )
                    .quantize(Decimal("0.0001"))
                )
            )
    return _avg(returns)


def _stream_gap_count(rows: list[MarketMicrostructureSnapshot]) -> int:
    return sum(
        1
        for row in rows
        if row.snapshot_payload.get("stream_gap") is True
        or row.snapshot_payload.get("gap_recovered") is True
    )


def _confidence(*, candidate_count: int, microstructure_count: int, active_days: int) -> str:
    if active_days >= 60 and (candidate_count >= 200 or microstructure_count >= 5000):
        return "high"
    if active_days >= 20 and (candidate_count >= 50 or microstructure_count >= 1000):
        return "medium"
    return "low"


def _contour_status(
    *,
    mode: str,
    candidate_count: int,
    sample_warning: str | None,
    microstructure_count: int,
) -> str:
    if sample_warning is not None:
        return "data_only" if candidate_count == 0 and microstructure_count > 0 else "research_only"
    if mode == "data_shadow":
        return "data_only"
    if mode == "strategy_shadow":
        return "shadow_only"
    return "active"


def _split_blocker_counts(blockers: list[BlockerEvent]) -> tuple[int, int]:
    if not blockers:
        return 0, 0
    ordered = sorted(blockers, key=lambda row: row.ts_utc)
    midpoint = len(ordered) // 2
    return (
        sum(1 for row in ordered[:midpoint] if not row.passed),
        sum(1 for row in ordered[midpoint:] if not row.passed),
    )


def _regime_changed(
    microstructure: list[MarketMicrostructureSnapshot],
    candles: list[MarketCandle],
) -> bool:
    if len(microstructure) < 20 and len(candles) < 20:
        return False
    ordered_micro = sorted(microstructure, key=lambda row: row.ts_utc)
    first_micro = ordered_micro[: len(ordered_micro) // 2]
    second_micro = ordered_micro[len(ordered_micro) // 2 :]
    first_spread = _avg(_decimal_values(row.spread_bps for row in first_micro))
    second_spread = _avg(_decimal_values(row.spread_bps for row in second_micro))
    first_depth = _avg(_depth_values(first_micro))
    second_depth = _avg(_depth_values(second_micro))
    if (
        first_spread is not None
        and second_spread is not None
        and first_spread > ZERO
        and second_spread >= first_spread * Decimal("1.5")
    ):
        return True
    if (
        first_depth is not None
        and second_depth is not None
        and first_depth > ZERO
        and second_depth <= first_depth * Decimal("0.5")
    ):
        return True
    ordered_candles = sorted(candles, key=lambda row: row.close_ts_utc)
    first_vol = _candle_volatility_bps(ordered_candles[: len(ordered_candles) // 2])
    second_vol = _candle_volatility_bps(ordered_candles[len(ordered_candles) // 2 :])
    return (
        first_vol is not None
        and second_vol is not None
        and first_vol > ZERO
        and second_vol >= first_vol * Decimal("2")
    )


def _ratio_string(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0"
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001")))


def _combine_bias(values: list[str]) -> str:
    present = [value for value in values if value != "unknown"]
    if not present:
        return "unknown"
    if "long_bias" in present and "short_bias" in present:
        return "mixed"
    counts = Counter(present)
    return counts.most_common(1)[0][0]


def _combine_activity(values: list[str]) -> str:
    if "high" in values:
        return "high"
    if "normal" in values:
        return "normal"
    if "low" in values:
        return "low"
    return "unknown"


def _parse_dt(value: str) -> datetime:
    return _ensure_utc(datetime.fromisoformat(value))


def _parse_optional_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    return _parse_dt(str(value))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _decimal_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _string_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None

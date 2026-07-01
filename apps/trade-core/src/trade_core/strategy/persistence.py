"""Persistence helpers for strategy decisions, blockers, and state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from trade_core.market_data import MarketState
from trade_core.session import SessionSnapshot
from trade_core.strategy.models import (
    RiskDecision,
    SignalCandidateDecision,
    StrategyState,
)
from trade_core.strategy.state_machine import StrategyStateMachine
from trading_common.db.models import (
    BlockerEvent,
    CandidateStageResult,
    MarketContextSnapshot,
    RiskEvent,
    SignalCandidate,
    StrategyStateEvent,
)
from trading_common.db.repositories import (
    BlockerEventRepository,
    CandidateStageResultRepository,
    MarketContextSnapshotRepository,
    RiskEventRepository,
    SignalCandidateRepository,
    StrategyStateEventRepository,
)
from trading_common.observability import DomainEventType
from trading_common.telemetry import bind_context, get_logger, log_event

LOGGER = get_logger(__name__)


class SqlAlchemyStrategyEventStore:
    """Writes machine-readable strategy events to PostgreSQL-backed tables."""

    def __init__(
        self,
        *,
        candidates: SignalCandidateRepository,
        blockers: BlockerEventRepository,
        risk_events: RiskEventRepository,
        state_events: StrategyStateEventRepository,
        candidate_stages: CandidateStageResultRepository | None = None,
        market_contexts: MarketContextSnapshotRepository | None = None,
        state_machine: StrategyStateMachine | None = None,
    ) -> None:
        self._candidates = candidates
        self._blockers = blockers
        self._risk_events = risk_events
        self._state_events = state_events
        self._candidate_stages = candidate_stages
        self._market_contexts = market_contexts
        self._state_machine = state_machine or StrategyStateMachine()

    def record_state_transition(
        self,
        *,
        snapshot: SessionSnapshot,
        strategy_id: str,
        strategy_version: int,
        previous_state: StrategyState | None,
        new_state: StrategyState,
        event_type: str,
        reason_code: str | None,
        instrument_id: str | None = None,
        payload: dict[str, object] | None = None,
        ts_utc: datetime | None = None,
    ) -> StrategyStateEvent:
        if previous_state is not None:
            self._state_machine.validate_transition(previous_state, new_state)
        event = StrategyStateEvent(
            **_session_context(snapshot),
            ts_utc=ts_utc or datetime.now(tz=UTC),
            exchange_ts=None,
            received_ts=None,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            instrument_id=instrument_id,
            previous_state=previous_state.value if previous_state is not None else None,
            new_state=new_state.value,
            event_type=event_type,
            reason_code=reason_code,
            state_payload=payload or {},
        )
        return self._state_events.create(event)

    def record_candidate(
        self,
        *,
        decision: SignalCandidateDecision,
        snapshot: SessionSnapshot,
        market_state: MarketState | None,
        run_id: UUID | None = None,
        ts_utc: datetime | None = None,
    ) -> SignalCandidate:
        candidate_payload: dict[str, object] = {
            **_session_context(snapshot),
            "ts_utc": ts_utc or datetime.now(tz=UTC),
            "exchange_ts": None,
            "received_ts": None,
            "run_id": run_id,
            "instrument_id": decision.instrument.instrument_id,
            "strategy_id": decision.strategy_id,
            "strategy_version": decision.strategy_version,
            "timeframe": decision.timeframe.value,
            "side": decision.side.value,
            "signal_type": decision.action.value,
            "candidate_status": "created",
            "expected_edge_bps": decision.expected_edge_bps,
            "expected_holding_minutes": decision.expected_holding_minutes,
            "last_price": decision.intended_price,
            "mid_price": market_state.mid_price if market_state is not None else None,
            "spread_abs": market_state.spread_abs if market_state is not None else None,
            "spread_bps": market_state.spread_bps if market_state is not None else None,
            "market_quality_score": (
                market_state.market_quality_score if market_state is not None else None
            ),
            "book_imbalance": market_state.book_imbalance if market_state is not None else None,
            "candle_age_ms": None,
            "data_freshness_ms": (
                market_state.feed_freshness.age_ms if market_state is not None else None
            ),
            "signal_fingerprint": decision.signal_fingerprint,
            "signal_payload": {
                "event_type": DomainEventType.SIGNAL_CANDIDATE_CREATED.value,
                "order_type": decision.order_type,
                "lot_qty": decision.lot_qty,
                "time_in_force": decision.time_in_force,
                "condition_payload": decision.condition_payload,
            },
        }
        if decision.candidate_id is not None:
            candidate_payload["candidate_id"] = decision.candidate_id
        candidate = SignalCandidate(**candidate_payload)
        persisted = self._candidates.create_idempotent(candidate)
        if self._market_contexts is not None and market_state is not None:
            self._market_contexts.create_idempotent(
                MarketContextSnapshot(
                    **_session_context(snapshot),
                    ts_utc=ts_utc or datetime.now(tz=UTC),
                    exchange_ts=None,
                    received_ts=None,
                    candidate_id=persisted.candidate_id,
                    instrument_id=persisted.instrument_id,
                    timeframe=persisted.timeframe,
                    snapshot_kind="signal_candidate_created",
                    last_price=persisted.last_price,
                    mid_price=market_state.mid_price,
                    best_bid_price=market_state.best_bid.price if market_state.best_bid else None,
                    best_ask_price=market_state.best_ask.price if market_state.best_ask else None,
                    spread_abs=market_state.spread_abs,
                    spread_bps=market_state.spread_bps,
                    bid_depth_lots=market_state.bid_depth_lots,
                    ask_depth_lots=market_state.ask_depth_lots,
                    book_imbalance=market_state.book_imbalance,
                    market_quality_score=market_state.market_quality_score,
                    candle_age_ms=None,
                    data_freshness_ms=market_state.feed_freshness.age_ms,
                    feature_snapshot=market_state.as_read_model(),
                    explanation_payload={
                        "event_type": DomainEventType.MARKET_CONTEXT_SNAPSHOT_WRITTEN.value,
                        "snapshot_kind": "signal_candidate_created",
                    },
                )
            )
        _log_candidate_event(
            event_type=DomainEventType.SIGNAL_CANDIDATE_CREATED.value,
            candidate=persisted,
            stage_name="candidate_creation",
            payload={"signal_fingerprint": persisted.signal_fingerprint},
        )
        return persisted

    def record_blockers(
        self,
        *,
        candidate: SignalCandidate,
        decision: RiskDecision,
        market_state: MarketState | None,
        ts_utc: datetime | None = None,
    ) -> tuple[BlockerEvent, ...]:
        events: list[BlockerEvent] = []
        for blocker in decision.blockers:
            if self._candidate_stages is not None:
                self._candidate_stages.create_idempotent(
                    CandidateStageResult(
                        calendar_date=candidate.calendar_date,
                        trading_date=candidate.trading_date,
                        session_type=candidate.session_type,
                        session_phase=candidate.session_phase,
                        micro_session_id=candidate.micro_session_id,
                        broker_trading_status=candidate.broker_trading_status,
                        ts_utc=ts_utc or datetime.now(tz=UTC),
                        exchange_ts=None,
                        received_ts=None,
                        candidate_id=candidate.candidate_id,
                        instrument_id=candidate.instrument_id,
                        timeframe=candidate.timeframe,
                        strategy_id=candidate.strategy_id,
                        strategy_version=candidate.strategy_version,
                        stage_seq=blocker.gate_rank,
                        stage_name=blocker.gate_name,
                        stage_outcome="passed" if blocker.passed else "blocked",
                        passed=blocker.passed,
                        blocker_code=blocker.code.value,
                        blocker_family=_blocker_family(blocker.code.value),
                        measured_value=blocker.observed_value,
                        threshold_value=blocker.limit_value,
                        explanation_payload={
                            "event_type": (DomainEventType.CANDIDATE_STAGE_RESULT_RECORDED.value),
                            **blocker.reason_payload,
                        },
                    )
                )
                _log_candidate_event(
                    event_type=DomainEventType.CANDIDATE_STAGE_RESULT_RECORDED.value,
                    candidate=candidate,
                    stage_name=blocker.gate_name,
                    payload={
                        "stage_seq": blocker.gate_rank,
                        "stage_outcome": "passed" if blocker.passed else "blocked",
                        "blocker_code": blocker.code.value,
                    },
                )
            if blocker.passed:
                continue
            events.append(
                self._blockers.create_idempotent(
                    BlockerEvent(
                        calendar_date=candidate.calendar_date,
                        trading_date=candidate.trading_date,
                        session_type=candidate.session_type,
                        session_phase=candidate.session_phase,
                        micro_session_id=candidate.micro_session_id,
                        broker_trading_status=candidate.broker_trading_status,
                        ts_utc=ts_utc or datetime.now(tz=UTC),
                        exchange_ts=None,
                        received_ts=None,
                        candidate_id=candidate.candidate_id,
                        instrument_id=candidate.instrument_id,
                        timeframe=candidate.timeframe,
                        strategy_id=candidate.strategy_id,
                        gate_name=blocker.gate_name,
                        gate_rank=blocker.gate_rank,
                        stage_seq=blocker.gate_rank,
                        stage_name=blocker.gate_name,
                        stage_outcome="blocked",
                        passed=blocker.passed,
                        reason_code=blocker.code.value,
                        blocker_code=blocker.code.value,
                        blocker_family=_blocker_family(blocker.code.value),
                        measured_value=blocker.observed_value,
                        threshold_value=blocker.limit_value,
                        reason_payload={
                            "event_type": DomainEventType.BLOCKER_TRIGGERED.value,
                            **blocker.reason_payload,
                        },
                        explanation_payload=blocker.reason_payload,
                        is_final_blocker=blocker.is_final_blocker,
                        blocker_rank=blocker.gate_rank if not blocker.passed else None,
                        market_quality_score=(
                            market_state.market_quality_score if market_state is not None else None
                        ),
                        spread_bps=market_state.spread_bps if market_state is not None else None,
                        expected_edge_bps=candidate.expected_edge_bps,
                    )
                )
            )
            _log_candidate_event(
                event_type=DomainEventType.BLOCKER_TRIGGERED.value,
                candidate=candidate,
                level="WARNING" if blocker.is_final_blocker else "INFO",
                stage_name=blocker.gate_name,
                payload={
                    "blocker_code": blocker.code.value,
                    "blocker_family": _blocker_family(blocker.code.value),
                    "is_final_blocker": blocker.is_final_blocker,
                    "measured_value": str(blocker.observed_value)
                    if blocker.observed_value is not None
                    else None,
                    "threshold_value": str(blocker.limit_value)
                    if blocker.limit_value is not None
                    else None,
                },
            )
            if blocker.is_final_blocker and self._market_contexts is not None:
                self._market_contexts.create_idempotent(
                    _counterfactual_seed_snapshot(
                        candidate=candidate,
                        blocker_code=blocker.code.value,
                        blocker_family=_blocker_family(blocker.code.value),
                        market_state=market_state,
                        ts_utc=ts_utc or datetime.now(tz=UTC),
                    )
                )
        if decision.allowed:
            self._candidates.update_status(candidate.candidate_id, "allowed")
        else:
            self._candidates.update_status(candidate.candidate_id, "blocked")
        return tuple(events)

    def record_risk_events(
        self,
        *,
        candidate: SignalCandidate,
        decision: RiskDecision,
        ts_utc: datetime | None = None,
    ) -> tuple[RiskEvent, ...]:
        failed_blockers = tuple(blocker for blocker in decision.blockers if not blocker.passed)
        events = tuple(
            self._risk_events.create(
                RiskEvent(
                    calendar_date=candidate.calendar_date,
                    trading_date=candidate.trading_date,
                    session_type=candidate.session_type,
                    session_phase=candidate.session_phase,
                    micro_session_id=candidate.micro_session_id,
                    broker_trading_status=candidate.broker_trading_status,
                    ts_utc=ts_utc or datetime.now(tz=UTC),
                    exchange_ts=None,
                    received_ts=None,
                    candidate_id=candidate.candidate_id,
                    order_intent_id=None,
                    instrument_id=candidate.instrument_id,
                    timeframe=candidate.timeframe,
                    risk_rule=blocker.gate_name,
                    severity="warning" if blocker.is_final_blocker else "info",
                    reason_code=blocker.code.value,
                    limit_value=blocker.limit_value,
                    observed_value=blocker.observed_value,
                    action_taken="block_candidate" if blocker.is_final_blocker else "observe",
                    risk_payload={
                        "event_type": DomainEventType.RISK_EVENT_RECORDED.value,
                        **blocker.reason_payload,
                    },
                )
            )
            for blocker in failed_blockers
        )
        return events


def _session_context(snapshot: SessionSnapshot) -> dict[str, object]:
    return {
        "calendar_date": snapshot.calendar_date,
        "trading_date": snapshot.trading_date,
        "session_type": snapshot.session_type.value,
        "session_phase": snapshot.session_phase.value,
        "micro_session_id": snapshot.micro_session_id or "unassigned",
        "broker_trading_status": snapshot.broker_trading_status,
    }


def decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _counterfactual_seed_snapshot(
    *,
    candidate: SignalCandidate,
    blocker_code: str,
    blocker_family: str,
    market_state: MarketState | None,
    ts_utc: datetime,
) -> MarketContextSnapshot:
    return MarketContextSnapshot(
        calendar_date=candidate.calendar_date,
        trading_date=candidate.trading_date,
        session_type=candidate.session_type,
        session_phase=candidate.session_phase,
        micro_session_id=candidate.micro_session_id,
        broker_trading_status=candidate.broker_trading_status,
        ts_utc=ts_utc,
        exchange_ts=None,
        received_ts=None,
        candidate_id=candidate.candidate_id,
        instrument_id=candidate.instrument_id,
        timeframe=candidate.timeframe,
        snapshot_kind="counterfactual_seed_snapshot",
        last_price=candidate.last_price,
        mid_price=market_state.mid_price if market_state is not None else candidate.mid_price,
        best_bid_price=market_state.best_bid.price
        if market_state is not None and market_state.best_bid is not None
        else None,
        best_ask_price=market_state.best_ask.price
        if market_state is not None and market_state.best_ask is not None
        else None,
        spread_abs=market_state.spread_abs if market_state is not None else candidate.spread_abs,
        spread_bps=market_state.spread_bps if market_state is not None else candidate.spread_bps,
        bid_depth_lots=market_state.bid_depth_lots if market_state is not None else None,
        ask_depth_lots=market_state.ask_depth_lots if market_state is not None else None,
        book_imbalance=(
            market_state.book_imbalance if market_state is not None else candidate.book_imbalance
        ),
        market_quality_score=market_state.market_quality_score
        if market_state is not None
        else candidate.market_quality_score,
        candle_age_ms=candidate.candle_age_ms,
        data_freshness_ms=market_state.feed_freshness.age_ms
        if market_state is not None
        else candidate.data_freshness_ms,
        feature_snapshot=market_state.as_read_model() if market_state is not None else {},
        explanation_payload={
            "event_type": DomainEventType.MARKET_CONTEXT_SNAPSHOT_WRITTEN.value,
            "snapshot_kind": "counterfactual_seed_snapshot",
            "source_event_type": "blocked_candidate",
            "blocker_code": blocker_code,
            "blocker_family": blocker_family,
            "counterfactual_horizons_minutes": [5, 10, 15],
            "candidate_side": candidate.side,
            "signal_type": candidate.signal_type,
            "expected_edge_bps": str(candidate.expected_edge_bps)
            if candidate.expected_edge_bps is not None
            else None,
        },
    )


def _blocker_family(blocker_code: str) -> str:
    if blocker_code in {"spread_too_wide", "market_quality_low", "stale_market_data"}:
        return "market_quality"
    if blocker_code in {"session_forbidden", "order_type_forbidden"}:
        return "session_policy"
    if blocker_code in {
        "risk_budget_exceeded",
        "max_drawdown_reached",
        "position_limit_reached",
        "exit_without_position",
        "exit_quantity_exceeds_position",
        "instrument_lot_size_unknown",
        "price_tick_invalid",
        "insufficient_margin",
        "max_short_exposure_reached",
        "max_long_exposure_reached",
        "position_side_conflict",
    }:
        return "risk_limits"
    if blocker_code in {
        "short_not_allowed_by_config",
        "short_not_allowed_by_broker",
        "short_permission_unknown",
    }:
        return "short_selling_policy"
    if blocker_code in {"open_order_conflict"}:
        return "execution_safety"
    if blocker_code in {"total_costs_exceed_edge"}:
        return "cost_model"
    return "strategy_edge"


def _log_candidate_event(
    *,
    event_type: str,
    candidate: SignalCandidate,
    stage_name: str,
    payload: dict[str, object],
    level: str = "INFO",
) -> None:
    with bind_context(
        session_type=candidate.session_type,
        exchange_phase=candidate.session_phase,
        micro_session_id=candidate.micro_session_id,
        instrument=candidate.instrument_id,
        timeframe=candidate.timeframe,
        strategy_id=candidate.strategy_id,
        strategy_version=str(candidate.strategy_version),
        candidate_id=str(candidate.candidate_id),
    ):
        log_event(
            logger=LOGGER,
            level=level,
            event_type=event_type,
            component="strategy.persistence",
            stage_name=stage_name,
            details=payload,
        )

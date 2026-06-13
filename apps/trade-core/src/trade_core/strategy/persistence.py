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
    RiskEvent,
    SignalCandidate,
    StrategyStateEvent,
)
from trading_common.db.repositories import (
    BlockerEventRepository,
    RiskEventRepository,
    SignalCandidateRepository,
    StrategyStateEventRepository,
)
from trading_common.observability import DomainEventType


class SqlAlchemyStrategyEventStore:
    """Writes machine-readable strategy events to PostgreSQL-backed tables."""

    def __init__(
        self,
        *,
        candidates: SignalCandidateRepository,
        blockers: BlockerEventRepository,
        risk_events: RiskEventRepository,
        state_events: StrategyStateEventRepository,
        state_machine: StrategyStateMachine | None = None,
    ) -> None:
        self._candidates = candidates
        self._blockers = blockers
        self._risk_events = risk_events
        self._state_events = state_events
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
        candidate = SignalCandidate(
            **_session_context(snapshot),
            ts_utc=ts_utc or datetime.now(tz=UTC),
            exchange_ts=None,
            received_ts=None,
            run_id=run_id,
            instrument_id=decision.instrument.instrument_id,
            strategy_id=decision.strategy_id,
            strategy_version=decision.strategy_version,
            timeframe=decision.timeframe.value,
            side=decision.side.value,
            signal_type=decision.action.value,
            candidate_status="created",
            expected_edge_bps=decision.expected_edge_bps,
            expected_holding_minutes=decision.expected_holding_minutes,
            last_price=decision.intended_price,
            mid_price=market_state.mid_price if market_state is not None else None,
            spread_abs=market_state.spread_abs if market_state is not None else None,
            spread_bps=market_state.spread_bps if market_state is not None else None,
            market_quality_score=(
                market_state.market_quality_score if market_state is not None else None
            ),
            book_imbalance=market_state.book_imbalance if market_state is not None else None,
            candle_age_ms=None,
            data_freshness_ms=(
                market_state.feed_freshness.age_ms if market_state is not None else None
            ),
            signal_fingerprint=decision.signal_fingerprint,
            signal_payload={
                "event_type": DomainEventType.SIGNAL_CANDIDATE_CREATED.value,
                "order_type": decision.order_type,
                "lot_qty": decision.lot_qty,
                "time_in_force": decision.time_in_force,
                "condition_payload": decision.condition_payload,
            },
        )
        return self._candidates.create(candidate)

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
            events.append(
                self._blockers.create(
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
                        strategy_id=candidate.strategy_id,
                        gate_name=blocker.gate_name,
                        gate_rank=blocker.gate_rank,
                        passed=blocker.passed,
                        reason_code=blocker.code.value,
                        reason_payload={
                            "event_type": DomainEventType.BLOCKER_TRIGGERED.value,
                            **blocker.reason_payload,
                        },
                        is_final_blocker=blocker.is_final_blocker,
                        blocker_rank=blocker.gate_rank if not blocker.passed else None,
                        market_quality_score=(
                            market_state.market_quality_score
                            if market_state is not None
                            else None
                        ),
                        spread_bps=market_state.spread_bps if market_state is not None else None,
                        expected_edge_bps=candidate.expected_edge_bps,
                    )
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

"""Strategy state machine validation."""

from __future__ import annotations

from trade_core.strategy.models import StrategyState


class InvalidStrategyTransition(ValueError):
    """Raised when a strategy transition violates the canonical state machine."""


ALLOWED_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]] = {
    StrategyState.IDLE: frozenset(
        {
            StrategyState.WARMING_UP,
            StrategyState.WAIT,
            StrategyState.CANDIDATE,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.WARMING_UP: frozenset(
        {
            StrategyState.WAIT,
            StrategyState.CANDIDATE,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.WAIT: frozenset(
        {StrategyState.CANDIDATE, StrategyState.DEGRADED, StrategyState.STOPPED}
    ),
    StrategyState.CANDIDATE: frozenset(
        {
            StrategyState.BLOCKED,
            StrategyState.PLACING_ORDER,
            StrategyState.WAIT,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.BLOCKED: frozenset(
        {StrategyState.WAIT, StrategyState.DEGRADED, StrategyState.STOPPED}
    ),
    StrategyState.PLACING_ORDER: frozenset(
        {
            StrategyState.WORKING_ORDER,
            StrategyState.BLOCKED,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.WORKING_ORDER: frozenset(
        {
            StrategyState.PARTIALLY_FILLED,
            StrategyState.IN_POSITION,
            StrategyState.EXITING,
            StrategyState.WAIT,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.PARTIALLY_FILLED: frozenset(
        {
            StrategyState.WORKING_ORDER,
            StrategyState.IN_POSITION,
            StrategyState.EXITING,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.IN_POSITION: frozenset(
        {
            StrategyState.EXITING,
            StrategyState.WAIT,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.EXITING: frozenset(
        {
            StrategyState.WORKING_ORDER,
            StrategyState.WAIT,
            StrategyState.DEGRADED,
            StrategyState.STOPPED,
        }
    ),
    StrategyState.DEGRADED: frozenset({StrategyState.WAIT, StrategyState.STOPPED}),
    StrategyState.STOPPED: frozenset({StrategyState.STOPPED}),
}


class StrategyStateMachine:
    """Small validator used before persisting state transitions."""

    def can_transition(self, previous: StrategyState, new: StrategyState) -> bool:
        if previous == new:
            return True
        return new in ALLOWED_TRANSITIONS[previous]

    def validate_transition(self, previous: StrategyState, new: StrategyState) -> None:
        if not self.can_transition(previous, new):
            msg = f"Invalid strategy transition: {previous.value} -> {new.value}"
            raise InvalidStrategyTransition(msg)

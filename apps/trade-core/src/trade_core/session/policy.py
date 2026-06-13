"""Session-aware order permission policy."""

from __future__ import annotations

from dataclasses import dataclass

from trade_core.session.models import SessionSnapshot
from trade_core.session.reason_codes import (
    ORDER_TYPE_FORBIDDEN,
    PHASE_FORBIDDEN,
    SESSION_FORBIDDEN,
)
from trading_common.enums import SessionPhase


@dataclass(frozen=True, slots=True)
class OrderPermission:
    allowed: bool
    reason_code: str | None
    allowed_order_types: tuple[str, ...]
    allowed_actions: tuple[str, ...]


class OrderSessionPolicy:
    """Gate order actions by resolved session phase."""

    _phase_actions: dict[SessionPhase, tuple[str, ...]] = {
        SessionPhase.OPENING_AUCTION: ("cancel",),
        SessionPhase.CONTINUOUS_TRADING: ("entry", "exit", "cancel", "replace"),
        SessionPhase.CLOSING_AUCTION: ("exit", "cancel"),
        SessionPhase.BREAK: ("cancel",),
        SessionPhase.DEALER_MODE: (),
        SessionPhase.CLOSED: (),
    }
    _phase_order_types: dict[SessionPhase, tuple[str, ...]] = {
        SessionPhase.OPENING_AUCTION: ("limit",),
        SessionPhase.CONTINUOUS_TRADING: ("limit", "market", "stop", "stop_limit"),
        SessionPhase.CLOSING_AUCTION: ("limit", "market"),
        SessionPhase.BREAK: ("limit", "market", "stop", "stop_limit"),
        SessionPhase.DEALER_MODE: (),
        SessionPhase.CLOSED: (),
    }

    def evaluate(
        self,
        *,
        snapshot: SessionSnapshot,
        action: str,
        order_type: str,
    ) -> OrderPermission:
        normalized_action = action.strip().lower()
        normalized_order_type = order_type.strip().lower()
        allowed_actions = self._phase_actions[snapshot.session_phase]
        allowed_order_types = self._phase_order_types[snapshot.session_phase]

        if snapshot.schedule_phase is None:
            return self._deny(SESSION_FORBIDDEN, allowed_order_types, allowed_actions)

        if normalized_action not in allowed_actions:
            return self._deny(
                snapshot.deny_reason_code or PHASE_FORBIDDEN,
                allowed_order_types,
                allowed_actions,
            )

        if normalized_action != "cancel" and normalized_order_type not in allowed_order_types:
            return self._deny(ORDER_TYPE_FORBIDDEN, allowed_order_types, allowed_actions)

        if (
            snapshot.session_phase == SessionPhase.CONTINUOUS_TRADING
            and not snapshot.is_trading_allowed
        ):
            return self._deny(
                snapshot.deny_reason_code or PHASE_FORBIDDEN,
                allowed_order_types,
                allowed_actions,
            )

        return OrderPermission(
            allowed=True,
            reason_code=None,
            allowed_order_types=allowed_order_types,
            allowed_actions=allowed_actions,
        )

    @staticmethod
    def _deny(
        reason_code: str,
        allowed_order_types: tuple[str, ...],
        allowed_actions: tuple[str, ...],
    ) -> OrderPermission:
        return OrderPermission(
            allowed=False,
            reason_code=reason_code,
            allowed_order_types=allowed_order_types,
            allowed_actions=allowed_actions,
        )

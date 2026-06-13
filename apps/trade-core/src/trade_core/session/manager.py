"""SessionManager combines exchange schedules with live broker status."""

from __future__ import annotations

from datetime import datetime

from trade_core.session.models import BrokerTradingStatus, SessionSnapshot, TradingSchedule
from trade_core.session.reason_codes import (
    PHASE_FORBIDDEN,
    SESSION_FORBIDDEN,
    WEEKEND_BROKER_MODE,
)
from trading_common.enums import SessionPhase, SessionType


class SessionManager:
    """Resolve current trading session without hardcoded exchange hours."""

    def evaluate(
        self,
        *,
        now: datetime,
        schedule: TradingSchedule,
        broker_status: BrokerTradingStatus,
    ) -> SessionSnapshot:
        window = schedule.active_window(now)
        broker_phase = broker_phase_from_status(broker_status)

        if window is None:
            session_type = SessionType.WEEKEND if now.weekday() >= 5 else SessionType.WEEKDAY_MAIN
            return SessionSnapshot(
                observed_at=now,
                calendar_date=now.date(),
                trading_date=now.date(),
                session_type=session_type,
                session_phase=SessionPhase.CLOSED,
                broker_phase=broker_phase,
                broker_trading_status=broker_status.status,
                broker_api_trade_available=broker_status.api_trade_available,
                schedule_phase=None,
                schedule_window_start_at=None,
                schedule_window_end_at=None,
                micro_session_id=None,
                is_trading_allowed=False,
                deny_reason_code=SESSION_FORBIDDEN,
                status_mismatch=broker_phase != SessionPhase.CLOSED,
            )

        effective_phase = resolve_effective_phase(
            schedule_phase=window.session_phase,
            broker_phase=broker_phase,
            api_trade_available=broker_status.api_trade_available,
        )
        status_mismatch = (
            broker_phase != window.session_phase
            or (
                window.session_phase == SessionPhase.CONTINUOUS_TRADING
                and not broker_status.api_trade_available
            )
        )
        is_allowed = (
            effective_phase == SessionPhase.CONTINUOUS_TRADING
            and broker_status.api_trade_available
        )

        return SessionSnapshot(
            observed_at=now,
            calendar_date=window.calendar_date or now.date(),
            trading_date=window.trading_date,
            session_type=window.session_type,
            session_phase=effective_phase,
            broker_phase=broker_phase,
            broker_trading_status=broker_status.status,
            broker_api_trade_available=broker_status.api_trade_available,
            schedule_phase=window.session_phase,
            schedule_window_start_at=window.start_at,
            schedule_window_end_at=window.end_at,
            micro_session_id=None,
            is_trading_allowed=is_allowed,
            deny_reason_code=None
            if is_allowed
            else deny_reason_for(window.session_type, effective_phase, broker_status),
            status_mismatch=status_mismatch,
        )


def broker_phase_from_status(status: BrokerTradingStatus) -> SessionPhase:
    normalized = status.normalized_status

    if "dealer" in normalized:
        return SessionPhase.DEALER_MODE
    if "opening" in normalized:
        return SessionPhase.OPENING_AUCTION
    if "closing" in normalized or "auction_price" in normalized:
        return SessionPhase.CLOSING_AUCTION
    if "break" in normalized:
        return SessionPhase.BREAK
    if normalized in {"normal", "normal_trading", "session_open", "trading", "open"}:
        return SessionPhase.CONTINUOUS_TRADING
    if "not_available" in normalized or "closed" in normalized or "close" in normalized:
        return SessionPhase.CLOSED
    if normalized in {"unspecified", "unknown", ""}:
        return SessionPhase.CLOSED
    return SessionPhase.CLOSED


def resolve_effective_phase(
    *,
    schedule_phase: SessionPhase,
    broker_phase: SessionPhase,
    api_trade_available: bool,
) -> SessionPhase:
    if schedule_phase != SessionPhase.CONTINUOUS_TRADING:
        return schedule_phase
    if not api_trade_available:
        return (
            broker_phase
            if broker_phase != SessionPhase.CONTINUOUS_TRADING
            else SessionPhase.CLOSED
        )
    return broker_phase


def deny_reason_for(
    session_type: SessionType,
    phase: SessionPhase,
    broker_status: BrokerTradingStatus,
) -> str:
    if session_type == SessionType.WEEKEND and (
        phase in {SessionPhase.DEALER_MODE, SessionPhase.CLOSED}
        or not broker_status.api_trade_available
    ):
        return WEEKEND_BROKER_MODE
    return PHASE_FORBIDDEN

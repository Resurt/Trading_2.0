"""Typed session models shared by SessionManager and micro-session manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from trading_common.enums import SessionPhase, SessionType

JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class BrokerTradingStatus:
    """SDK-neutral broker trading status observation."""

    status: str
    api_trade_available: bool = True
    instrument_id: str | None = None
    exchange_ts: datetime | None = None
    raw_payload: Mapping[str, object] = field(default_factory=dict)

    @property
    def normalized_status(self) -> str:
        return self.status.strip().lower().removeprefix("security_trading_status_")


@dataclass(frozen=True, slots=True)
class ScheduleWindow:
    """One exchange schedule interval already fetched from TradingSchedules."""

    session_type: SessionType
    session_phase: SessionPhase
    start_at: datetime
    end_at: datetime
    trading_date: date
    calendar_date: date | None = None

    def contains(self, moment: datetime) -> bool:
        return self.start_at <= moment < self.end_at


@dataclass(frozen=True, slots=True)
class TradingSchedule:
    """Prepared exchange schedule for a day or range of days."""

    windows: tuple[ScheduleWindow, ...]

    def active_window(self, moment: datetime) -> ScheduleWindow | None:
        for window in sorted(self.windows, key=lambda item: item.start_at):
            if window.contains(moment):
                return window
        return None


@dataclass(frozen=True, slots=True)
class SessionEventContext:
    """Canonical session context saved with domain events."""

    calendar_date: date
    trading_date: date
    session_type: SessionType
    session_phase: SessionPhase
    micro_session_id: str
    broker_trading_status: str

    def as_db_values(self) -> JsonObject:
        return {
            "calendar_date": self.calendar_date,
            "trading_date": self.trading_date,
            "session_type": self.session_type.value,
            "session_phase": self.session_phase.value,
            "micro_session_id": self.micro_session_id,
            "broker_trading_status": self.broker_trading_status,
        }

    def as_read_model(self) -> JsonObject:
        return {
            "calendar_date": self.calendar_date.isoformat(),
            "trading_date": self.trading_date.isoformat(),
            "session_type": self.session_type.value,
            "session_phase": self.session_phase.value,
            "micro_session_id": self.micro_session_id,
            "broker_trading_status": self.broker_trading_status,
        }


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Current session read-model candidate for API/UI and trading gates."""

    observed_at: datetime
    calendar_date: date
    trading_date: date
    session_type: SessionType
    session_phase: SessionPhase
    broker_phase: SessionPhase
    broker_trading_status: str
    broker_api_trade_available: bool
    schedule_phase: SessionPhase | None
    schedule_window_start_at: datetime | None
    schedule_window_end_at: datetime | None
    micro_session_id: str | None
    is_trading_allowed: bool
    deny_reason_code: str | None
    status_mismatch: bool
    source: str = "trading_schedule_and_broker_status"

    def with_micro_session(self, micro_session_id: str | None) -> SessionSnapshot:
        return replace(self, micro_session_id=micro_session_id)

    def event_context(self, micro_session_id: str) -> SessionEventContext:
        return SessionEventContext(
            calendar_date=self.calendar_date,
            trading_date=self.trading_date,
            session_type=self.session_type,
            session_phase=self.session_phase,
            micro_session_id=micro_session_id,
            broker_trading_status=self.broker_trading_status,
        )

    def as_read_model(self) -> JsonObject:
        return {
            "observed_at": self.observed_at.isoformat(),
            "calendar_date": self.calendar_date.isoformat(),
            "trading_date": self.trading_date.isoformat(),
            "session_type": self.session_type.value,
            "session_phase": self.session_phase.value,
            "broker_phase": self.broker_phase.value,
            "broker_trading_status": self.broker_trading_status,
            "broker_api_trade_available": self.broker_api_trade_available,
            "schedule_phase": self.schedule_phase.value if self.schedule_phase else None,
            "schedule_window_start_at": (
                self.schedule_window_start_at.isoformat()
                if self.schedule_window_start_at
                else None
            ),
            "schedule_window_end_at": (
                self.schedule_window_end_at.isoformat() if self.schedule_window_end_at else None
            ),
            "micro_session_id": self.micro_session_id,
            "is_trading_allowed": self.is_trading_allowed,
            "deny_reason_code": self.deny_reason_code,
            "status_mismatch": self.status_mismatch,
            "source": self.source,
        }


def floor_hour(moment: datetime) -> datetime:
    """Return the exchange-hour floor for a timezone-aware datetime."""

    return moment.replace(minute=0, second=0, microsecond=0)


def next_hour(moment: datetime) -> datetime:
    return floor_hour(moment) + timedelta(hours=1)


def micro_session_id_for(snapshot: SessionSnapshot, moment: datetime) -> str:
    """Build a stable logical micro-session id from trading context and hour bucket."""

    bucket_start = floor_hour(moment)
    return (
        f"{snapshot.trading_date.isoformat()}:"
        f"{snapshot.session_type.value}:"
        f"{bucket_start:%Y%m%dT%H%M}"
    )

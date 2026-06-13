"""Hourly logical micro-sessions inside long-lived trade-core."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from uuid import UUID

from trade_core.session.models import (
    JsonObject,
    SessionEventContext,
    SessionSnapshot,
    floor_hour,
    micro_session_id_for,
    next_hour,
)
from trade_core.session.persistence import SessionStateStore
from trade_core.session.reason_codes import EXCHANGE_SESSION_BOUNDARY, HOURLY_ROLLOVER
from trading_common.enums import SessionPhase, SessionType


@dataclass(frozen=True, slots=True)
class HourlyMicroSessionConfig:
    freeze_seconds: int = 90

    def __post_init__(self) -> None:
        if not 60 <= self.freeze_seconds <= 90:
            msg = "freeze_seconds must be in the 60-90 seconds range"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MicroSessionState:
    run_id: UUID
    micro_session_id: str
    calendar_date: date
    trading_date: date
    session_type: SessionType
    session_phase: SessionPhase
    broker_trading_status: str
    started_at: datetime
    planned_start_at: datetime
    planned_end_at: datetime
    freeze_starts_at: datetime
    frozen: bool = False

    def event_context(self) -> SessionEventContext:
        return SessionEventContext(
            calendar_date=self.calendar_date,
            trading_date=self.trading_date,
            session_type=self.session_type,
            session_phase=self.session_phase,
            micro_session_id=self.micro_session_id,
            broker_trading_status=self.broker_trading_status,
        )

    def as_read_model(self) -> JsonObject:
        return {
            "run_id": str(self.run_id),
            "micro_session_id": self.micro_session_id,
            "calendar_date": self.calendar_date.isoformat(),
            "trading_date": self.trading_date.isoformat(),
            "session_type": self.session_type.value,
            "session_phase": self.session_phase.value,
            "broker_trading_status": self.broker_trading_status,
            "started_at": self.started_at.isoformat(),
            "planned_start_at": self.planned_start_at.isoformat(),
            "planned_end_at": self.planned_end_at.isoformat(),
            "freeze_starts_at": self.freeze_starts_at.isoformat(),
            "frozen": self.frozen,
        }


@dataclass(frozen=True, slots=True)
class MicroSessionEvent:
    event_type: str
    micro_session_id: str
    run_id: UUID
    observed_at: datetime
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class MicroSessionTickResult:
    snapshot: SessionSnapshot
    active_state: MicroSessionState | None
    events: tuple[MicroSessionEvent, ...]


class HourlyMicroSessionManager:
    """Roll logical hourly sessions without restarting trade-core."""

    def __init__(
        self,
        *,
        store: SessionStateStore,
        config: HourlyMicroSessionConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or HourlyMicroSessionConfig()
        self._current: MicroSessionState | None = None

    @property
    def current_state(self) -> MicroSessionState | None:
        return self._current

    def on_snapshot(self, snapshot: SessionSnapshot) -> MicroSessionTickResult:
        events: list[MicroSessionEvent] = []

        if self._current is not None:
            if self._should_close_current(snapshot):
                events.extend(self._close_current(snapshot))
            else:
                events.extend(self._maybe_freeze(snapshot))

        if self._current is None and self._can_open(snapshot):
            events.append(self._open(snapshot))
            events.extend(self._maybe_freeze(snapshot))

        active_micro_session_id = self._current.micro_session_id if self._current else None
        return MicroSessionTickResult(
            snapshot=snapshot.with_micro_session(active_micro_session_id),
            active_state=self._current,
            events=tuple(events),
        )

    def _can_open(self, snapshot: SessionSnapshot) -> bool:
        return (
            snapshot.is_trading_allowed
            and snapshot.session_phase == SessionPhase.CONTINUOUS_TRADING
            and snapshot.schedule_window_end_at is not None
            and snapshot.observed_at < snapshot.schedule_window_end_at
        )

    def _open(self, snapshot: SessionSnapshot) -> MicroSessionEvent:
        if snapshot.schedule_window_start_at is None or snapshot.schedule_window_end_at is None:
            msg = "Cannot open micro-session without an active schedule window"
            raise RuntimeError(msg)
        planned_start_at = max(floor_hour(snapshot.observed_at), snapshot.schedule_window_start_at)
        planned_end_at = min(next_hour(snapshot.observed_at), snapshot.schedule_window_end_at)
        freeze_starts_at = planned_end_at - timedelta(seconds=self._config.freeze_seconds)
        micro_session_id = micro_session_id_for(snapshot, snapshot.observed_at)
        context = snapshot.event_context(micro_session_id)

        run_id = self._store.open_micro_session(
            context=context,
            observed_at=snapshot.observed_at,
            planned_start_at=planned_start_at,
            planned_end_at=planned_end_at,
            freeze_starts_at=freeze_starts_at,
            source_snapshot=snapshot.as_read_model(),
        )
        self._current = MicroSessionState(
            run_id=run_id,
            micro_session_id=micro_session_id,
            calendar_date=snapshot.calendar_date,
            trading_date=snapshot.trading_date,
            session_type=snapshot.session_type,
            session_phase=snapshot.session_phase,
            broker_trading_status=snapshot.broker_trading_status,
            started_at=snapshot.observed_at,
            planned_start_at=planned_start_at,
            planned_end_at=planned_end_at,
            freeze_starts_at=freeze_starts_at,
            frozen=False,
        )
        return self._event(
            "session_run_opened",
            snapshot.observed_at,
            {"state": self._current.as_read_model()},
        )

    def _maybe_freeze(self, snapshot: SessionSnapshot) -> tuple[MicroSessionEvent, ...]:
        if self._current is None or self._current.frozen:
            return ()
        if snapshot.observed_at < self._current.freeze_starts_at:
            return ()

        context = self._current.event_context()
        self._store.mark_freeze(
            context=context,
            run_id=self._current.run_id,
            freeze_started_at=snapshot.observed_at,
        )
        self._current = replace(self._current, frozen=True)
        return (
            self._event(
                "freeze_new_entries",
                snapshot.observed_at,
                {"state": self._current.as_read_model()},
            ),
        )

    def _should_close_current(self, snapshot: SessionSnapshot) -> bool:
        if self._current is None:
            return False
        if snapshot.observed_at >= self._current.planned_end_at:
            return True
        if (
            snapshot.session_phase != SessionPhase.CONTINUOUS_TRADING
            or not snapshot.is_trading_allowed
        ):
            return True
        if snapshot.session_type != self._current.session_type:
            return True
        return snapshot.trading_date != self._current.trading_date

    def _close_current(self, snapshot: SessionSnapshot) -> tuple[MicroSessionEvent, ...]:
        if self._current is None:
            return ()

        current = self._current
        context = current.event_context()
        reason_code = self._close_reason(snapshot, current)
        snapshot_payload: JsonObject = {
            "closing_snapshot": snapshot.as_read_model(),
            "closed_state": current.as_read_model(),
        }

        self._store.record_snapshot(
            context=context,
            run_id=current.run_id,
            observed_at=snapshot.observed_at,
            reason_code=reason_code,
            snapshot_payload=snapshot_payload,
        )
        self._store.close_micro_session(
            context=context,
            run_id=current.run_id,
            ended_at=snapshot.observed_at,
            close_reason_code=reason_code,
        )
        report_payload: JsonObject = {
            "report_type": "hourly",
            "micro_session_id": current.micro_session_id,
            "trading_date": str(current.trading_date),
            "session_type": current.session_type.value,
            "reason_code": reason_code,
        }
        self._store.request_report(
            context=context,
            run_id=current.run_id,
            requested_at=snapshot.observed_at,
            report_payload=report_payload,
        )

        self._current = None
        return (
            MicroSessionEvent(
                event_type="snapshot_taken",
                micro_session_id=current.micro_session_id,
                run_id=current.run_id,
                observed_at=snapshot.observed_at,
                payload=snapshot_payload,
            ),
            MicroSessionEvent(
                event_type="session_run_closed",
                micro_session_id=current.micro_session_id,
                run_id=current.run_id,
                observed_at=snapshot.observed_at,
                payload={"reason_code": reason_code},
            ),
            MicroSessionEvent(
                event_type="report_requested",
                micro_session_id=current.micro_session_id,
                run_id=current.run_id,
                observed_at=snapshot.observed_at,
                payload=report_payload,
            ),
        )

    @staticmethod
    def _close_reason(snapshot: SessionSnapshot, current: MicroSessionState) -> str:
        if (
            snapshot.session_type != current.session_type
            or snapshot.trading_date != current.trading_date
            or snapshot.session_phase != SessionPhase.CONTINUOUS_TRADING
            or not snapshot.is_trading_allowed
        ):
            return EXCHANGE_SESSION_BOUNDARY
        return HOURLY_ROLLOVER

    def _event(
        self,
        event_type: str,
        observed_at: datetime,
        payload: JsonObject,
    ) -> MicroSessionEvent:
        if self._current is None:
            msg = "Cannot emit micro-session event without active state"
            raise RuntimeError(msg)
        return MicroSessionEvent(
            event_type=event_type,
            micro_session_id=self._current.micro_session_id,
            run_id=self._current.run_id,
            observed_at=observed_at,
            payload=payload,
        )

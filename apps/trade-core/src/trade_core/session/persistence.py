"""Persistence boundary for session_run and strategy_state_event rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from trade_core.session.models import JsonObject, SessionEventContext
from trading_common.db.models import SessionRun, StrategyStateEvent
from trading_common.db.repositories import SessionRunRepository, StrategyStateEventRepository


class SessionStateStore(Protocol):
    """Storage contract used by HourlyMicroSessionManager."""

    def open_micro_session(
        self,
        *,
        context: SessionEventContext,
        observed_at: datetime,
        planned_start_at: datetime,
        planned_end_at: datetime,
        freeze_starts_at: datetime,
        source_snapshot: Mapping[str, object],
    ) -> UUID: ...

    def mark_freeze(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        freeze_started_at: datetime,
    ) -> None: ...

    def record_snapshot(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        observed_at: datetime,
        reason_code: str,
        snapshot_payload: Mapping[str, object],
    ) -> None: ...

    def close_micro_session(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        ended_at: datetime,
        close_reason_code: str,
    ) -> None: ...

    def request_report(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        requested_at: datetime,
        report_payload: Mapping[str, object],
    ) -> None: ...


class SqlAlchemySessionStateStore:
    """SQLAlchemy-backed session state store."""

    def __init__(
        self,
        session: Session,
        *,
        strategy_id: str,
        strategy_version: int,
    ) -> None:
        self._session_runs = SessionRunRepository(session)
        self._state_events = StrategyStateEventRepository(session)
        self._strategy_id = strategy_id
        self._strategy_version = strategy_version

    def open_micro_session(
        self,
        *,
        context: SessionEventContext,
        observed_at: datetime,
        planned_start_at: datetime,
        planned_end_at: datetime,
        freeze_starts_at: datetime,
        source_snapshot: Mapping[str, object],
    ) -> UUID:
        run = self._session_runs.create(
            SessionRun(
                **context.as_db_values(),
                strategy_id=self._strategy_id,
                strategy_version=self._strategy_version,
                status="open",
                started_at=observed_at,
                ended_at=None,
                freeze_started_at=None,
                report_requested_at=None,
                close_reason_code=None,
                run_payload={
                    "planned_start_at": planned_start_at.isoformat(),
                    "planned_end_at": planned_end_at.isoformat(),
                    "freeze_starts_at": freeze_starts_at.isoformat(),
                    "source_snapshot": dict(source_snapshot),
                },
            )
        )
        self._record_state_event(
            context=context,
            observed_at=observed_at,
            event_type="session_run_opened",
            previous_state=None,
            new_state="open",
            reason_code=None,
            payload={"run_id": str(run.run_id)},
        )
        return run.run_id

    def mark_freeze(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        freeze_started_at: datetime,
    ) -> None:
        self._session_runs.mark_freeze(run_id, freeze_started_at=freeze_started_at)
        self._record_state_event(
            context=context,
            observed_at=freeze_started_at,
            event_type="freeze_new_entries",
            previous_state="open",
            new_state="freezing",
            reason_code="hour_boundary_freeze",
            payload={"run_id": str(run_id)},
        )

    def record_snapshot(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        observed_at: datetime,
        reason_code: str,
        snapshot_payload: Mapping[str, object],
    ) -> None:
        self._record_state_event(
            context=context,
            observed_at=observed_at,
            event_type="snapshot_taken",
            previous_state="freezing",
            new_state="snapshot_taken",
            reason_code=reason_code,
            payload={"run_id": str(run_id), "snapshot": dict(snapshot_payload)},
        )

    def close_micro_session(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        ended_at: datetime,
        close_reason_code: str,
    ) -> None:
        self._session_runs.close(
            run_id,
            ended_at=ended_at,
            close_reason_code=close_reason_code,
        )
        self._record_state_event(
            context=context,
            observed_at=ended_at,
            event_type="session_run_closed",
            previous_state="snapshot_taken",
            new_state="closed",
            reason_code=close_reason_code,
            payload={"run_id": str(run_id)},
        )

    def request_report(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        requested_at: datetime,
        report_payload: Mapping[str, object],
    ) -> None:
        self._session_runs.request_report(run_id, requested_at=requested_at)
        self._record_state_event(
            context=context,
            observed_at=requested_at,
            event_type="report_requested",
            previous_state="closed",
            new_state="report_requested",
            reason_code=None,
            payload={"run_id": str(run_id), "report": dict(report_payload)},
        )

    def _record_state_event(
        self,
        *,
        context: SessionEventContext,
        observed_at: datetime,
        event_type: str,
        previous_state: str | None,
        new_state: str,
        reason_code: str | None,
        payload: Mapping[str, object],
    ) -> None:
        self._state_events.create(
            StrategyStateEvent(
                **context.as_db_values(),
                ts_utc=observed_at,
                exchange_ts=observed_at,
                received_ts=observed_at,
                strategy_id=self._strategy_id,
                strategy_version=self._strategy_version,
                instrument_id=None,
                previous_state=previous_state,
                new_state=new_state,
                event_type=event_type,
                reason_code=reason_code,
                state_payload=dict(payload),
            )
        )


@dataclass(slots=True)
class InMemorySessionStateStore:
    """Small test/dry-run store that implements SessionStateStore."""

    opened_runs: dict[UUID, JsonObject] = field(default_factory=dict)
    state_events: list[JsonObject] = field(default_factory=list)

    def open_micro_session(
        self,
        *,
        context: SessionEventContext,
        observed_at: datetime,
        planned_start_at: datetime,
        planned_end_at: datetime,
        freeze_starts_at: datetime,
        source_snapshot: Mapping[str, object],
    ) -> UUID:
        run_id = uuid4()
        self.opened_runs[run_id] = {
            **context.as_read_model(),
            "observed_at": observed_at.isoformat(),
            "planned_start_at": planned_start_at.isoformat(),
            "planned_end_at": planned_end_at.isoformat(),
            "freeze_starts_at": freeze_starts_at.isoformat(),
            "source_snapshot": dict(source_snapshot),
        }
        self._event(context, run_id, observed_at, "session_run_opened", None)
        return run_id

    def mark_freeze(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        freeze_started_at: datetime,
    ) -> None:
        self._event(
            context,
            run_id,
            freeze_started_at,
            "freeze_new_entries",
            "hour_boundary_freeze",
        )

    def record_snapshot(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        observed_at: datetime,
        reason_code: str,
        snapshot_payload: Mapping[str, object],
    ) -> None:
        self._event(context, run_id, observed_at, "snapshot_taken", reason_code)
        self.state_events[-1]["snapshot"] = dict(snapshot_payload)

    def close_micro_session(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        ended_at: datetime,
        close_reason_code: str,
    ) -> None:
        self._event(context, run_id, ended_at, "session_run_closed", close_reason_code)

    def request_report(
        self,
        *,
        context: SessionEventContext,
        run_id: UUID,
        requested_at: datetime,
        report_payload: Mapping[str, object],
    ) -> None:
        self._event(context, run_id, requested_at, "report_requested", None)
        self.state_events[-1]["report"] = dict(report_payload)

    def _event(
        self,
        context: SessionEventContext,
        run_id: UUID,
        observed_at: datetime,
        event_type: str,
        reason_code: str | None,
    ) -> None:
        self.state_events.append(
            {
                **context.as_read_model(),
                "run_id": str(run_id),
                "observed_at": observed_at.isoformat(),
                "event_type": event_type,
                "reason_code": reason_code,
            }
        )

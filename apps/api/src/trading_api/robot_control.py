"""Database-backed robot control plane for operator commands."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from trading_api.auth import AuthContext
from trading_api.schemas import RobotCommand, RobotCommandResponse
from trading_common import ServiceName
from trading_common.db.models import AuditEvent
from trading_common.db.repositories import RobotCommandRepository
from trading_common.db.service import DatabaseService
from trading_common.enums import SessionPhase, SessionType


class RobotControlService:
    """Persist operator commands for trade-core to consume asynchronously."""

    def __init__(self, database: DatabaseService) -> None:
        self._database = database

    def request(
        self,
        *,
        command: RobotCommand,
        auth: AuthContext,
        payload: Mapping[str, object] | None = None,
    ) -> RobotCommandResponse:
        requested_at = datetime.now(tz=UTC)
        with self._database.session_scope() as session:
            repository = RobotCommandRepository(session)
            row = repository.create(
                command_type=command.value,
                requested_by=auth.subject,
                requested_role=auth.role.value,
                requested_at=requested_at,
                payload=payload,
                reason_code="operator_requested",
            )
            session.add(
                _audit_event(
                    action=f"robot_command_{command.value}_requested",
                    actor=auth.subject,
                    command_id=str(row.command_id),
                    payload={
                        "command_type": command.value,
                        "requested_role": auth.role.value,
                        "auth_mode": auth.auth_mode,
                        "payload": dict(payload or {}),
                    },
                    ts_utc=requested_at,
                )
            )
            return RobotCommandResponse(
                accepted=True,
                command_id=row.command_id,
                command=command,
                requested_by_role=auth.role,
                requested_by=auth.subject,
                requested_at=row.requested_at,
                status=row.status,
                reason_code=row.reason_code,
                payload=dict(row.payload),
                message=f"Robot command {command.value} persisted for trade-core",
            )

    def start(self, *, auth: AuthContext) -> RobotCommandResponse:
        return self.request(command=RobotCommand.START, auth=auth)

    def stop(self, *, auth: AuthContext) -> RobotCommandResponse:
        return self.request(command=RobotCommand.STOP, auth=auth)

    def pause(self, *, auth: AuthContext) -> RobotCommandResponse:
        return self.request(command=RobotCommand.PAUSE, auth=auth)

    def resume(self, *, auth: AuthContext) -> RobotCommandResponse:
        return self.request(command=RobotCommand.RESUME, auth=auth)

    def emergency_stop(self, *, auth: AuthContext) -> RobotCommandResponse:
        return self.request(
            command=RobotCommand.EMERGENCY_STOP,
            auth=auth,
            payload={"cancel_reason_code": "manual_operator_emergency_stop"},
        )

    def current_state(self) -> str:
        with self._database.session_scope() as session:
            repository = RobotCommandRepository(session)
            return repository.robot_state_from_command(repository.latest())


def _audit_event(
    *,
    action: str,
    actor: str,
    command_id: str,
    payload: Mapping[str, object],
    ts_utc: datetime,
) -> AuditEvent:
    today = ts_utc.date()
    return AuditEvent(
        calendar_date=today,
        trading_date=today,
        session_type=SessionType.WEEKEND.value,
        session_phase=SessionPhase.CLOSED.value,
        micro_session_id="operator-control",
        broker_trading_status="unknown",
        ts_utc=ts_utc,
        exchange_ts=ts_utc,
        received_ts=ts_utc,
        service=ServiceName.API.value,
        actor=actor,
        action=action,
        entity_type="robot_command",
        entity_id=command_id,
        severity="info",
        correlation_id=command_id,
        audit_payload=dict(payload),
    )

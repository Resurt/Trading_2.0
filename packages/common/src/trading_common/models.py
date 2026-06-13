"""Small shared dataclasses used by initial service skeletons."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from trading_common.enums import RuntimeMode, ServiceName, SessionPhase, SessionType


@dataclass(frozen=True, slots=True)
class AppIdentity:
    """Stable identity for a service process."""

    service: ServiceName
    version: str
    runtime_mode: RuntimeMode


@dataclass(frozen=True, slots=True)
class TradingContext:
    """Session context that should travel with domain events and logs."""

    run_id: UUID
    ts_utc: datetime
    calendar_date: date
    trading_date: date
    session_type: SessionType
    session_phase: SessionPhase
    micro_session_id: str
    broker_trading_status: str


class HealthStatus(StrEnum):
    """Generic health status values."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    """Minimal health payload for smoke checks and future health endpoints."""

    identity: AppIdentity
    status: HealthStatus
    detail: str = "service skeleton is importable"

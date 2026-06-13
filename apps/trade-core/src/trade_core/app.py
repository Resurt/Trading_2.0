"""Initial trade-core application boundary.

No broker integration or trading business logic lives here yet. This file only
declares an importable service skeleton for the monorepo bootstrap step.
"""

from trading_common import AppIdentity, RuntimeMode, ServiceHealth, ServiceName
from trading_common.models import HealthStatus


def runtime_mode_from_env(value: str | None) -> RuntimeMode:
    """Parse runtime mode for local service startup."""

    if value is None:
        return RuntimeMode.HISTORICAL_REPLAY
    return RuntimeMode(value)


def create_identity(runtime_mode: RuntimeMode = RuntimeMode.HISTORICAL_REPLAY) -> AppIdentity:
    """Return the service identity used by health checks and logs."""

    return AppIdentity(
        service=ServiceName.TRADE_CORE,
        version="0.1.0",
        runtime_mode=runtime_mode,
    )


def health() -> ServiceHealth:
    """Return a placeholder health payload."""

    return ServiceHealth(identity=create_identity(), status=HealthStatus.OK)

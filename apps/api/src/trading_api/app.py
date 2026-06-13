"""Initial API application boundary.

FastAPI routes will be added in a later step. For now this module stays
dependency-light so smoke tests can validate package wiring without services.
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
        service=ServiceName.API,
        version="0.1.0",
        runtime_mode=runtime_mode,
    )


def health() -> ServiceHealth:
    """Return a placeholder health payload."""

    return ServiceHealth(identity=create_identity(), status=HealthStatus.OK)

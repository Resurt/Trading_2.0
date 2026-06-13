"""Initial report-worker application boundary.

Celery tasks are intentionally deferred to the reporting step. This module only
provides an importable service skeleton and health payload.
"""

from trading_common import AppIdentity, RuntimeMode, ServiceHealth, ServiceName
from trading_common.models import HealthStatus


def create_identity(runtime_mode: RuntimeMode = RuntimeMode.HISTORICAL_REPLAY) -> AppIdentity:
    """Return the service identity used by health checks and logs."""

    return AppIdentity(
        service=ServiceName.REPORT_WORKER,
        version="0.1.0",
        runtime_mode=runtime_mode,
    )


def health() -> ServiceHealth:
    """Return a placeholder health payload."""

    return ServiceHealth(identity=create_identity(), status=HealthStatus.OK)

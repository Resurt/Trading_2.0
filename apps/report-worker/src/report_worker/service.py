"""HTTP entrypoint for the local report-worker container."""

from __future__ import annotations

from report_worker.app import create_identity
from trading_common import LaunchModePolicy
from trading_common.http_health import run_health_server
from trading_common.models import HealthStatus, ServiceHealth
from trading_common.observability import configure_json_logging


def main() -> None:
    launch_policy = LaunchModePolicy.from_env()
    runtime_mode = launch_policy.mode
    identity = create_identity(runtime_mode)
    configure_json_logging(service=identity.service)
    run_health_server(
        ServiceHealth(
            identity=identity,
            status=HealthStatus.OK,
            detail="report-worker health server is running; Celery tasks are available",
        )
    )


if __name__ == "__main__":
    main()

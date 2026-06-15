"""HTTP health/metrics entrypoint for the report-worker sidecar container."""

from __future__ import annotations

from report_worker.app import create_identity
from report_worker.metrics import REPORT_WORKER_METRICS, sample_report_worker_metrics
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
            detail="report-worker-health is running; Celery worker metrics are sampled from Redis",
        ),
        metrics=REPORT_WORKER_METRICS,
        metrics_sampler=sample_report_worker_metrics,
    )


if __name__ == "__main__":
    main()

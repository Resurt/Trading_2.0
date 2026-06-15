"""HTTP entrypoint for the local trade-core container."""

from __future__ import annotations

from trade_core.app import create_identity
from trade_core.runtime import TradeCoreRuntime
from trading_common import LaunchModePolicy
from trading_common.http_health import run_health_server
from trading_common.models import HealthStatus, ServiceHealth
from trading_common.observability import configure_json_logging


def main() -> None:
    launch_policy = LaunchModePolicy.from_env()
    runtime_mode = launch_policy.mode
    identity = create_identity(runtime_mode)
    configure_json_logging(service=identity.service)
    runtime = TradeCoreRuntime(launch_policy=launch_policy)
    runtime.start_background()
    try:
        run_health_server(
            ServiceHealth(
                identity=identity,
                status=HealthStatus.OK,
                detail=(
                    "trade-core runtime is running; "
                    f"mode={launch_policy.mode.value}; "
                    f"real_orders={launch_policy.allows_real_orders}"
                ),
            ),
            metrics=runtime.metrics,
            metrics_sampler=runtime.sample_metrics,
        )
    finally:
        runtime.request_stop()


if __name__ == "__main__":
    main()

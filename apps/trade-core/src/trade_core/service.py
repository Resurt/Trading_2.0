"""HTTP entrypoint for the local trade-core container."""

from __future__ import annotations

import os

from trade_core.app import create_identity, runtime_mode_from_env
from trading_common.http_health import run_health_server
from trading_common.models import HealthStatus, ServiceHealth


def main() -> None:
    runtime_mode = runtime_mode_from_env(os.getenv("TRADING_RUNTIME_MODE"))
    identity = create_identity(runtime_mode)
    run_health_server(
        ServiceHealth(
            identity=identity,
            status=HealthStatus.OK,
            detail="trade-core skeleton is running; broker integration is not enabled yet",
        )
    )


if __name__ == "__main__":
    main()

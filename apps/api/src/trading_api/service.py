"""Uvicorn entrypoint for the FastAPI BFF container."""

from __future__ import annotations

import os

import uvicorn

from trading_api.app import create_fastapi_app, create_identity, runtime_mode_from_env
from trading_common.observability import configure_json_logging


def main() -> None:
    runtime_mode = runtime_mode_from_env(os.getenv("TRADING_RUNTIME_MODE"))
    identity = create_identity(runtime_mode)
    configure_json_logging(service=identity.service)
    uvicorn.run(
        create_fastapi_app(runtime_mode=runtime_mode),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_config=None,
    )


if __name__ == "__main__":
    main()

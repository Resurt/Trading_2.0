"""Health probe for the Celery report-worker process."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from report_worker.celery_app import celery_app


def ping_worker(*, timeout_seconds: float = 5.0) -> list[dict[str, Any]]:
    """Return Celery ping responses for report-worker health checks."""

    destination_raw = os.getenv("CELERY_WORKER_HEALTH_DESTINATION")
    destination = [destination_raw] if destination_raw else None
    responses = celery_app.control.ping(
        destination=destination,
        timeout=timeout_seconds,
    )
    return list(responses or [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Check that Celery worker accepts control pings.")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    responses = ping_worker(timeout_seconds=args.timeout)
    ok = bool(responses)
    print(
        json.dumps(
            {"ok": ok, "responses": responses},
            ensure_ascii=False,
            default=str,
        )
    )
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

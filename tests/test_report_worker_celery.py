from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from report_worker import worker_health
from report_worker.celery_app import REPORTS_QUEUE, create_celery_app


def test_celery_app_routes_report_tasks_to_reports_queue() -> None:
    app = create_celery_app(
        broker_url="memory://",
        result_backend="cache+memory://",
    )

    assert app.conf.task_default_queue == REPORTS_QUEUE
    assert app.conf.task_routes == {
        "report_worker.*": {
            "queue": REPORTS_QUEUE,
        }
    }


def test_worker_health_reports_celery_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    class Control:
        def ping(
            self,
            *,
            destination: list[str] | None,
            timeout: float,
        ) -> list[dict[str, object]]:
            assert destination is None
            assert timeout == 2.5
            return [{"report-worker@example": {"ok": "pong"}}]

    monkeypatch.setattr(worker_health, "celery_app", SimpleNamespace(control=Control()))

    assert worker_health.ping_worker(timeout_seconds=2.5) == [
        {"report-worker@example": {"ok": "pong"}}
    ]


def test_worker_health_supports_destination_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Control:
        def ping(
            self,
            *,
            destination: list[str] | None,
            timeout: float,
        ) -> list[dict[str, object]]:
            seen["destination"] = destination
            seen["timeout"] = timeout
            return []

    monkeypatch.setenv("CELERY_WORKER_HEALTH_DESTINATION", "report-worker@host")
    monkeypatch.setattr(worker_health, "celery_app", SimpleNamespace(control=Control()))

    assert worker_health.ping_worker(timeout_seconds=1.0) == []
    assert seen == {"destination": ["report-worker@host"], "timeout": 1.0}

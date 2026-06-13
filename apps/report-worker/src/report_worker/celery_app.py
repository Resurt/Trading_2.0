"""Celery application for report-worker tasks."""

from __future__ import annotations

import os

from celery import Celery


def create_celery_app(
    *,
    broker_url: str | None = None,
    result_backend: str | None = None,
) -> Celery:
    broker = broker_url or os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    backend = result_backend or os.getenv("CELERY_RESULT_BACKEND", broker)
    app = Celery(
        "report_worker",
        broker=broker,
        backend=backend,
        include=("report_worker.tasks",),
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=("json",),
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        broker_connection_retry_on_startup=True,
    )
    return app


celery_app = create_celery_app()

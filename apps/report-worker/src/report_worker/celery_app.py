"""Celery application for report-worker tasks."""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

REPORTS_QUEUE = "reports"


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
        task_default_queue=os.getenv("CELERY_DEFAULT_QUEUE", REPORTS_QUEUE),
        task_routes={
            "report_worker.*": {
                "queue": os.getenv("CELERY_REPORTS_QUEUE", REPORTS_QUEUE),
            }
        },
    )
    beat_schedule = _diagnostic_beat_schedule()
    if beat_schedule:
        app.conf.beat_schedule = beat_schedule
    return app

def _diagnostic_beat_schedule() -> dict[str, dict[str, object]]:
    schedule: dict[str, dict[str, object]] = {}
    if _env_enabled("INTRADAY_ANALYTICS_DAILY_ENABLED"):
        schedule["daily-intraday-analytics-rebuild"] = {
            "task": "report_worker.run_daily_intraday_analytics_rebuild",
            "schedule": crontab(hour=18, minute=30),
        }
    if _env_enabled("CALIBRATION_OBSERVATORY_DAILY_ENABLED"):
        schedule["daily-calibration-diagnostics"] = {
            "task": "report_worker.run_daily_calibration_diagnostics",
            "schedule": crontab(hour=19, minute=0),
        }
    if _env_enabled("CALIBRATION_OBSERVATORY_WEEKLY_ENABLED"):
        schedule["weekly-calibration-observatory"] = {
            "task": "report_worker.run_weekly_calibration_observatory",
            "schedule": crontab(hour=19, minute=30, day_of_week="sun"),
        }
    return schedule


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


celery_app = create_celery_app()

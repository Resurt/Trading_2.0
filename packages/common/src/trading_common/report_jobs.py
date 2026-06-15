"""Report job outbox dispatch helpers shared by trade-core, API and worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from trading_common.db.models import ReportJobOutbox
from trading_common.db.repositories import (
    DAILY_REBUILD_TASK,
    HOURLY_REPORT_TASK,
    ReportJobRepository,
)

REPORTS_QUEUE = "reports"


class CeleryLikeApp(Protocol):
    def send_task(
        self,
        name: str,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        queue: str | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class ReportJobDispatcher:
    """Dispatch pending report outbox rows to Celery after DB commit."""

    celery_app: CeleryLikeApp
    queue: str = REPORTS_QUEUE
    retry_delay_seconds: int = 60

    def dispatch_pending(
        self,
        session: Session,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[ReportJobOutbox]:
        observed_at = now or datetime.now(tz=UTC)
        repository = ReportJobRepository(session)
        dispatched: list[ReportJobOutbox] = []
        for job in repository.list_dispatchable(now=observed_at, limit=limit):
            try:
                async_result = self.celery_app.send_task(
                    job.task_name,
                    args=task_args_for_job(job),
                    queue=self.queue,
                )
                repository.mark_enqueued(job, celery_task_id=str(async_result.id))
                dispatched.append(job)
            except Exception as exc:
                repository.mark_failed(
                    job,
                    failed_at=observed_at,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_delay_seconds=self.retry_delay_seconds,
                )
        session.flush()
        return dispatched


def task_args_for_job(job: ReportJobOutbox) -> tuple[object, ...]:
    """Build canonical Celery task args from a persisted job row."""

    force_rebuild = bool(job.job_payload.get("force_rebuild", True))
    if job.task_name == HOURLY_REPORT_TASK:
        if job.micro_session_id is None:
            msg = "micro_session_id is required for hourly report job"
            raise ValueError(msg)
        return (job.micro_session_id, job.strategy_id, force_rebuild)
    if job.task_name == DAILY_REBUILD_TASK:
        if job.trading_date is None:
            msg = "trading_date is required for daily rebuild job"
            raise ValueError(msg)
        return (
            job.trading_date.isoformat(),
            job.strategy_id,
            bool(job.job_payload.get("include_counterfactual", True)),
            _optional_str(job.job_payload.get("instrument_id")),
            _optional_str(job.job_payload.get("timeframe")),
            _optional_str(job.job_payload.get("session_type")),
            _optional_int(job.job_payload.get("strategy_version")),
            force_rebuild,
        )
    msg = f"Unsupported report task: {job.task_name}"
    raise ValueError(msg)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None

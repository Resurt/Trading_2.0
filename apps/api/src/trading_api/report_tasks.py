"""Celery client wrapper used by FastAPI report endpoints."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from celery import Celery

from trading_api.schemas import (
    DailyReportRunRequest,
    ReportJobResponse,
    ReportJobStatusResponse,
    ReportRebuildRequest,
    ReportScope,
)
from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import ReportJobOutbox
from trading_common.db.repositories import (
    DAILY_REBUILD_TASK,
    HOURLY_REPORT_TASK,
    ReportJobRepository,
)
from trading_common.db.service import DatabaseService
from trading_common.report_jobs import REPORTS_QUEUE, ReportJobDispatcher


class ReportTaskClient(Protocol):
    def enqueue_daily_report(self, request: DailyReportRunRequest) -> ReportJobResponse: ...

    def enqueue_report_rebuild(self, request: ReportRebuildRequest) -> ReportJobResponse: ...

    def job_status(self, job_id: str) -> ReportJobStatusResponse: ...


@dataclass(slots=True)
class CeleryReportTaskClient:
    app: Celery
    database: DatabaseService | None = None
    dispatcher: ReportJobDispatcher | None = None

    @classmethod
    def from_env(cls, *, database: DatabaseService | None = None) -> CeleryReportTaskClient:
        broker = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
        backend = os.getenv("CELERY_RESULT_BACKEND", broker)
        queue = os.getenv("CELERY_REPORTS_QUEUE", REPORTS_QUEUE)
        app = Celery("trading_api_report_client", broker=broker, backend=backend)
        return cls(app, database=database, dispatcher=ReportJobDispatcher(app, queue=queue))

    def enqueue_daily_report(self, request: DailyReportRunRequest) -> ReportJobResponse:
        rebuild_request = ReportRebuildRequest(
            scope=ReportScope.DAILY,
            trading_date=request.trading_date,
            strategy_id=request.strategy_id,
            include_counterfactual=request.include_counterfactual,
        )
        return self.enqueue_report_rebuild(rebuild_request)

    def enqueue_report_rebuild(self, request: ReportRebuildRequest) -> ReportJobResponse:
        if request.scope == ReportScope.HOURLY:
            if request.micro_session_id is None:
                msg = "micro_session_id is required for hourly report rebuild"
                raise ValueError(msg)
            return self._create_and_dispatch_hourly_rebuild(request)
        return self._create_and_dispatch_daily_rebuild(request)

    def job_status(self, job_id: str) -> ReportJobStatusResponse:
        database = self._database()
        with database.session_scope() as session:
            job = ReportJobRepository(session).get(job_id)
            if job is None:
                return ReportJobStatusResponse(
                    job_id=job_id,
                    task_name="unknown",
                    status="not_found",
                    ready=True,
                    successful=False,
                    failed=True,
                    result=None,
                    error="report job not found",
                    payload={},
                )
            return _job_status_response(job)

    def _create_and_dispatch_daily_rebuild(
        self,
        request: ReportRebuildRequest,
    ) -> ReportJobResponse:
        job = self._create_job(
            report_type="daily_rebuild",
            task_name=DAILY_REBUILD_TASK,
            strategy_id=request.strategy_id,
            trading_date=request.trading_date,
            micro_session_id=None,
            force_rebuild=request.force_rebuild,
            job_payload={
                "trading_date": request.trading_date.isoformat(),
                "instrument_id": request.instrument_id,
                "timeframe": request.timeframe,
                "session_type": request.session_type,
                "strategy_version": request.strategy_version,
                "include_counterfactual": request.include_counterfactual,
            },
        )
        return _job_response(job)

    def _create_and_dispatch_hourly_rebuild(
        self,
        request: ReportRebuildRequest,
    ) -> ReportJobResponse:
        if request.micro_session_id is None:
            msg = "micro_session_id is required for hourly report rebuild"
            raise ValueError(msg)
        job = self._create_job(
            report_type="hourly",
            task_name=HOURLY_REPORT_TASK,
            strategy_id=request.strategy_id,
            trading_date=request.trading_date,
            micro_session_id=request.micro_session_id,
            force_rebuild=request.force_rebuild,
            job_payload={
                "trading_date": request.trading_date.isoformat(),
                "scope": request.scope.value,
            },
            idempotency_key=(
                ReportJobRepository.hourly_idempotency_key(
                    micro_session_id=request.micro_session_id,
                    strategy_id=request.strategy_id,
                )
            ),
        )
        return _job_response(job)

    def _create_job(
        self,
        *,
        report_type: str,
        task_name: str,
        strategy_id: str,
        trading_date: date | None,
        micro_session_id: str | None,
        force_rebuild: bool,
        job_payload: dict[str, object],
        idempotency_key: str | None = None,
    ) -> ReportJobOutbox:
        database = self._database()
        requested_at = datetime.now(tz=UTC)
        with database.session_scope() as session:
            job = ReportJobRepository(session).create_job_idempotent(
                report_type=report_type,
                task_name=task_name,
                strategy_id=strategy_id,
                trading_date=trading_date,
                micro_session_id=micro_session_id,
                requested_at=requested_at,
                force_rebuild=force_rebuild,
                job_payload=job_payload,
                idempotency_key=idempotency_key,
            )
            report_job_id = job.report_job_id
        with database.session_scope() as session:
            repository = ReportJobRepository(session)
            queued_job = repository.get(report_job_id)
            if queued_job is None:
                msg = f"Report job vanished after commit: {report_job_id}"
                raise LookupError(msg)
            self._dispatcher().dispatch_pending(session, now=requested_at, limit=50)
            return queued_job

    def _database(self) -> DatabaseService:
        if self.database is None:
            self.database = DatabaseService(build_database_url_from_env())
        return self.database

    def _dispatcher(self) -> ReportJobDispatcher:
        if self.dispatcher is None:
            self.dispatcher = ReportJobDispatcher(self.app)
        return self.dispatcher


def _job_response(job: ReportJobOutbox) -> ReportJobResponse:
    return ReportJobResponse(
        job_id=str(job.report_job_id),
        task_name=job.task_name,
        status=job.status,
        payload=_job_payload(job),
    )


def _job_status_response(job: ReportJobOutbox) -> ReportJobStatusResponse:
    successful = job.status == "succeeded"
    failed = job.status == "dead_letter"
    return ReportJobStatusResponse(
        job_id=str(job.report_job_id),
        task_name=job.task_name,
        status=job.status,
        ready=successful or failed,
        successful=successful,
        failed=failed,
        result=dict(job.result_payload) if successful else None,
        error=job.last_error if failed or job.status == "retry" else None,
        payload=_job_payload(job),
    )


def _job_payload(job: ReportJobOutbox) -> dict[str, Any]:
    return {
        **dict(job.job_payload),
        "report_job_id": str(job.report_job_id),
        "celery_task_id": job.celery_task_id,
        "report_type": job.report_type,
        "micro_session_id": job.micro_session_id,
        "strategy_id": job.strategy_id,
        "trading_date": job.trading_date.isoformat() if job.trading_date else None,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "last_error": job.last_error,
        "requested_at": job.requested_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
    }

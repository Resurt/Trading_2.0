"""Celery client wrapper used by FastAPI report endpoints."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from celery import Celery

from trading_api.schemas import (
    DailyReportRunRequest,
    ReportJobResponse,
    ReportJobStatusResponse,
    ReportRebuildRequest,
    ReportScope,
)


class ReportTaskClient(Protocol):
    def enqueue_daily_report(self, request: DailyReportRunRequest) -> ReportJobResponse: ...

    def enqueue_report_rebuild(self, request: ReportRebuildRequest) -> ReportJobResponse: ...

    def job_status(self, job_id: str) -> ReportJobStatusResponse: ...


@dataclass(slots=True)
class CeleryReportTaskClient:
    app: Celery

    @classmethod
    def from_env(cls) -> CeleryReportTaskClient:
        broker = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
        backend = os.getenv("CELERY_RESULT_BACKEND", broker)
        return cls(Celery("trading_api_report_client", broker=broker, backend=backend))

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
            return self._send_hourly_rebuild(request)
        return self._send_daily_rebuild(request)

    def job_status(self, job_id: str) -> ReportJobStatusResponse:
        async_result = self.app.AsyncResult(job_id)
        raw_result: Any = async_result.result if async_result.ready() else None
        failed = bool(async_result.failed())
        successful = bool(async_result.successful())
        return ReportJobStatusResponse(
            job_id=job_id,
            task_name=str(getattr(async_result, "task_name", None) or "unknown"),
            status=str(async_result.status).lower(),
            ready=bool(async_result.ready()),
            successful=successful,
            failed=failed,
            result=_json_payload(raw_result) if successful else None,
            error=str(raw_result) if failed and raw_result is not None else None,
            payload={"state": str(async_result.state)},
        )

    def _send_daily_rebuild(self, request: ReportRebuildRequest) -> ReportJobResponse:
        task_name = "report_worker.rebuild_reports_for_date"
        async_result = self.app.send_task(
            task_name,
            args=(
                request.trading_date.isoformat(),
                request.strategy_id,
                request.include_counterfactual,
                request.instrument_id,
                request.timeframe,
                request.session_type,
                request.strategy_version,
                request.force_rebuild,
            ),
        )
        return ReportJobResponse(
            job_id=str(async_result.id),
            task_name=task_name,
            status="queued",
            payload={
                "trading_date": request.trading_date.isoformat(),
                "strategy_id": request.strategy_id,
                "instrument_id": request.instrument_id,
                "timeframe": request.timeframe,
                "session_type": request.session_type,
                "strategy_version": request.strategy_version,
                "include_counterfactual": request.include_counterfactual,
                "force_rebuild": request.force_rebuild,
            },
        )

    def _send_hourly_rebuild(self, request: ReportRebuildRequest) -> ReportJobResponse:
        task_name = "report_worker.build_hourly_report"
        async_result = self.app.send_task(
            task_name,
            args=(
                request.micro_session_id,
                request.strategy_id,
                request.force_rebuild,
            ),
        )
        return ReportJobResponse(
            job_id=str(async_result.id),
            task_name=task_name,
            status="queued",
            payload={
                "micro_session_id": request.micro_session_id,
                "strategy_id": request.strategy_id,
                "force_rebuild": request.force_rebuild,
            },
        )


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": str(value)}

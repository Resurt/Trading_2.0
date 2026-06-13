"""Celery client wrapper used by FastAPI report endpoints."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from celery import Celery

from trading_api.schemas import DailyReportRunRequest, ReportJobResponse


class ReportTaskClient(Protocol):
    def enqueue_daily_report(self, request: DailyReportRunRequest) -> ReportJobResponse: ...


@dataclass(slots=True)
class CeleryReportTaskClient:
    app: Celery

    @classmethod
    def from_env(cls) -> CeleryReportTaskClient:
        broker = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
        backend = os.getenv("CELERY_RESULT_BACKEND", broker)
        return cls(Celery("trading_api_report_client", broker=broker, backend=backend))

    def enqueue_daily_report(self, request: DailyReportRunRequest) -> ReportJobResponse:
        task_name = "report_worker.rebuild_reports_for_date"
        async_result = self.app.send_task(
            task_name,
            args=(
                request.trading_date.isoformat(),
                request.strategy_id,
                request.include_counterfactual,
            ),
        )
        return ReportJobResponse(
            job_id=str(async_result.id),
            task_name=task_name,
            status="queued",
            payload={
                "trading_date": request.trading_date.isoformat(),
                "strategy_id": request.strategy_id,
                "include_counterfactual": request.include_counterfactual,
            },
        )

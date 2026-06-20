"""Celery task pipeline for reports and diagnostic analytics."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, TypeVar, cast

from celery import current_task

from report_worker.analytics import (
    CalibrationDiagnosticService,
    IntradayAnalyticsService,
    ReportAnalyticsService,
    RollingPerformanceCubeService,
)
from report_worker.celery_app import celery_app
from report_worker.metrics import observe_counterfactual_job, observe_report_generation
from trading_common.db.config import build_database_url_from_env
from trading_common.db.repositories import ReportJobRepository
from trading_common.db.service import DatabaseService

_TaskCallable = TypeVar("_TaskCallable", bound=Callable[..., object])


def report_task(*, name: str) -> Callable[[_TaskCallable], _TaskCallable]:
    return cast(Callable[[_TaskCallable], _TaskCallable], celery_app.task(name=name))


@report_task(name="report_worker.build_hourly_report")
def build_hourly_report(
    micro_session_id: str,
    strategy_id: str,
    force_rebuild: bool = True,
) -> dict[str, object]:
    task_id = _current_celery_task_id()
    try:
        with observe_report_generation(), _database().session_scope() as session:
            report_jobs = ReportJobRepository(session)
            report_jobs.mark_started_by_celery_task_id(
                task_id,
                started_at=datetime.now(tz=UTC),
            )
            service = ReportAnalyticsService(session)
            report = service.build_hourly_report(
                micro_session_id=micro_session_id,
                strategy_id=strategy_id,
                force_rebuild=force_rebuild,
            )
            payload = service.hourly_read_model(report)
            report_jobs.mark_succeeded_by_celery_task_id(
                task_id,
                finished_at=datetime.now(tz=UTC),
                result_payload=payload,
            )
            return payload
    except Exception as exc:
        _mark_report_job_failed(task_id, exc)
        raise


@report_task(name="report_worker.build_daily_report")
def build_daily_report(
    trading_date: str,
    strategy_id: str,
    instrument_id: str | None = None,
    timeframe: str | None = None,
    session_type: str | None = None,
    strategy_version: int | None = None,
    force_rebuild: bool = True,
) -> dict[str, object]:
    task_id = _current_celery_task_id()
    try:
        with observe_report_generation(), _database().session_scope() as session:
            report_jobs = ReportJobRepository(session)
            report_jobs.mark_started_by_celery_task_id(
                task_id,
                started_at=datetime.now(tz=UTC),
            )
            service = ReportAnalyticsService(session)
            report = service.build_daily_report(
                trading_date=date.fromisoformat(trading_date),
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                strategy_version=strategy_version,
                force_rebuild=force_rebuild,
            )
            payload = service.daily_read_model(report)
            report_jobs.mark_succeeded_by_celery_task_id(
                task_id,
                finished_at=datetime.now(tz=UTC),
                result_payload=payload,
            )
            return payload
    except Exception as exc:
        _mark_report_job_failed(task_id, exc)
        raise


@report_task(name="report_worker.rebuild_reports_for_date")
def rebuild_reports_for_date(
    trading_date: str,
    strategy_id: str,
    include_counterfactual: bool = True,
    instrument_id: str | None = None,
    timeframe: str | None = None,
    session_type: str | None = None,
    strategy_version: int | None = None,
    force_rebuild: bool = True,
) -> dict[str, object]:
    task_id = _current_celery_task_id()
    try:
        with observe_report_generation(), _database().session_scope() as session:
            report_jobs = ReportJobRepository(session)
            report_jobs.mark_started_by_celery_task_id(
                task_id,
                started_at=datetime.now(tz=UTC),
            )
            service = ReportAnalyticsService(session)
            report = service.rebuild_reports_for_date(
                trading_date=date.fromisoformat(trading_date),
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                strategy_version=strategy_version,
                force_rebuild=force_rebuild,
                include_counterfactual=include_counterfactual,
            )
            payload = service.daily_read_model(report)
            report_jobs.mark_succeeded_by_celery_task_id(
                task_id,
                finished_at=datetime.now(tz=UTC),
                result_payload=payload,
            )
            return payload
    except Exception as exc:
        _mark_report_job_failed(task_id, exc)
        raise


@report_task(name="report_worker.run_counterfactual_analysis_for_date")
def run_counterfactual_analysis_for_date(
    trading_date: str,
    strategy_id: str,
    instrument_id: str | None = None,
    timeframe: str | None = None,
    session_type: str | None = None,
    strategy_version: int | None = None,
    force_rebuild: bool = True,
) -> dict[str, object]:
    task_id = _current_celery_task_id()
    try:
        with observe_counterfactual_job(), _database().session_scope() as session:
            report_jobs = ReportJobRepository(session)
            report_jobs.mark_started_by_celery_task_id(
                task_id,
                started_at=datetime.now(tz=UTC),
            )
            service = ReportAnalyticsService(session)
            results = service.run_counterfactual_analysis_for_date(
                trading_date=date.fromisoformat(trading_date),
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                strategy_version=strategy_version,
                force_rebuild=force_rebuild,
            )
            payload = {
                "trading_date": trading_date,
                "strategy_id": strategy_id,
                "result_count": len(results),
                "results": service.counterfactual_read_models(results),
            }
            report_jobs.mark_succeeded_by_celery_task_id(
                task_id,
                finished_at=datetime.now(tz=UTC),
                result_payload=payload,
            )
            return payload
    except Exception as exc:
        _mark_report_job_failed(task_id, exc)
        raise


@report_task(name="report_worker.run_daily_intraday_analytics_rebuild")
def run_daily_intraday_analytics_rebuild(
    trading_date: str | None = None,
    mode: str = "all",
) -> dict[str, object]:
    target_date = date.fromisoformat(trading_date) if trading_date else _today_utc()
    with observe_report_generation(), _database().session_scope() as session:
        service = IntradayAnalyticsService(session)
        return service.build_for_trading_date(target_date, mode=mode)


@report_task(name="report_worker.run_daily_calibration_diagnostics")
def run_daily_calibration_diagnostics(
    universe: str | None = None,
    lookback_days: int = 20,
    mode: str = "all",
) -> dict[str, object]:
    target_universe = _parse_universe(universe)
    with observe_report_generation(), _database().session_scope() as session:
        diagnostic = CalibrationDiagnosticService(session).run_diagnostics(
            universe=target_universe,
            lookback_days=lookback_days,
            mode=mode,
            trigger_type="scheduled_daily",
            requested_by="report_worker",
        )
        cube = RollingPerformanceCubeService(session).build_rolling_cube(
            window_names=("7d", "20d", "60d"),
            universe=target_universe,
            mode=mode,
        )
        return {
            "diagnostic_run_id": str(diagnostic["diagnostic_run_id"]),
            "diagnosis": diagnostic["diagnosis"],
            "confidence": diagnostic["confidence"],
            "rolling_cube_rows": len(cube),
            "candidate_config_created": False,
            "runtime_config_changed": False,
        }


@report_task(name="report_worker.run_weekly_calibration_observatory")
def run_weekly_calibration_observatory(
    universe: str | None = None,
    lookback_days: int = 60,
    windows: str = "7d,20d,60d,90d,180d,365d",
    mode: str = "all",
) -> dict[str, object]:
    target_universe = _parse_universe(universe)
    window_names = tuple(part.strip() for part in windows.split(",") if part.strip())
    with observe_report_generation(), _database().session_scope() as session:
        diagnostic = CalibrationDiagnosticService(session).run_diagnostics(
            universe=target_universe,
            lookback_days=lookback_days,
            mode=mode,
            trigger_type="scheduled_weekly",
            requested_by="report_worker",
        )
        cube = RollingPerformanceCubeService(session).build_rolling_cube(
            window_names=window_names,
            universe=target_universe,
            mode=mode,
        )
        return {
            "diagnostic_run_id": str(diagnostic["diagnostic_run_id"]),
            "diagnosis": diagnostic["diagnosis"],
            "confidence": diagnostic["confidence"],
            "rolling_cube_rows": len(cube),
            "candidate_config_created": False,
            "runtime_config_changed": False,
        }


def _database() -> DatabaseService:
    return DatabaseService(build_database_url_from_env())


def _current_celery_task_id() -> str | None:
    task = cast(Any, current_task)
    request = getattr(task, "request", None)
    task_id = getattr(request, "id", None)
    return str(task_id) if task_id else None


def _mark_report_job_failed(task_id: str | None, exc: Exception) -> None:
    if task_id is None:
        return
    database = _database()
    with database.session_scope() as session:
        ReportJobRepository(session).mark_failed_by_celery_task_id(
            task_id,
            failed_at=datetime.now(tz=UTC),
            error=f"{type(exc).__name__}: {exc}",
        )


def _today_utc() -> date:
    return datetime.now(tz=UTC).date()


def _parse_universe(value: str | None) -> list[str]:
    raw = value or os.getenv("CALIBRATION_OBSERVATORY_UNIVERSE") or "SBER,GAZP"
    return [part.strip() for part in raw.split(",") if part.strip()]

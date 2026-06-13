"""Celery task pipeline for hourly, daily, rebuild, and counterfactual reports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TypeVar, cast

from report_worker.analytics import ReportAnalyticsService
from report_worker.celery_app import celery_app
from trading_common.db.config import build_database_url_from_env
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
    with _database().session_scope() as session:
        service = ReportAnalyticsService(session)
        report = service.build_hourly_report(
            micro_session_id=micro_session_id,
            strategy_id=strategy_id,
            force_rebuild=force_rebuild,
        )
        return service.hourly_read_model(report)


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
    with _database().session_scope() as session:
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
        return service.daily_read_model(report)


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
    with _database().session_scope() as session:
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
        return service.daily_read_model(report)


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
    with _database().session_scope() as session:
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
        return {
            "trading_date": trading_date,
            "strategy_id": strategy_id,
            "result_count": len(results),
            "results": service.counterfactual_read_models(results),
        }


def _database() -> DatabaseService:
    return DatabaseService(build_database_url_from_env())

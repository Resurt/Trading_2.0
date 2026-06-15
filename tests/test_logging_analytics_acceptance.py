from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from report_worker.analytics import ReportAnalyticsService
from tests.fixtures.logging_analytics_acceptance import (
    SCENARIO_NAMES,
    TRADING_DATE,
    seed_logging_analytics_acceptance_day,
)
from trading_common.analytics_acceptance import AnalyticsAcceptanceChecker
from trading_common.db.base import Base
from trading_common.db.models import AuditEvent


def test_logging_analytics_acceptance_fixture_passes_definition_of_done() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        fixture = seed_logging_analytics_acceptance_day(session)
        daily = ReportAnalyticsService(session).rebuild_reports_for_date(
            trading_date=fixture.trading_date,
            strategy_id=fixture.strategy_id,
            include_counterfactual=True,
        )
        report = AnalyticsAcceptanceChecker(session).run(
            trading_date=fixture.trading_date,
            strategy_id=fixture.strategy_id,
        )

        assert set(fixture.scenario_names) == set(SCENARIO_NAMES)
        assert report.passed, report.as_payload()
        assert {check.code for check in report.checks} == {
            "candidate_terminal_outcome",
            "blocker_measured_threshold",
            "broker_order_correlation",
            "canceled_order_counterfactual",
            "daily_report_calibration_shape",
            "hourly_rollover_no_trade_core_restart",
            "stream_reconnect_gap_recovery",
            "weekend_session_scenario",
            "no_raw_secrets_in_logs",
        }
        assert daily.market_regime == "trend_up"
        assert daily.pnl_net is not None
        assert daily.report_payload["blocker_ranking"]
        assert daily.report_payload["funnel"]
        assert daily.report_payload["missed_opportunity_summary"]
        cancel_analytics = cast(dict[str, object], daily.report_payload["canceled_order_analytics"])
        assert cancel_analytics["cancelled_intent_count"] == 2

    engine.dispose()


def test_logging_analytics_acceptance_is_deterministic() -> None:
    assert _acceptance_payload() == _acceptance_payload()


def test_logging_analytics_acceptance_detects_raw_secret_leak() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        fixture = seed_logging_analytics_acceptance_day(session)
        ReportAnalyticsService(session).rebuild_reports_for_date(
            trading_date=fixture.trading_date,
            strategy_id=fixture.strategy_id,
            include_counterfactual=True,
        )
        session.add(
            AuditEvent(
                calendar_date=TRADING_DATE,
                trading_date=TRADING_DATE,
                session_type="weekday_main",
                session_phase="continuous_trading",
                micro_session_id="2026-06-12:weekday_main:1000",
                broker_trading_status="normal_trading",
                ts_utc=datetime(2026, 6, 12, 10, 50, tzinfo=UTC),
                exchange_ts=None,
                received_ts=None,
                service="trade-core",
                actor="system",
                action="debug_headers",
                entity_type="broker_request",
                entity_id="request-1",
                severity="info",
                correlation_id="secret-leak-check",
                audit_payload={"token": "fake-token-for-secret-leak-check"},
            )
        )
        report = AnalyticsAcceptanceChecker(session).run(
            trading_date=fixture.trading_date,
            strategy_id=fixture.strategy_id,
        )
        secret_check = next(
            check for check in report.checks if check.code == "no_raw_secrets_in_logs"
        )

        assert not secret_check.passed

    engine.dispose()


def test_replay_day_acceptance_payload_is_deterministic() -> None:
    from scripts.run_replay_day import _run_once

    first = _run_once()
    second = _run_once()

    assert first == second
    assert first["session_rollover_verified"] is True
    assert first["blocker_pipeline_verified"] is True
    assert first["counterfactual_pipeline_verified"] is True


def _acceptance_payload() -> dict[str, object]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            fixture = seed_logging_analytics_acceptance_day(session)
            ReportAnalyticsService(session).rebuild_reports_for_date(
                trading_date=fixture.trading_date,
                strategy_id=fixture.strategy_id,
                include_counterfactual=True,
            )
            return AnalyticsAcceptanceChecker(session).run(
                trading_date=fixture.trading_date,
                strategy_id=fixture.strategy_id,
            ).as_payload()
    finally:
        engine.dispose()

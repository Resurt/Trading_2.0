from __future__ import annotations

import io
import json
import logging
from uuid import uuid4

from trading_common import RuntimeMode, ServiceName
from trading_common.models import AppIdentity
from trading_common.observability import (
    BOUNDED_PROMETHEUS_LABELS,
    CONTEXT_FIELDS,
    PROMETHEUS_METRIC_NAMES,
    STRICT_DOMAIN_EVENT_TYPES,
    DomainEventType,
    JsonLogFormatter,
    LogContextFilter,
    TradingMetrics,
    log_context,
)


def test_json_logging_injects_contextvars_context() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(LogContextFilter(service=ServiceName.TRADE_CORE))
    handler.setFormatter(JsonLogFormatter(service=ServiceName.TRADE_CORE))

    logger = logging.getLogger("tests.observability.context")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)

    run_id = uuid4()
    candidate_id = uuid4()
    with log_context(
        run_id=run_id,
        session_type="weekday_main",
        session_phase="continuous_trading",
        micro_session_id="2026-06-12:weekday_main:1000",
        instrument_id="MOEX:SBER",
        timeframe="5m",
        strategy_id="baseline",
        candidate_id=candidate_id,
    ):
        logger.info(
            "candidate created",
            extra={
                "event_type": DomainEventType.SIGNAL_CANDIDATE_CREATED.value,
                "expected_edge_bps": "12.5",
            },
        )

    payload = json.loads(stream.getvalue())

    assert payload["event_type"] == "signal_candidate_created"
    assert payload["service"] == "trade-core"
    assert payload["run_id"] == str(run_id)
    assert payload["candidate_id"] == str(candidate_id)
    assert payload["instrument_id"] == "MOEX:SBER"
    assert payload["expected_edge_bps"] == "12.5"


def test_strict_event_types_match_required_catalog() -> None:
    assert set(STRICT_DOMAIN_EVENT_TYPES) == {
        "signal_candidate_created",
        "candidate_stage_result_recorded",
        "market_context_snapshot_written",
        "blocker_triggered",
        "order_intent_created",
        "broker_order_posted",
        "broker_order_updated",
        "broker_order_cancelled",
        "order_state_changed",
        "fill_received",
        "strategy_state_changed",
        "risk_event_recorded",
        "session_snapshot_written",
        "market_status_changed",
        "bar_closed",
        "stream_gap_recovery_requested",
        "stream_gap_recovery_completed",
        "stream_gap_backfill_started",
        "stream_gap_backfill_completed",
        "stream_gap_recovery_failed",
        "order_reconciliation_completed",
        "position_reconciliation_completed",
    }
    assert "request_order_id" in CONTEXT_FIELDS


def test_prometheus_metrics_are_registered_and_rendered() -> None:
    metrics = TradingMetrics(
        AppIdentity(
            service=ServiceName.TRADE_CORE,
            version="0.1.0",
            runtime_mode=RuntimeMode.HISTORICAL_REPLAY,
        )
    )
    metrics.observe_broker_post_order_latency(0.12, status="success")
    metrics.observe_order_state_convergence(0.5, status="success")
    metrics.observe_candle_close_delivery_lag(
        0.25,
        instrument="MOEX:SBER",
        timeframe="5m",
    )
    metrics.observe_session_rollover_duration(
        0.8,
        session_type="weekday_main",
        status="success",
    )
    metrics.observe_report_generation_duration(1.2, status="success")
    metrics.inc_stream_reconnect(stream_type="market_data", result="success")
    metrics.inc_rejected_order(status="broker_rejected")
    metrics.inc_risk_event(result="spread_too_wide")
    metrics.inc_counterfactual_job(status="success")
    metrics.inc_report_job_failed(status="error")
    metrics.set_open_orders(2)
    metrics.set_active_positions(1, instrument="MOEX:SBER")
    metrics.set_market_stream_alive(
        True,
        stream_type="market_data",
        instrument="MOEX:SBER",
        timeframe="5m",
    )
    metrics.set_last_stream_message_age(
        3.5,
        stream_type="market_data",
        instrument="MOEX:SBER",
        timeframe="5m",
    )
    metrics.set_celery_queue_backlog(4, status="ready")

    rendered = metrics.render().decode("utf-8")

    for metric_name in PROMETHEUS_METRIC_NAMES:
        assert metric_name in rendered
    assert 'result="spread_too_wide"' in rendered
    assert 'instrument="MOEX:SBER"' in rendered
    assert 'timeframe="5m"' in rendered
    assert "runtime_mode=" not in rendered
    assert "candidate_id=" not in rendered
    assert "request_order_id=" not in rendered


def test_prometheus_labels_are_bounded_for_observability_stack() -> None:
    assert set(BOUNDED_PROMETHEUS_LABELS) == {
        "service",
        "instrument",
        "timeframe",
        "session_type",
        "stream_type",
        "status",
        "result",
    }
    assert "candidate_id" not in BOUNDED_PROMETHEUS_LABELS
    assert "request_order_id" not in BOUNDED_PROMETHEUS_LABELS
    assert "tracking_id" not in BOUNDED_PROMETHEUS_LABELS

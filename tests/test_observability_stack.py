from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_prometheus_scrape_config_includes_rules_and_backend_services() -> None:
    prometheus_config = (ROOT / "deploy/prometheus/prometheus.yml").read_text(encoding="utf-8")

    assert "/etc/prometheus/rules/*.yml" in prometheus_config
    assert "trade-core:8001" in prometheus_config
    assert "api:8000" in prometheus_config
    assert "report-worker-health:8002" in prometheus_config


def test_prometheus_alert_rules_cover_required_observability_metrics() -> None:
    alert_rules = (ROOT / "deploy/prometheus/rules/trading-alerts.yml").read_text(
        encoding="utf-8"
    )

    for metric_name in (
        "broker_post_order_latency_seconds",
        "session_rollover_duration_seconds",
        "report_generation_duration_seconds",
        "stream_reconnect_total",
        "rejected_orders_total",
        "risk_events_total",
        "counterfactual_jobs_total",
        "report_jobs_failed_total",
        "market_stream_alive",
        "last_stream_message_age_seconds",
        "celery_queue_backlog",
    ):
        assert metric_name in alert_rules

    for alert_name in (
        "TradingServiceDown",
        "MarketStreamStale",
        "BrokerPostOrderLatencyHigh",
        "CeleryQueueBacklogHigh",
        "RejectedOrdersSpike",
    ):
        assert alert_name in alert_rules


def test_grafana_observability_dashboard_has_required_sections() -> None:
    dashboard_path = ROOT / "deploy/grafana/dashboards/observability-stack.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    titles = {panel["title"] for panel in dashboard["panels"]}

    assert "a) Health trade-core" in titles
    assert "b) Broker/API latency" in titles
    assert "c) Stream reconnects and lag" in titles
    assert "d) Hourly rollover" in titles
    assert "e) Report-worker queue and failures" in titles
    assert "f) Blocker overview" in titles
    assert "g) Rejected/canceled orders" in titles
    assert "h) Top infrastructure incidents" in titles


def test_fluent_bit_loki_labels_avoid_high_cardinality_ids() -> None:
    fluent_bit_config = (ROOT / "deploy/fluent-bit/fluent-bit.conf").read_text(encoding="utf-8")

    assert "$service" in fluent_bit_config
    assert "$exchange_phase" in fluent_bit_config
    assert "$instrument" in fluent_bit_config
    assert "$candidate_id" not in fluent_bit_config
    assert "$request_order_id" not in fluent_bit_config
    assert "$exchange_order_id" not in fluent_bit_config
    assert "$tracking_id" not in fluent_bit_config

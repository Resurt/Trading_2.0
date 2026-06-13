from report_worker import health as report_worker_health
from trade_core import health as trade_core_health
from trading_api import health as api_health
from trading_common import RuntimeMode, ServiceName
from trading_common.http_health import render_health, render_metrics


def test_backend_service_skeletons_are_importable() -> None:
    assert trade_core_health().identity.service is ServiceName.TRADE_CORE
    assert api_health().identity.service is ServiceName.API
    assert report_worker_health().identity.service is ServiceName.REPORT_WORKER


def test_service_skeletons_default_to_replay_mode() -> None:
    assert trade_core_health().identity.runtime_mode is RuntimeMode.HISTORICAL_REPLAY
    assert api_health().identity.runtime_mode is RuntimeMode.HISTORICAL_REPLAY
    assert report_worker_health().identity.runtime_mode is RuntimeMode.HISTORICAL_REPLAY


def test_health_and_metrics_payloads_are_renderable() -> None:
    health = trade_core_health()
    metrics = render_metrics(health)

    assert b'"service": "trade-core"' in render_health(health)
    assert b"trading_service_up" in metrics
    assert b'service="trade-core"' in metrics

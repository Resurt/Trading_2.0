from report_worker import health as report_worker_health
from trade_core import health as trade_core_health
from trading_api import health as api_health
from trading_common import RuntimeMode, ServiceName


def test_backend_service_skeletons_are_importable() -> None:
    assert trade_core_health().identity.service is ServiceName.TRADE_CORE
    assert api_health().identity.service is ServiceName.API
    assert report_worker_health().identity.service is ServiceName.REPORT_WORKER


def test_service_skeletons_default_to_replay_mode() -> None:
    assert trade_core_health().identity.runtime_mode is RuntimeMode.HISTORICAL_REPLAY
    assert api_health().identity.runtime_mode is RuntimeMode.HISTORICAL_REPLAY
    assert report_worker_health().identity.runtime_mode is RuntimeMode.HISTORICAL_REPLAY

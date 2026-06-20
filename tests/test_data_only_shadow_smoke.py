from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import scripts.run_data_only_shadow_smoke as smoke

from trading_common.db.base import Base
from trading_common.db.service import DatabaseService

MSK = ZoneInfo("Europe/Moscow")


class FakeRuntime:
    instances: list[FakeRuntime] = []

    def __init__(self, *, config: object, launch_policy: object) -> None:
        del launch_policy
        self.config = config
        self.database = DatabaseService("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.database.engine)
        self.broker_gateway = object()
        self._session = None
        self.start_calls = 0
        self.shutdown_calls = 0
        FakeRuntime.instances.append(self)

    async def _resolve_runtime_instruments(self) -> None:
        return None

    async def start(self) -> None:
        self.start_calls += 1

    async def run_cycle(self) -> None:
        return None

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class FakePreflightService:
    def __init__(self, broker_gateway: object) -> None:
        del broker_gateway

    async def run(self, config: object) -> object:
        del config
        return FakePreflightResult()


class FakePreflightResult:
    market_open = False
    market_closed_expected = True
    reason_code = "weekend_session_closed"
    session_type = "weekend"
    session_phase = "closed"

    def as_payload(self) -> dict[str, Any]:
        now = datetime(2026, 6, 20, 22, tzinfo=MSK)
        return {
            "market_open": False,
            "market_closed_expected": True,
            "now_msk": now.isoformat(),
            "trading_date": date(2026, 6, 20).isoformat(),
            "calendar_date": date(2026, 6, 20).isoformat(),
            "session_type": "weekend",
            "session_phase": "closed",
            "broker_trading_status": "unknown",
            "api_trade_available": False,
            "next_session_at": "2026-06-21T10:00:00+03:00",
            "next_session_type": "weekend",
            "current_window_start_at": None,
            "current_window_end_at": None,
            "reason_code": "weekend_session_closed",
            "instruments_checked": [],
            "per_instrument_status": {},
            "source": "fallback_weekend_time_rules",
            "warnings": ["fallback_schedule_used"],
        }


def test_closed_market_smoke_does_not_start_runtime_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del tmp_path
    FakeRuntime.instances.clear()
    monkeypatch.setattr(smoke, "TradeCoreRuntime", FakeRuntime)
    monkeypatch.setattr(smoke, "TradingSessionPreflightService", FakePreflightService)
    args = argparse.Namespace(
        instruments="SBER,GAZP",
        minutes=0,
        database_url=None,
        require_dividend_sync=False,
        require_market_open=False,
        allow_closed_market_success=True,
        preflight_only=False,
        max_instruments_per_stream_batch=4,
        stream_batch_delay_seconds=2.0,
        json_output=True,
        dry_run=False,
    )

    payload = asyncio.run(smoke.async_main(args))

    assert payload["passed"] is True
    assert payload["market_open"] is False
    assert payload["market_closed_expected"] is True
    assert payload["warning"] == "market_closed_expected_no_live_samples"
    assert payload["post_order_calls"] == 0
    assert payload["cancel_order_calls"] == 0
    assert payload["signal_candidates_delta"] == 0
    assert payload["order_intents_delta"] == 0
    assert payload["broker_orders_delta"] == 0
    assert payload["microstructure_snapshots_delta"] == 0
    assert FakeRuntime.instances[0].start_calls == 0

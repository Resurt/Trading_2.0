from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from trade_core.broker_gateway import BrokerGateway
from trade_core.market_data import Candle, OrderBookSnapshot, PriceLevel, Timeframe
from trade_core.runtime import (
    SafeNoopBrokerGateway,
    TradeCoreRuntime,
    TradeCoreRuntimeConfig,
)
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    OrderIntent,
    SignalCandidate,
)
from trading_common.db.service import DatabaseService

MSK = ZoneInfo("Europe/Moscow")


def msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def runtime_config(tmp_path: Path) -> TradeCoreRuntimeConfig:
    return TradeCoreRuntimeConfig(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'runtime.db').as_posix()}",
        auto_create_sqlite_schema=True,
        tick_interval_seconds=0.01,
        micro_session_freeze_seconds=60,
    )


def build_runtime(
    tmp_path: Path,
    *,
    mode: RuntimeMode = RuntimeMode.HISTORICAL_REPLAY,
    gateway: SafeNoopBrokerGateway | None = None,
) -> TradeCoreRuntime:
    broker_gateway = gateway or SafeNoopBrokerGateway(now=msk(2026, 6, 12, 10))
    return TradeCoreRuntime(
        config=runtime_config(tmp_path),
        launch_policy=LaunchModePolicy.from_mode(mode),
        database=DatabaseService(runtime_config(tmp_path).database_url or ""),
        broker_gateway=cast(BrokerGateway, broker_gateway),
    )


def test_runtime_starts_historical_replay_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("TINVEST_TOKEN", raising=False)
    monkeypatch.delenv("TBANK_FULL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TBANK_READONLY_TOKEN", raising=False)
    runtime = build_runtime(tmp_path)

    snapshot = asyncio.run(runtime.run_cycle(now=msk(2026, 6, 12, 10)))

    assert runtime.stats.started is True
    assert snapshot.session_type == "weekday_main"
    assert snapshot.micro_session_id is not None
    assert runtime.stats.stream_tasks_started > 0
    asyncio.run(runtime.shutdown())


@pytest.mark.parametrize("mode", [RuntimeMode.HISTORICAL_REPLAY, RuntimeMode.SHADOW])
def test_runtime_does_not_call_post_order_in_replay_or_shadow(
    tmp_path: Path,
    mode: RuntimeMode,
) -> None:
    gateway = SafeNoopBrokerGateway(now=msk(2026, 6, 12, 10))
    runtime = build_runtime(tmp_path, mode=mode, gateway=gateway)

    asyncio.run(_run_candidate_path(runtime))

    assert gateway.post_order_calls == []
    assert runtime.stats.order_intents_created == 1
    with runtime.database.session_factory() as session:
        broker_order = session.execute(select(BrokerOrder)).scalar_one()
        assert broker_order.broker_status == "pseudo_posted"

    asyncio.run(runtime.shutdown())


def test_runtime_rejects_unconfirmed_production(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="production mode requires"):
        TradeCoreRuntime.from_env(
            {
                "TRADING_RUNTIME_MODE": "production",
                "TRADING_DATABASE_URL": f"sqlite+pysqlite:///{(tmp_path / 'prod.db').as_posix()}",
            }
        )


def test_runtime_micro_session_rollover_requests_report(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)

    asyncio.run(runtime.run_cycle(now=msk(2026, 6, 12, 10, 55)))
    asyncio.run(runtime.run_cycle(now=msk(2026, 6, 12, 11)))

    assert runtime.stats.report_requests
    assert runtime.stats.report_requests[0]["report_type"] == "hourly"
    assert runtime.stats.report_requests[0]["reason_code"] == "hourly_rollover"
    assert runtime.current_snapshot is not None
    assert runtime.current_snapshot.micro_session_id == "2026-06-12:weekday_main:20260612T1100"
    asyncio.run(runtime.shutdown())


def test_closed_bar_candidate_risk_order_path_is_deterministic(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)

    asyncio.run(_run_candidate_path(runtime))

    with runtime.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SignalCandidate)) == 1
        assert session.scalar(select(func.count()).select_from(CandidateStageResult)) == 9
        assert session.scalar(select(func.count()).select_from(BlockerEvent)) == 0
        intent = session.execute(select(OrderIntent)).scalar_one()
        broker_order = session.execute(select(BrokerOrder)).scalar_one()

    assert intent.status == "pseudo_submitted"
    assert broker_order.broker_status == "pseudo_posted"
    assert runtime.stats.candidates_created == 1
    assert runtime.stats.order_intents_created == 1
    asyncio.run(runtime.shutdown())


async def _run_candidate_path(runtime: TradeCoreRuntime) -> None:
    await runtime.run_cycle(now=msk(2026, 6, 12, 10))
    await runtime.process_order_book(
        OrderBookSnapshot(
            instrument_id="MOEX:SBER",
            bids=(PriceLevel(price=Decimal("99.99"), quantity_lots=Decimal("100")),),
            asks=(PriceLevel(price=Decimal("100.01"), quantity_lots=Decimal("100")),),
            depth=1,
            exchange_ts=msk(2026, 6, 12, 10).astimezone(UTC),
            received_ts=msk(2026, 6, 12, 10).astimezone(UTC),
        )
    )
    for offset in range(5):
        open_ts = msk(2026, 6, 12, 10) + timedelta(minutes=offset)
        close_ts = open_ts + timedelta(minutes=1)
        await runtime.process_candle(
            Candle(
                instrument_id="MOEX:SBER",
                timeframe=Timeframe.M1,
                open_ts_utc=open_ts.astimezone(UTC),
                close_ts_utc=close_ts.astimezone(UTC),
                exchange_open_ts=open_ts,
                exchange_close_ts=close_ts,
                open_price=Decimal("100"),
                high_price=Decimal("102"),
                low_price=Decimal("99"),
                close_price=Decimal("101.50"),
                volume_lots=Decimal("10"),
                is_closed=True,
                source="runtime_test",
            )
        )

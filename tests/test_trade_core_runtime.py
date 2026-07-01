from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    CandleRequest,
    DividendsRequest,
    InstrumentRef,
    InstrumentResolveRequest,
    OrderBookRequest,
    PositionsRequest,
    RequestMetadata,
    StreamEvent,
    TradingSchedulesRequest,
)
from trade_core.market_data import (
    Bar,
    Candle,
    MarketDataEvent,
    MarketEventType,
    OrderBookSnapshot,
    PriceLevel,
    Timeframe,
)
from trade_core.runtime import (
    DEFAULT_INSTRUMENTS,
    LOCAL_SQLITE_ENV,
    SafeNoopBrokerGateway,
    TradeCoreRuntime,
    TradeCoreRuntimeConfig,
)
from trade_core.strategy import ConfigDrivenStrategyEngine
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import (
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    InstrumentRegistry,
    MarketMicrostructureSnapshot,
    OrderIntent,
    PositionSnapshot,
    RobotCommand,
    SignalCandidate,
    StrategyConfig,
)
from trading_common.db.repositories import RobotCommandRepository
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


def test_runtime_config_compose_env_builds_postgres_url(tmp_path: Path) -> None:
    password_file = tmp_path / "postgres_password"
    password_file.write_text("compose-secret", encoding="utf-8")

    config = TradeCoreRuntimeConfig.from_env(
        {
            "POSTGRES_HOST": "postgres",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "trading_2_0",
            "POSTGRES_USER": "trading_app",
            "POSTGRES_PASSWORD_FILE": str(password_file),
        }
    )

    assert config.database_backend == "postgresql"
    assert "postgres:5432/trading_2_0" in (config.database_url or "")
    assert "compose-secret" not in config.database_url_redacted
    assert "***@postgres:5432" in config.database_url_redacted
    assert config.auto_create_sqlite_schema is False


def test_runtime_config_requires_explicit_local_sqlite() -> None:
    with pytest.raises(RuntimeError, match="Set DATABASE_URL"):
        TradeCoreRuntimeConfig.from_env({"POSTGRES_PASSWORD_FILE": "missing-postgres-secret"})


def test_runtime_config_allows_explicit_local_sqlite() -> None:
    config = TradeCoreRuntimeConfig.from_env({LOCAL_SQLITE_ENV: "1"})

    assert config.database_backend == "sqlite"
    assert config.auto_create_sqlite_schema is True


def test_runtime_config_enables_data_only_shadow_from_env() -> None:
    config = TradeCoreRuntimeConfig.from_env(
        {
            LOCAL_SQLITE_ENV: "1",
            "TRADING_DATA_ONLY_SHADOW": "true",
        }
    )

    assert config.data_only_shadow_enabled is True


def test_default_runtime_instruments_have_no_placeholder_uid() -> None:
    tickers = {instrument.ticker for instrument in DEFAULT_INSTRUMENTS}

    assert {"SBER", "GAZP"} <= tickers
    assert all(instrument.instrument_uid is None for instrument in DEFAULT_INSTRUMENTS)


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


class FailingRecoveryGateway(SafeNoopBrokerGateway):
    async def get_candles(
        self,
        request: CandleRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        raise RuntimeError("gap backfill failed")


class RecordingScheduleGateway(SafeNoopBrokerGateway):
    def __init__(self) -> None:
        super().__init__()
        self.trading_schedule_requests: list[TradingSchedulesRequest] = []

    async def trading_schedules(
        self,
        request: TradingSchedulesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        self.trading_schedule_requests.append(request)
        return await super().trading_schedules(request, metadata)


class RecordingStreamGateway(SafeNoopBrokerGateway):
    def __init__(self, *, now: datetime | None = None) -> None:
        super().__init__(now=now)
        self.market_stream_names: list[str] = []
        self.order_stream_accounts: list[str] = []

    async def resolve_instruments(
        self,
        request: InstrumentResolveRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="ResolveInstruments",
            data={
                "instruments": [
                    {
                        "instrument_id": f"uid-{ticker.lower()}",
                        "instrument_uid": f"uid-{ticker.lower()}",
                        "ticker": ticker,
                        "class_code": request.class_code,
                        "figi": f"figi-{ticker.lower()}",
                        "name": ticker,
                        "lot_size": 10,
                        "min_price_increment": "0.01",
                        "currency": "RUB",
                        "api_trade_available": True,
                        "short_available": False,
                        "supports_weekend": True,
                    }
                    for ticker in request.tickers
                ]
            },
            headers={},
        )

    async def stream_market_data(self, stream_name: str) -> AsyncIterator[StreamEvent]:
        self.market_stream_names.append(stream_name)
        if False:
            yield StreamEvent(stream_name=stream_name, event_type="noop", payload={})

    async def stream_orders(self, account_id: str) -> AsyncIterator[StreamEvent]:
        self.order_stream_accounts.append(account_id)
        if False:
            yield StreamEvent(stream_name="OrderStateStream", event_type="noop", payload={})


class PollingOrderBookGateway(RecordingStreamGateway):
    def __init__(self, *, now: datetime | None = None) -> None:
        super().__init__(now=now)
        self.order_book_requests: list[InstrumentRef] = []
        self.position_requests: list[PositionsRequest] = []

    async def get_positions(
        self,
        request: PositionsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        self.position_requests.append(request)
        return await super().get_positions(request, metadata)

    async def get_order_book(
        self,
        request: OrderBookRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        instrument = request.instrument
        self.order_book_requests.append(instrument)
        now = self.now or msk(2026, 6, 28, 14)
        return BrokerUnaryResponse(
            method_name="GetOrderBook",
            data={
                "instrument_id": instrument.instrument_uid or instrument.instrument_id,
                "instrument_uid": instrument.instrument_uid,
                "figi": instrument.figi,
                "depth": 10,
                "exchange_ts": now.astimezone(UTC).isoformat(),
                "bids": [{"price": "100.00", "quantity_lots": "12"}],
                "asks": [{"price": "100.10", "quantity_lots": "8"}],
            },
            headers={},
        )


class NoopReportJobDispatcher:
    def dispatch_pending(self, session: object) -> list[object]:
        del session
        return []


def noop_report_job_dispatcher() -> Any:
    return NoopReportJobDispatcher()


def data_only_preflight_payload(
    *,
    now: datetime,
    session_type: str,
    start_at: datetime,
    end_at: datetime,
    next_collection_at: datetime | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "now_msk": now.isoformat(),
        "trading_date": now.date().isoformat(),
        "calendar_date": now.date().isoformat(),
        "session_type": session_type,
        "session_phase": "continuous_trading",
        "market_open": True,
        "market_window_open": True,
        "market_closed_expected": False,
        "official_exchange_open": True,
        "official_exchange_closed": False,
        "venue_type": "official_exchange",
        "quote_source_allowed_for_data_collection": True,
        "data_only_collection_allowed": True,
        "streams_for_calibration_allowed": True,
        "current_window_start_at": start_at.isoformat(),
        "current_window_end_at": end_at.isoformat(),
        "reason_code": "market_open",
    }
    if next_collection_at is not None:
        payload["next_collection_window_at"] = next_collection_at.isoformat()
        payload["next_resume_at"] = next_collection_at.isoformat()
    return payload


def create_data_only_start_command(runtime: TradeCoreRuntime, preflight: dict[str, object]) -> None:
    with runtime.database.session_scope() as session:
        RobotCommandRepository(session).create(
            command_type="start",
            requested_by="desk-operator",
            requested_role="operator",
            requested_at=datetime.now(tz=UTC),
            payload={
                "mode": "data_shadow",
                "trading_disabled": True,
                "data_only_shadow": True,
                "instruments": "SBER,GAZP",
                "preflight_result": preflight,
            },
        )


class ResolvingGateway(SafeNoopBrokerGateway):
    def __init__(self, *, now: datetime | None = None) -> None:
        super().__init__(now=now)
        self.stream_instruments: tuple[InstrumentRef, ...] = ()
        self.resolve_calls: list[InstrumentResolveRequest] = []

    async def resolve_instruments(
        self,
        request: InstrumentResolveRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.resolve_calls.append(request)
        return BrokerUnaryResponse(
            method_name="ResolveInstruments",
            data={
                "instruments": [
                    {
                        "instrument_id": f"uid-{ticker.lower()}",
                        "instrument_uid": f"uid-{ticker.lower()}",
                        "ticker": ticker,
                        "class_code": request.class_code,
                        "figi": f"figi-{ticker.lower()}",
                        "name": ticker,
                        "lot_size": 10,
                        "min_price_increment": "0.01",
                        "currency": "RUB",
                        "api_trade_available": True,
                        "short_available": ticker == "SBER",
                        "supports_weekend": False,
                    }
                    for ticker in request.tickers
                ]
            },
            headers={},
        )

    def set_market_stream_instruments(self, instruments: tuple[InstrumentRef, ...]) -> None:
        self.stream_instruments = instruments


class PartialDividendResolvingGateway(ResolvingGateway):
    async def get_dividends(
        self,
        request: DividendsRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        if request.instrument.ticker == "GAZP":
            raise RuntimeError("dividend sync failed for GAZP")
        return BrokerUnaryResponse(
            method_name="GetDividends",
            data={"instrument_id": request.instrument.instrument_id, "dividends": []},
            headers={},
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


def test_refresh_trading_schedule_does_not_request_past_day(tmp_path: Path) -> None:
    now = msk(2026, 6, 21, 11, 30)
    gateway = RecordingScheduleGateway()
    runtime = build_runtime(tmp_path, gateway=gateway)

    asyncio.run(runtime.refresh_trading_schedule(now=now))

    assert gateway.trading_schedule_requests
    request = gateway.trading_schedule_requests[0]
    assert request.from_ == now
    assert request.to == now + timedelta(days=1)


def test_data_only_shadow_closed_bar_does_not_evaluate_strategy(tmp_path: Path) -> None:
    runtime = TradeCoreRuntime(
        config=replace(runtime_config(tmp_path), data_only_shadow_enabled=True),
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.HISTORICAL_REPLAY),
        database=DatabaseService(runtime_config(tmp_path).database_url or ""),
        broker_gateway=cast(BrokerGateway, SafeNoopBrokerGateway(now=msk(2026, 6, 12, 10))),
    )

    class RaisingStrategyEngine:
        def evaluate(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("strategy evaluation must be disabled")

    runtime.strategy_engine = cast(ConfigDrivenStrategyEngine, RaisingStrategyEngine())
    bar = Bar(
        instrument_id="MOEX:SBER",
        timeframe=Timeframe.M5,
        open_ts_utc=datetime(2026, 6, 12, 7, 0, tzinfo=UTC),
        close_ts_utc=datetime(2026, 6, 12, 7, 5, tzinfo=UTC),
        exchange_open_ts=msk(2026, 6, 12, 10, 0),
        exchange_close_ts=msk(2026, 6, 12, 10, 5),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100.5"),
        volume_lots=Decimal("10"),
        source_candle_count=5,
    )

    asyncio.run(
        runtime._handle_closed_bar(
            MarketDataEvent(
                event_type=MarketEventType.BAR_CLOSED,
                payload=bar,
                ts_utc=bar.close_ts_utc,
                instrument_id=bar.instrument_id,
            )
        )
    )

    assert runtime.stats.processed_closed_bars == 0
    assert runtime.stats.order_intents_created == 0


def test_data_only_shadow_runtime_does_not_subscribe_strategy_handler(tmp_path: Path) -> None:
    runtime = TradeCoreRuntime(
        config=replace(runtime_config(tmp_path), data_only_shadow_enabled=True),
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.HISTORICAL_REPLAY),
        database=DatabaseService(runtime_config(tmp_path).database_url or ""),
        broker_gateway=cast(BrokerGateway, SafeNoopBrokerGateway(now=msk(2026, 6, 12, 10))),
    )

    asyncio.run(runtime.start())

    assert runtime.live_market_data_collector is not None
    assert runtime.market_event_bus.subscribers_for(MarketEventType.BAR_CLOSED) == 0

    asyncio.run(runtime.shutdown())


def test_data_only_shadow_runtime_waits_for_operator_start_and_stops(tmp_path: Path) -> None:
    gateway = RecordingStreamGateway(now=msk(2026, 6, 13, 11, 30))
    runtime = TradeCoreRuntime(
        config=replace(runtime_config(tmp_path), data_only_shadow_enabled=True),
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(runtime_config(tmp_path).database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
    )

    async def run() -> None:
        await runtime.start()
        await asyncio.sleep(0)
        assert gateway.market_stream_names == []
        assert gateway.order_stream_accounts == []
        assert runtime.stats.stream_tasks_started == 0

        with runtime.database.session_scope() as session:
            RobotCommandRepository(session).create(
                command_type="start",
                requested_by="desk-operator",
                requested_role="operator",
                requested_at=datetime.now(tz=UTC),
                payload={
                    "mode": "data_shadow",
                    "trading_disabled": True,
                    "data_only_shadow": True,
                    "preflight_result": {
                        "market_open": True,
                        "market_closed_expected": False,
                        "official_exchange_open": True,
                        "official_exchange_closed": False,
                        "venue_type": "official_exchange",
                        "quote_source_allowed_for_data_collection": True,
                        "data_only_collection_allowed": True,
                        "streams_for_calibration_allowed": True,
                        "reason_code": "market_open",
                    },
                },
            )
        assert await runtime.process_robot_commands_async() == 1
        await asyncio.sleep(0)
        assert "market_trades" in runtime.config.data_only_stream_names
        assert gateway.market_stream_names == list(runtime.config.data_only_stream_names)
        assert gateway.order_stream_accounts == []
        assert runtime.stats.stream_tasks_started == len(runtime.config.data_only_stream_names)
        assert runtime.stats.collector_state == "collecting"

        with runtime.database.session_scope() as session:
            RobotCommandRepository(session).create(
                command_type="stop",
                requested_by="desk-operator",
                requested_role="operator",
                requested_at=datetime.now(tz=UTC),
                payload={
                    "mode": "data_shadow",
                    "trading_disabled": True,
                    "data_only_shadow": True,
                },
            )
        assert await runtime.process_robot_commands_async() == 1
        assert runtime.stats.stream_tasks_started == 0
        assert runtime.stats.collector_state == "stopped_by_operator"
        await runtime.shutdown()

    asyncio.run(run())


def test_data_only_start_probe_fallback_polls_order_book_and_writes_microstructure(
    tmp_path: Path,
) -> None:
    gateway = PollingOrderBookGateway(now=msk(2026, 6, 28, 14, 30))
    config = replace(
        runtime_config(tmp_path),
        data_only_shadow_enabled=True,
        data_only_order_book_poll_interval_seconds=1,
    )
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(config.database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
        report_job_dispatcher=noop_report_job_dispatcher(),
    )

    async def run() -> None:
        await runtime.start()
        with runtime.database.session_scope() as session:
            RobotCommandRepository(session).create(
                command_type="start",
                requested_by="desk-operator",
                requested_role="operator",
                requested_at=datetime.now(tz=UTC),
                payload={
                    "mode": "data_shadow",
                    "trading_disabled": True,
                    "data_only_shadow": True,
                    "preflight_result": {
                        "now_msk": "2026-06-28T14:30:00+03:00",
                        "session_type": "weekend",
                        "session_phase": "continuous_trading",
                        "market_open": True,
                        "market_window_open": True,
                        "market_closed_expected": False,
                        "official_exchange_open": False,
                        "official_exchange_closed": False,
                        "venue_type": "broker_status_fallback_time_rules",
                        "quote_source_allowed_for_data_collection": True,
                        "data_only_collection_allowed": True,
                        "streams_for_calibration_allowed": True,
                        "reason_code": "market_open",
                    },
                },
            )

        assert await runtime.process_robot_commands_async() == 1
        assert runtime.stats.collector_state == "collecting"
        await runtime.run_cycle(now=msk(2026, 6, 28, 14, 30))

        with runtime.database.session_factory() as session:
            snapshot = session.execute(
                select(MarketMicrostructureSnapshot).order_by(
                    MarketMicrostructureSnapshot.ts_utc
                )
            ).scalars().first()
            assert snapshot is not None
            assert snapshot.session_type == "weekend"
            assert snapshot.session_phase == "continuous_trading"
            assert snapshot.best_bid == Decimal("100.00000000")
            assert snapshot.best_ask == Decimal("100.10000000")
            assert snapshot.spread_abs == Decimal("0.10000000")
            assert snapshot.mid_price == Decimal("100.05000000")
            assert snapshot.spread_bps == Decimal("9.9950")
            assert snapshot.snapshot_payload["data_only_polling_fallback"] is True
            assert snapshot.snapshot_payload["include_in_calibration"] is True
            assert snapshot.snapshot_payload["calibration_allowed"] is True
            assert session.scalar(select(func.count()).select_from(SignalCandidate)) == 0
            assert session.scalar(select(func.count()).select_from(OrderIntent)) == 0
            assert session.scalar(select(func.count()).select_from(BrokerOrder)) == 0

        assert gateway.order_book_requests
        assert gateway.position_requests == []
        assert gateway.post_order_calls == []
        assert gateway.cancel_order_calls == []
        await runtime.shutdown()

    asyncio.run(run())


def test_data_only_collection_auto_stops_after_preflight_window_end(tmp_path: Path) -> None:
    gateway = PollingOrderBookGateway(now=msk(2026, 6, 28, 14, 30))
    config = replace(
        runtime_config(tmp_path),
        data_only_shadow_enabled=True,
        data_only_order_book_poll_interval_seconds=1,
    )
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(config.database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
    )

    async def run() -> None:
        await runtime.start()
        with runtime.database.session_scope() as session:
            RobotCommandRepository(session).create(
                command_type="start",
                requested_by="desk-operator",
                requested_role="operator",
                requested_at=datetime.now(tz=UTC),
                payload={
                    "mode": "data_shadow",
                    "trading_disabled": True,
                    "data_only_shadow": True,
                    "preflight_result": {
                        "now_msk": "2026-06-28T14:30:00+03:00",
                        "session_type": "weekend",
                        "session_phase": "continuous_trading",
                        "market_open": True,
                        "market_window_open": True,
                        "market_closed_expected": False,
                        "official_exchange_open": True,
                        "official_exchange_closed": False,
                        "venue_type": "official_exchange",
                        "quote_source_allowed_for_data_collection": True,
                        "data_only_collection_allowed": True,
                        "streams_for_calibration_allowed": True,
                        "current_window_start_at": "2026-06-28T10:00:00+03:00",
                        "current_window_end_at": "2026-06-28T19:00:00+03:00",
                        "reason_code": "market_open",
                    },
                },
            )

        assert await runtime.process_robot_commands_async() == 1
        assert runtime.stats.collector_state == "collecting"
        await runtime.run_cycle(now=msk(2026, 6, 28, 14, 30))

        with runtime.database.session_factory() as session:
            first_snapshot_count = session.scalar(
                select(func.count()).select_from(MarketMicrostructureSnapshot)
            )
            first_snapshot = session.execute(
                select(MarketMicrostructureSnapshot).order_by(
                    MarketMicrostructureSnapshot.ts_utc.desc()
                )
            ).scalars().first()
            assert first_snapshot_count == 2
            assert first_snapshot is not None
            assert first_snapshot.session_phase == "continuous_trading"
            assert first_snapshot.micro_session_id.endswith("T1400")
            assert first_snapshot.snapshot_payload["include_in_calibration"] is True

        order_book_requests_before_close = len(gateway.order_book_requests)
        gateway.now = msk(2026, 6, 28, 19, 1)
        await runtime.run_cycle(now=msk(2026, 6, 28, 19, 1))

        assert runtime.stats.collector_state == "stopped_day_complete"
        assert runtime.robot_control_state == "stopped_day_complete"
        assert runtime.stats.daily_collection_active is False
        assert runtime.stats.completed_for_day is True
        assert runtime.stats.stream_tasks_started == 0
        assert len(gateway.order_book_requests) == order_book_requests_before_close

        with runtime.database.session_factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(MarketMicrostructureSnapshot))
                == first_snapshot_count
            )
            auto_stop_event = session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "data_only_shadow_collection_auto_stopped"
                )
            ).scalars().first()
            assert auto_stop_event is not None
            assert auto_stop_event.audit_payload["reason_code"] == (
                "data_only_session_window_closed"
            )
            assert auto_stop_event.audit_payload["collector_state"] == "stopped_day_complete"
            day_complete_event = session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "data_only_shadow_collection_day_complete"
                )
            ).scalars().first()
            assert day_complete_event is not None
            assert session.scalar(select(func.count()).select_from(SignalCandidate)) == 0
            assert session.scalar(select(func.count()).select_from(OrderIntent)) == 0
            assert session.scalar(select(func.count()).select_from(BrokerOrder)) == 0

        assert gateway.post_order_calls == []
        assert gateway.cancel_order_calls == []
        await runtime.shutdown()

    asyncio.run(run())


def test_data_only_weekday_start_rolls_morning_main_evening_and_completes_day(
    tmp_path: Path,
) -> None:
    gateway = PollingOrderBookGateway(now=msk(2026, 6, 29, 7, 5))
    config = replace(
        runtime_config(tmp_path),
        data_only_shadow_enabled=True,
        data_only_order_book_poll_interval_seconds=1,
    )
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(config.database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
        report_job_dispatcher=noop_report_job_dispatcher(),
    )

    async def run() -> None:
        await runtime.start()
        create_data_only_start_command(
            runtime,
            data_only_preflight_payload(
                now=msk(2026, 6, 29, 7, 5),
                session_type="weekday_morning",
                start_at=msk(2026, 6, 29, 7),
                end_at=msk(2026, 6, 29, 10),
            ),
        )
        assert await runtime.process_robot_commands_async() == 1
        assert runtime.stats.collector_state == "collecting"
        assert runtime.stats.daily_collection_active is True

        await runtime.run_cycle(now=msk(2026, 6, 29, 7, 5))
        gateway.now = msk(2026, 6, 29, 9, 59)
        await runtime.run_cycle(now=msk(2026, 6, 29, 9, 59))
        assert runtime.stats.collector_state == "collecting"

        gateway.now = msk(2026, 6, 29, 10)
        await runtime.run_cycle(now=msk(2026, 6, 29, 10))
        assert runtime.stats.collector_state == "collecting"
        assert runtime.stats.current_window_state == "collecting"

        with runtime.database.session_factory() as session:
            latest = session.execute(
                select(MarketMicrostructureSnapshot).order_by(
                    MarketMicrostructureSnapshot.ts_utc.desc()
                )
            ).scalars().first()
            assert latest is not None
            assert latest.session_type == "weekday_main"
            assert latest.snapshot_payload["include_in_calibration"] is True

        gateway.now = msk(2026, 6, 29, 18, 59)
        await runtime.run_cycle(now=msk(2026, 6, 29, 18, 59))
        assert runtime.stats.collector_state == "collecting"
        assert runtime.stats.daily_collection_active is True
        assert runtime.stats.next_collection_window_at == msk(2026, 6, 29, 19)

        gateway.now = msk(2026, 6, 29, 19)
        await runtime.run_cycle(now=msk(2026, 6, 29, 19))
        assert runtime.stats.collector_state == "collecting"

        with runtime.database.session_factory() as session:
            latest = session.execute(
                select(MarketMicrostructureSnapshot).order_by(
                    MarketMicrostructureSnapshot.ts_utc.desc()
                )
            ).scalars().first()
            assert latest is not None
            assert latest.session_type == "weekday_evening"

        gateway.now = msk(2026, 6, 29, 23, 50)
        await runtime.run_cycle(now=msk(2026, 6, 29, 23, 50))
        assert runtime.stats.collector_state == "stopped_day_complete"
        assert runtime.stats.daily_collection_active is False
        assert runtime.stats.completed_for_day is True

        with runtime.database.session_factory() as session:
            actions = set(session.execute(select(AuditEvent.action)).scalars())
            assert "data_only_shadow_collection_paused_until_next_window" in actions
            assert "data_only_shadow_collection_resumed" in actions
            assert "data_only_shadow_collection_day_complete" in actions
            lifecycle_events = session.execute(
                select(AuditEvent).where(
                    AuditEvent.action.in_(
                        {
                            "data_only_shadow_collection_started",
                            "data_only_shadow_collection_window_closed",
                            "data_only_shadow_collection_paused_until_next_window",
                            "data_only_shadow_collection_resumed",
                            "data_only_shadow_collection_day_complete",
                        }
                    )
                )
            ).scalars()
            for event in lifecycle_events:
                assert "uses_pseudo_orders" not in event.audit_payload
                assert "shadow_pseudo_order" not in event.audit_payload
            assert session.scalar(select(func.count()).select_from(SignalCandidate)) == 0
            assert session.scalar(select(func.count()).select_from(OrderIntent)) == 0
            assert session.scalar(select(func.count()).select_from(BrokerOrder)) == 0

        assert gateway.post_order_calls == []
        assert gateway.cancel_order_calls == []
        await runtime.shutdown()

    asyncio.run(run())


def test_data_only_manual_stop_cancels_daily_auto_resume(tmp_path: Path) -> None:
    gateway = PollingOrderBookGateway(now=msk(2026, 6, 29, 7, 5))
    config = replace(
        runtime_config(tmp_path),
        data_only_shadow_enabled=True,
        data_only_order_book_poll_interval_seconds=1,
    )
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(config.database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
        report_job_dispatcher=noop_report_job_dispatcher(),
    )

    async def run() -> None:
        await runtime.start()
        create_data_only_start_command(
            runtime,
            data_only_preflight_payload(
                now=msk(2026, 6, 29, 7, 5),
                session_type="weekday_morning",
                start_at=msk(2026, 6, 29, 7),
                end_at=msk(2026, 6, 29, 10),
            ),
        )
        assert await runtime.process_robot_commands_async() == 1
        await runtime.run_cycle(now=msk(2026, 6, 29, 7, 5))

        with runtime.database.session_scope() as session:
            RobotCommandRepository(session).create(
                command_type="stop",
                requested_by="desk-operator",
                requested_role="operator",
                requested_at=datetime.now(tz=UTC),
                payload={"mode": "data_shadow", "data_only_shadow": True},
            )
        assert await runtime.process_robot_commands_async() == 1
        assert runtime.stats.collector_state == "stopped_by_operator"
        assert runtime.stats.daily_collection_active is False
        requests_after_stop = len(gateway.order_book_requests)

        gateway.now = msk(2026, 6, 29, 10)
        await runtime.run_cycle(now=msk(2026, 6, 29, 10))
        assert runtime.stats.collector_state == "stopped_by_operator"
        assert len(gateway.order_book_requests) == requests_after_stop
        assert gateway.post_order_calls == []
        assert gateway.cancel_order_calls == []
        await runtime.shutdown()

    asyncio.run(run())


def test_data_only_daily_intent_restores_and_resumes_after_runtime_restart(
    tmp_path: Path,
) -> None:
    database_url = runtime_config(tmp_path).database_url or ""
    config = replace(
        runtime_config(tmp_path),
        database_url=database_url,
        data_only_shadow_enabled=True,
        data_only_order_book_poll_interval_seconds=1,
    )
    first_gateway = PollingOrderBookGateway(now=msk(2026, 6, 29, 6, 50))
    first_runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(database_url),
        broker_gateway=cast(BrokerGateway, first_gateway),
        report_job_dispatcher=noop_report_job_dispatcher(),
    )

    async def first_run() -> None:
        await first_runtime.start()
        preflight = data_only_preflight_payload(
            now=msk(2026, 6, 29, 6, 50),
            session_type="weekday_morning",
            start_at=msk(2026, 6, 29, 7),
            end_at=msk(2026, 6, 29, 10),
            next_collection_at=msk(2026, 6, 29, 7),
        )
        preflight.update(
            {
                "session_phase": "closed",
                "market_open": False,
                "market_window_open": False,
                "data_only_collection_allowed": False,
                "streams_for_calibration_allowed": False,
                "reason_code": "before_collection_window",
            }
        )
        create_data_only_start_command(
            first_runtime,
            preflight,
        )
        assert await first_runtime.process_robot_commands_async() == 1
        assert first_runtime.stats.collector_state == "armed_until_next_window"
        assert first_runtime.stats.daily_collection_active is True
        assert first_gateway.order_book_requests == []
        await first_runtime.shutdown()

    asyncio.run(first_run())

    second_gateway = PollingOrderBookGateway(now=msk(2026, 6, 29, 7))
    second_runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(database_url),
        broker_gateway=cast(BrokerGateway, second_gateway),
        report_job_dispatcher=noop_report_job_dispatcher(),
    )

    async def second_run() -> None:
        await second_runtime.start()
        assert second_runtime.stats.daily_collection_active is True
        assert second_runtime.stats.collector_state == "armed_until_next_window"
        await second_runtime.run_cycle(now=msk(2026, 6, 29, 7))
        assert second_runtime.stats.collector_state == "collecting"
        with second_runtime.database.session_factory() as session:
            latest = session.execute(
                select(MarketMicrostructureSnapshot).order_by(
                    MarketMicrostructureSnapshot.ts_utc.desc()
                )
            ).scalars().first()
            assert latest is not None
            assert latest.session_type == "weekday_morning"
        assert second_gateway.post_order_calls == []
        assert second_gateway.cancel_order_calls == []
        await second_runtime.shutdown()

    asyncio.run(second_run())


def test_data_only_daily_intent_restarts_active_window_after_runtime_restart(
    tmp_path: Path,
) -> None:
    database_url = runtime_config(tmp_path).database_url or ""
    config = replace(
        runtime_config(tmp_path),
        database_url=database_url,
        data_only_shadow_enabled=True,
        data_only_order_book_poll_interval_seconds=1,
    )
    first_gateway = PollingOrderBookGateway(now=msk(2026, 6, 29, 10, 10))
    first_runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(database_url),
        broker_gateway=cast(BrokerGateway, first_gateway),
        report_job_dispatcher=noop_report_job_dispatcher(),
    )

    async def first_run() -> int:
        await first_runtime.start()
        create_data_only_start_command(
            first_runtime,
            data_only_preflight_payload(
                now=msk(2026, 6, 29, 10, 10),
                session_type="weekday_main",
                start_at=msk(2026, 6, 29, 10),
                end_at=msk(2026, 6, 29, 19),
                next_collection_at=msk(2026, 6, 29, 19),
            ),
        )
        assert await first_runtime.process_robot_commands_async() == 1
        await first_runtime.run_cycle(now=msk(2026, 6, 29, 10, 10))
        assert first_runtime.stats.collector_state == "collecting"
        with first_runtime.database.session_factory() as session:
            snapshot_count = session.scalar(
                select(func.count()).select_from(MarketMicrostructureSnapshot)
            )
        await first_runtime.shutdown()
        return int(snapshot_count or 0)

    first_snapshot_count = asyncio.run(first_run())

    second_gateway = PollingOrderBookGateway(now=msk(2026, 6, 29, 10, 30))
    second_runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(database_url),
        broker_gateway=cast(BrokerGateway, second_gateway),
        report_job_dispatcher=noop_report_job_dispatcher(),
    )

    async def second_run() -> None:
        await second_runtime.start()
        assert second_runtime.stats.daily_collection_active is True
        assert second_runtime.stats.collector_state == "paused_until_next_window"
        assert second_runtime.stats.next_resume_at is None
        await second_runtime.run_cycle(now=msk(2026, 6, 29, 10, 30))
        assert second_runtime.stats.collector_state == "collecting"
        assert second_gateway.order_book_requests
        with second_runtime.database.session_factory() as session:
            snapshot_count = session.scalar(
                select(func.count()).select_from(MarketMicrostructureSnapshot)
            )
            latest = session.execute(
                select(MarketMicrostructureSnapshot).order_by(
                    MarketMicrostructureSnapshot.ts_utc.desc()
                )
            ).scalars().first()
        assert int(snapshot_count or 0) > first_snapshot_count
        assert latest is not None
        assert latest.session_type == "weekday_main"
        assert second_gateway.post_order_calls == []
        assert second_gateway.cancel_order_calls == []
        await second_runtime.shutdown()

    asyncio.run(second_run())


def test_data_only_shadow_start_command_closed_market_does_not_create_orders(
    tmp_path: Path,
) -> None:
    gateway = RecordingStreamGateway(now=msk(2026, 6, 21, 22, 0))
    runtime = TradeCoreRuntime(
        config=replace(runtime_config(tmp_path), data_only_shadow_enabled=True),
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(runtime_config(tmp_path).database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
    )

    async def run() -> None:
        await runtime.start()
        with runtime.database.session_scope() as session:
            RobotCommandRepository(session).create(
                command_type="start",
                requested_by="desk-operator",
                requested_role="operator",
                requested_at=datetime.now(tz=UTC),
                payload={
                    "mode": "data_shadow",
                    "trading_disabled": True,
                    "data_only_shadow": True,
                    "preflight_result": {
                        "market_open": False,
                        "market_closed_expected": True,
                        "reason_code": "market_closed_expected",
                    },
                },
            )
        assert await runtime.process_robot_commands_async() == 1
        assert runtime.stats.collector_state == "preflight_blocked"
        assert runtime.stats.stream_tasks_started == 0
        assert gateway.market_stream_names == []
        assert gateway.order_stream_accounts == []
        assert gateway.post_order_calls == []
        assert gateway.cancel_order_calls == []
        with runtime.database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(SignalCandidate)) == 0
            assert session.scalar(select(func.count()).select_from(OrderIntent)) == 0
            assert session.scalar(select(func.count()).select_from(BrokerOrder)) == 0
        await runtime.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("mode", [RuntimeMode.HISTORICAL_REPLAY, RuntimeMode.SHADOW])
def test_runtime_does_not_call_post_order_in_replay_or_shadow(
    tmp_path: Path,
    mode: RuntimeMode,
) -> None:
    gateway: SafeNoopBrokerGateway = (
        ResolvingGateway(now=msk(2026, 6, 12, 10))
        if mode is RuntimeMode.SHADOW
        else SafeNoopBrokerGateway(now=msk(2026, 6, 12, 10))
    )
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


def test_runtime_requires_tbank_sdk_for_production_like_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_sdk_import() -> object:
        raise RuntimeError("sdk missing in test image")

    monkeypatch.setattr("trade_core.runtime.load_tbank_sdk", fail_sdk_import)

    with pytest.raises(RuntimeError, match="T-Bank SDK extra is required"):
        TradeCoreRuntime(
            config=runtime_config(tmp_path),
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
            database=DatabaseService(runtime_config(tmp_path).database_url or ""),
        )


def test_runtime_resolves_default_instruments_for_shadow_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("trade_core.runtime.load_tbank_sdk", lambda: object())
    gateway = ResolvingGateway(now=msk(2026, 6, 12, 10))
    runtime = build_runtime(tmp_path, mode=RuntimeMode.SHADOW, gateway=gateway)

    asyncio.run(runtime.start())

    assert gateway.resolve_calls[0].tickers == ("SBER", "GAZP")
    assert tuple(instrument.instrument_id for instrument in runtime.config.instruments) == (
        "MOEX:SBER",
        "MOEX:GAZP",
    )
    assert tuple(instrument.instrument_uid for instrument in runtime.config.instruments) == (
        "uid-sber",
        "uid-gazp",
    )
    assert tuple(instrument.instrument_uid for instrument in gateway.stream_instruments) == (
        "uid-sber",
        "uid-gazp",
    )
    with runtime.database.session_factory() as session:
        rows = session.execute(
            select(InstrumentRegistry).order_by(InstrumentRegistry.ticker)
        ).scalars()
        registry = {row.ticker: row for row in rows}
    assert registry["SBER"].instrument_id == "MOEX:SBER"
    assert registry["SBER"].instrument_uid == "uid-sber"
    assert registry["SBER"].source == "tbank_resolved"
    assert registry["SBER"].resolution_status == "resolved"
    assert registry["GAZP"].instrument_id == "MOEX:GAZP"
    asyncio.run(runtime.shutdown())


@pytest.mark.parametrize(
    ("fail_open", "expected_state"),
    [(False, "degraded"), (True, "running")],
)
def test_runtime_marks_dividend_calendar_unavailable_on_partial_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail_open: bool,
    expected_state: str,
) -> None:
    monkeypatch.setattr("trade_core.runtime.load_tbank_sdk", lambda: object())
    gateway = PartialDividendResolvingGateway(now=msk(2026, 6, 12, 10))
    config = replace(
        runtime_config(tmp_path),
        dividend_sync_enabled=True,
        dividend_sync_fail_open=fail_open,
    )
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        database=DatabaseService(config.database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
    )

    asyncio.run(runtime.start())

    assert runtime._dividend_calendar_available is False
    assert runtime.robot_control_state == expected_state
    session = runtime._session
    assert session is not None
    rows = session.execute(
        select(AuditEvent).where(
            AuditEvent.action.in_(
                ("dividend_sync_completed_with_errors", "dividend_sync_failed")
            )
        )
    ).scalars().all()
    matching_payloads = [
        row.audit_payload for row in rows if "dividend_sync_clean" in row.audit_payload
    ]
    assert matching_payloads
    latest_payload = matching_payloads[-1]
    assert latest_payload["dividend_sync_clean"] is False
    assert latest_payload["failed_instruments"] == 1
    assert latest_payload["fail_open"] is fail_open
    asyncio.run(runtime.shutdown())


def test_runtime_loads_strategy_config_from_database(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    with runtime.database.session_scope() as session:
        session.add(
            StrategyConfig(
                strategy_id="baseline",
                version=7,
                session_template="weekday_main",
                is_active=True,
                valid_from=datetime.now(tz=UTC),
                valid_to=None,
                config_payload={
                    "allow_long": True,
                    "allow_short": True,
                    "assumed_commission_bps_per_side": "6",
                    "assumed_slippage_bps": "2",
                },
                risk_limits={
                    "max_position_lots": 3,
                    "max_daily_loss_rub": "1500",
                    "min_edge_after_costs_bps": "4",
                },
            )
        )

    asyncio.run(runtime.start())

    assert runtime.strategy_config.strategy_id == "baseline"
    assert runtime.strategy_config.strategy_version == 7
    assert runtime.strategy_config.allow_short is True
    assert runtime.risk_limits.max_position_lots == 3
    assert runtime.risk_limits.max_daily_loss_rub == Decimal("1500")
    assert runtime.risk_limits.assumed_commission_bps_per_side == Decimal("6")
    assert runtime.risk_limits.assumed_slippage_bps == Decimal("2")
    asyncio.run(runtime.shutdown())


def test_runtime_micro_session_rollover_requests_report(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)

    asyncio.run(runtime.run_cycle(now=msk(2026, 6, 12, 10, 55)))
    asyncio.run(runtime.run_cycle(now=msk(2026, 6, 12, 11)))

    assert runtime.stats.report_requests
    assert runtime.stats.report_requests[0]["report_type"] == "hourly"
    assert runtime.stats.report_requests[0]["reason_code"] == "hourly_rollover"
    assert runtime.current_snapshot is not None
    assert runtime.current_snapshot.micro_session_id == "2026-06-12:weekday_main:20260612T1100"
    with runtime.database.session_factory() as session:
        snapshots = list(
            session.execute(
                select(PositionSnapshot).order_by(PositionSnapshot.snapshot_ts)
            ).scalars()
        )
        assert len(snapshots) >= 2
        assert {snapshot.snapshot_reason for snapshot in snapshots} >= {
            "micro_session_session_run_opened",
            "micro_session_snapshot_taken",
        }
    asyncio.run(runtime.shutdown())


def test_emergency_stop_cancels_working_orders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("trade_core.runtime.load_tbank_sdk", lambda: object())
    gateway = ResolvingGateway(now=msk(2026, 6, 12, 10))
    config = runtime_config(tmp_path)
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(
            RuntimeMode.SANDBOX,
            sandbox_orders_confirmed=True,
        ),
        database=DatabaseService(config.database_url or ""),
        broker_gateway=cast(BrokerGateway, gateway),
    )
    order_intent_id = uuid4()

    async def run() -> None:
        await runtime.run_cycle(now=msk(2026, 6, 12, 10))
        assert runtime.current_snapshot is not None
        request_order_id = uuid4()
        with runtime.database.session_scope() as session:
            session.add(
                OrderIntent(
                    calendar_date=runtime.current_snapshot.calendar_date,
                    trading_date=runtime.current_snapshot.trading_date,
                    session_type=runtime.current_snapshot.session_type.value,
                    session_phase=runtime.current_snapshot.session_phase.value,
                    micro_session_id=runtime.current_snapshot.micro_session_id or "unassigned",
                    broker_trading_status=runtime.current_snapshot.broker_trading_status,
                    order_intent_id=order_intent_id,
                    candidate_id=None,
                    instrument_id="uid-sber",
                    timeframe="5m",
                    strategy_id=runtime.strategy_config.strategy_id,
                    strategy_version=runtime.strategy_config.strategy_version,
                    side="buy",
                    order_action="place",
                    order_type="limit",
                    lot_qty=1,
                    intended_price=Decimal("100"),
                    time_in_force="day",
                    request_order_id=request_order_id,
                    tracking_id="tracking-open",
                    idempotency_key=f"test:{request_order_id}",
                    execution_policy_version=1,
                    status="submitted",
                    cancel_reason_code=None,
                    reject_reason_code=None,
                    created_ts=datetime.now(tz=UTC),
                    submitted_ts=datetime.now(tz=UTC),
                    terminal_ts=None,
                    intent_payload={
                        "account_id": "account-1",
                        "instrument_uid": "uid-sber",
                        "ticker": "SBER",
                        "class_code": "TQBR",
                    },
                )
            )
            session.add(
                RobotCommand(
                    command_type="emergency_stop",
                    requested_by="desk-operator",
                    requested_role="operator",
                    requested_at=datetime.now(tz=UTC),
                    status="requested",
                    reason_code=None,
                    accepted_at=None,
                    applied_at=None,
                    finished_at=None,
                    payload={"source": "test"},
                    result_payload={},
                )
            )
        processed = await runtime.process_robot_commands_async()
        assert processed == 1

    asyncio.run(run())

    assert len(gateway.cancel_order_calls) == 1
    cancel_request = gateway.cancel_order_calls[0]
    assert cancel_request.payload["cancel_reason_code"] == "manual_operator_emergency_stop"
    assert runtime.robot_control_state == "emergency_stopped"
    assert runtime.stats.open_orders == 0
    with runtime.database.session_factory() as session:
        intent = session.get(OrderIntent, order_intent_id)
        assert intent is not None
        assert intent.status == "cancelled"
        assert intent.cancel_reason_code == "manual_operator_emergency_stop"
    asyncio.run(runtime.shutdown())


def test_stop_command_reaches_trade_core_consumer(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    command_id = None

    asyncio.run(runtime.start())
    with runtime.database.session_scope() as session:
        command = RobotCommandRepository(session).create(
            command_type="stop",
            requested_by="desk-operator",
            requested_role="operator",
            requested_at=datetime.now(tz=UTC),
            payload={"source": "test"},
        )
        command_id = command.command_id

    processed = runtime.process_robot_commands()

    assert processed == 1
    assert runtime.robot_control_state == "stopped"
    assert command_id is not None
    with runtime.database.session_factory() as session:
        stored_command = session.get(RobotCommand, command_id)
        assert stored_command is not None
        assert stored_command.status == "applied"
        assert stored_command.reason_code == "runtime_safe_stopped"
    asyncio.run(runtime.shutdown())


def test_closed_bar_candidate_risk_order_path_is_deterministic(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)

    asyncio.run(_run_candidate_path(runtime))

    with runtime.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SignalCandidate)) == 1
        stage_names = list(
            session.execute(
                select(CandidateStageResult.stage_name).order_by(
                    CandidateStageResult.stage_seq
                )
            ).scalars()
        )
        assert len(stage_names) == 24
        assert {
            "dividend_calendar_available",
            "future_dividend_risk_window_policy",
            "short_blocked_dividend_window",
            "dividend_gap_day_policy",
            "corporate_action_day_policy",
            "short_on_special_day_policy",
            "position_state_freshness",
            "position_reconciliation",
            "long_allowed_by_config",
            "total_expected_costs",
            "max_gross_exposure",
            "max_net_exposure",
        } <= set(stage_names)
        assert session.scalar(select(func.count()).select_from(BlockerEvent)) == 0
        intent = session.execute(select(OrderIntent)).scalar_one()
        broker_order = session.execute(select(BrokerOrder)).scalar_one()

    assert intent.status == "pseudo_submitted"
    assert broker_order.broker_status == "pseudo_posted"
    assert runtime.stats.candidates_created == 1
    assert runtime.stats.order_intents_created == 1
    asyncio.run(runtime.shutdown())


def test_stream_gap_recovery_failure_marks_runtime_degraded(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        gateway=FailingRecoveryGateway(now=msk(2026, 6, 12, 10)),
    )

    async def run() -> None:
        await runtime.run_cycle(now=msk(2026, 6, 12, 10))
        with pytest.raises(RuntimeError, match="gap backfill failed"):
            await runtime._recover_stream_gap_from_gateway("candles", "account-1")

    asyncio.run(run())

    assert runtime.robot_control_state == "degraded"
    asyncio.run(runtime.shutdown())


async def _run_candidate_path(
    runtime: TradeCoreRuntime,
    *,
    instrument_id: str | None = None,
) -> None:
    await runtime.run_cycle(now=msk(2026, 6, 12, 10))
    resolved_instrument_id = instrument_id or runtime.config.instruments[0].instrument_id
    await runtime.process_order_book(
        OrderBookSnapshot(
            instrument_id=resolved_instrument_id,
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
                instrument_id=resolved_instrument_id,
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

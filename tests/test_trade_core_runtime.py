from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
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
)
from trade_core.market_data import Candle, OrderBookSnapshot, PriceLevel, Timeframe
from trade_core.runtime import (
    DEFAULT_INSTRUMENTS,
    LOCAL_SQLITE_ENV,
    SafeNoopBrokerGateway,
    TradeCoreRuntime,
    TradeCoreRuntimeConfig,
)
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import (
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    InstrumentRegistry,
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
        "uid-sber",
        "uid-gazp",
    )
    assert tuple(instrument.instrument_id for instrument in gateway.stream_instruments) == (
        "uid-sber",
        "uid-gazp",
    )
    with runtime.database.session_factory() as session:
        rows = session.execute(
            select(InstrumentRegistry).order_by(InstrumentRegistry.ticker)
        ).scalars()
        registry = {row.ticker: row for row in rows}
    assert registry["SBER"].instrument_id == "uid-sber"
    assert registry["SBER"].instrument_uid == "uid-sber"
    assert registry["GAZP"].instrument_id == "uid-gazp"
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

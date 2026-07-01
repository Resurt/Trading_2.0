from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    CancelOrderRequest,
    InstrumentRef,
    InstrumentResolveRequest,
    OrderPlacementRequest,
)
from trade_core.infra.tbank import (
    TBankBrokerConfig,
    TBankEnvironment,
    TBankTokenBundle,
    build_sandbox_smoke_plan,
)
from trade_core.infra.tbank.secrets import load_tbank_tokens_for_launch
from trade_core.instruments import InstrumentResolverService
from trade_core.market_data import Candle, Timeframe
from trade_core.replay import (
    ReplayCounterfactualCase,
    ReplayEvent,
    ReplayEventType,
    ReplayHarness,
)
from trade_core.session import (
    BrokerTradingStatus,
    ScheduleWindow,
    SessionManager,
    SessionSnapshot,
    TradingSchedule,
)
from trade_core.strategy import (
    CancelReasonCode,
    DefaultExecutionEngine,
    OrderIntentRequest,
    SignalAction,
    SignalCandidateDecision,
    TradeSide,
)
from trading_common import (
    PRODUCTION_CONFIRM_ENV,
    PRODUCTION_CONFIRM_VALUE,
    SANDBOX_ORDERS_CONFIRM_ENV,
    SANDBOX_ORDERS_CONFIRM_VALUE,
    LaunchModePolicy,
    RuntimeMode,
    parse_runtime_mode,
)
from trading_common.db.base import Base
from trading_common.db.models import BrokerOrder, InstrumentRegistry
from trading_common.db.repositories import OrderRepository
from trading_common.enums import SessionPhase, SessionType

MSK = ZoneInfo("Europe/Moscow")


def msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def snapshot() -> SessionSnapshot:
    now = utc(2026, 6, 12, 7)
    return SessionSnapshot(
        observed_at=now,
        calendar_date=date(2026, 6, 12),
        trading_date=date(2026, 6, 12),
        session_type=SessionType.WEEKDAY_MAIN,
        session_phase=SessionPhase.CONTINUOUS_TRADING,
        broker_phase=SessionPhase.CONTINUOUS_TRADING,
        broker_trading_status="normal_trading",
        broker_api_trade_available=True,
        schedule_phase=SessionPhase.CONTINUOUS_TRADING,
        schedule_window_start_at=now,
        schedule_window_end_at=now + timedelta(hours=1),
        micro_session_id="2026-06-12:weekday_main:0700",
        is_trading_allowed=True,
        deny_reason_code=None,
        status_mismatch=False,
    )


def candidate() -> SignalCandidateDecision:
    return SignalCandidateDecision(
        strategy_id="baseline_config_stub",
        strategy_version=1,
        instrument=InstrumentRef(
            instrument_id="MOEX:SBER",
            instrument_uid="uid-sber",
            class_code="TQBR",
            ticker="SBER",
        ),
        timeframe=Timeframe.M5,
        action=SignalAction.ENTRY,
        side=TradeSide.BUY,
        order_type="limit",
        lot_qty=1,
        intended_price=Decimal("100.00"),
        time_in_force="day",
        expected_edge_bps=Decimal("25"),
        expected_holding_minutes=5,
        signal_fingerprint="controlled-launch-candidate",
        condition_payload={"test": True},
        candidate_id=uuid4(),
    )


class FakeBrokerGateway:
    def __init__(self) -> None:
        self.posted: list[OrderPlacementRequest] = []
        self.cancelled: list[CancelOrderRequest] = []

    async def post_order(
        self,
        request: OrderPlacementRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.posted.append(request)
        return BrokerUnaryResponse(
            method_name="PostOrder",
            data={"exchange_order_id": "exchange-1", "broker_status": "posted"},
            headers={"x-tracking-id": "tracking-post"},
        )

    async def cancel_order(
        self,
        request: CancelOrderRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.cancelled.append(request)
        return BrokerUnaryResponse(
            method_name="CancelOrder",
            data={"exchange_order_id": request.exchange_order_id, "broker_status": "cancelled"},
        )


def test_launch_mode_default_and_production_confirmation() -> None:
    assert parse_runtime_mode(None) is RuntimeMode.HISTORICAL_REPLAY
    replay = LaunchModePolicy.from_env({})

    assert replay.mode is RuntimeMode.HISTORICAL_REPLAY
    assert replay.allows_real_orders is False
    assert replay.uses_pseudo_orders is True

    with pytest.raises(RuntimeError, match="production mode requires"):
        LaunchModePolicy.from_env({"TRADING_RUNTIME_MODE": "production"})

    production = LaunchModePolicy.from_env(
        {
            "TRADING_RUNTIME_MODE": "production",
            PRODUCTION_CONFIRM_ENV: PRODUCTION_CONFIRM_VALUE,
        }
    )
    assert production.allows_real_orders is True
    assert production.requires_full_access_token is True

    sandbox_default = LaunchModePolicy.from_env({"TRADING_RUNTIME_MODE": "sandbox"})
    sandbox_confirmed = LaunchModePolicy.from_env(
        {
            "TRADING_RUNTIME_MODE": "sandbox",
            SANDBOX_ORDERS_CONFIRM_ENV: SANDBOX_ORDERS_CONFIRM_VALUE,
        }
    )
    assert sandbox_default.allows_real_orders is False
    assert sandbox_default.order_submission_mode == "sandbox_pseudo_order"
    assert sandbox_default.real_order_block_reason_code == "sandbox_orders_not_confirmed"
    assert sandbox_confirmed.allows_real_orders is True


def test_tbank_config_and_secret_policy_follow_launch_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox_policy = LaunchModePolicy.from_mode(RuntimeMode.SANDBOX)
    sandbox_config = TBankBrokerConfig.from_launch_policy(sandbox_policy)
    shadow_config = TBankBrokerConfig.from_launch_policy(
        LaunchModePolicy.from_mode(RuntimeMode.SHADOW)
    )

    assert sandbox_config.environment is TBankEnvironment.SANDBOX
    assert sandbox_config.target == sandbox_config.sandbox_target
    assert shadow_config.environment is TBankEnvironment.LIVE

    with pytest.raises(RuntimeError, match="does not use"):
        TBankBrokerConfig.from_launch_policy(
            LaunchModePolicy.from_mode(RuntimeMode.HISTORICAL_REPLAY)
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TBANK_FULL_ACCESS_TOKEN_FILE", raising=False)
    monkeypatch.delenv("TBANK_READONLY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("TBANK_FULL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TBANK_READONLY_TOKEN", raising=False)
    monkeypatch.setenv("TINVEST_TOKEN", "dev-token")
    tokens = load_tbank_tokens_for_launch(sandbox_policy)
    assert tokens.full_access_token == "dev-token"

    monkeypatch.delenv("TINVEST_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="full-access"):
        load_tbank_tokens_for_launch(sandbox_policy)

    production_policy = LaunchModePolicy.from_mode(
        RuntimeMode.PRODUCTION,
        production_confirmed=True,
    )
    monkeypatch.setenv("TBANK_FULL_ACCESS_TOKEN", "env-prod-token")
    monkeypatch.setenv("TBANK_READONLY_TOKEN", "env-read-token")
    monkeypatch.setenv("TBANK_FULL_ACCESS_TOKEN_FILE", str(tmp_path / "missing-full"))
    monkeypatch.setenv("TBANK_READONLY_TOKEN_FILE", str(tmp_path / "missing-readonly"))
    with pytest.raises(RuntimeError, match="full-access"):
        load_tbank_tokens_for_launch(production_policy)

    full_token_file = tmp_path / "full-token"
    readonly_token_file = tmp_path / "readonly-token"
    full_token_file.write_text("file-full-token", encoding="utf-8")
    readonly_token_file.write_text("file-read-token", encoding="utf-8")
    monkeypatch.setenv("TBANK_FULL_ACCESS_TOKEN_FILE", str(full_token_file))
    monkeypatch.setenv("TBANK_READONLY_TOKEN_FILE", str(readonly_token_file))
    production_tokens = load_tbank_tokens_for_launch(production_policy)

    assert production_tokens.full_access_token == "file-full-token"
    assert production_tokens.readonly_token == "file-read-token"


def test_shadow_mode_records_pseudo_order_without_broker_post() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = FakeBrokerGateway()

    with Session(engine) as session:
        execution = DefaultExecutionEngine(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=OrderRepository(session),
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        )
        intent = execution.create_order_intent(
            OrderIntentRequest(candidate=candidate(), session_snapshot=snapshot(), account_id="a1")
        )
        result = asyncio.run(execution.post_order(intent))
        stored_broker_order = session.execute(select(BrokerOrder)).scalar_one()

        assert result.broker_status == "pseudo_posted"
        assert intent.status == "pseudo_submitted"
        assert fake_gateway.posted == []
        assert stored_broker_order.posted_at is not None
        broker_payload_data = cast(dict[str, object], stored_broker_order.broker_payload["data"])
        assert broker_payload_data["real_broker_call"] is False

        cancel_result = asyncio.run(
            execution.cancel_order(
                intent,
                account_id="a1",
                cancel_reason_code=CancelReasonCode.STALE_ORDER,
                cancel_payload={"source": "shadow_smoke"},
            )
        )
        assert cancel_result.broker_status == "cancelled"
        assert intent.cancel_reason_code == "stale_order"
        assert fake_gateway.cancelled == []

    engine.dispose()


def test_instrument_resolver_uses_cached_registry_when_broker_resolver_times_out() -> None:
    class TimeoutResolveGateway:
        def __init__(self) -> None:
            self.requests: list[InstrumentResolveRequest] = []

        async def resolve_instruments(
            self,
            request: InstrumentResolveRequest,
            metadata: object | None = None,
        ) -> BrokerUnaryResponse:
            del metadata
            self.requests.append(request)
            raise TimeoutError("ShareBy timeout")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = TimeoutResolveGateway()

    with Session(engine) as session:
        session.add_all(
            [
                InstrumentRegistry(
                    instrument_id="MOEX:SBER",
                    ticker="SBER",
                    class_code="TQBR",
                    figi="figi-sber",
                    instrument_uid="uid-sber",
                    name="SBER",
                    lot_size=10,
                    min_price_increment=Decimal("0.01"),
                    currency="RUB",
                    is_enabled=True,
                    supports_morning=True,
                    supports_evening=True,
                    supports_weekend=False,
                    source="tbank_resolved",
                    resolution_status="resolved",
                    instrument_payload={},
                ),
                InstrumentRegistry(
                    instrument_id="MOEX:GAZP",
                    ticker="GAZP",
                    class_code="TQBR",
                    figi="figi-gazp",
                    instrument_uid="uid-gazp",
                    name="GAZP",
                    lot_size=10,
                    min_price_increment=Decimal("0.01"),
                    currency="RUB",
                    is_enabled=True,
                    supports_morning=True,
                    supports_evening=True,
                    supports_weekend=False,
                    source="tbank_resolved",
                    resolution_status="resolved",
                    instrument_payload={},
                ),
            ]
        )
        session.flush()
        service = InstrumentResolverService(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            session=session,
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        )
        resolved = asyncio.run(
            service.resolve_startup_instruments(
                (
                    InstrumentRef(instrument_id="MOEX:SBER", ticker="SBER", class_code="TQBR"),
                    InstrumentRef(instrument_id="MOEX:GAZP", ticker="GAZP", class_code="TQBR"),
                )
            )
        )

    assert fake_gateway.requests[0].tickers == ("SBER", "GAZP")
    assert [instrument.instrument_uid for instrument in resolved] == ["uid-sber", "uid-gazp"]
    assert [instrument.instrument_id for instrument in resolved] == ["MOEX:SBER", "MOEX:GAZP"]
    engine.dispose()


def test_sandbox_mode_allows_gateway_post_only_after_explicit_confirmation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = FakeBrokerGateway()

    with Session(engine) as session:
        execution = DefaultExecutionEngine(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=OrderRepository(session),
            launch_policy=LaunchModePolicy.from_mode(
                RuntimeMode.SANDBOX,
                sandbox_orders_confirmed=True,
            ),
        )
        intent = execution.create_order_intent(
            OrderIntentRequest(candidate=candidate(), session_snapshot=snapshot(), account_id="a1")
        )
        result = asyncio.run(execution.post_order(intent))

        assert result.broker_status == "posted"
        assert fake_gateway.posted[0].request_order_id == intent.request_order_id

    engine.dispose()


def test_replay_harness_verifies_rollover_blocker_and_counterfactual_pipeline() -> None:
    manager = SessionManager()
    schedule = TradingSchedule(
        windows=(
            ScheduleWindow(
                session_type=SessionType.WEEKDAY_MAIN,
                session_phase=SessionPhase.CONTINUOUS_TRADING,
                start_at=msk(2026, 6, 12, 10),
                end_at=msk(2026, 6, 12, 12),
                trading_date=date(2026, 6, 12),
                calendar_date=date(2026, 6, 12),
            ),
        )
    )
    broker_status = BrokerTradingStatus(status="normal_trading", api_trade_available=True)

    def counterfactual_callback(
        cases: Sequence[ReplayCounterfactualCase],
        candles: Sequence[Candle],
    ) -> list[dict[str, object]]:
        return [
            {
                "source_event_type": cases[0].source_event_type,
                "candle_count": len(candles),
                "would_profit_5m": True,
            }
        ]

    events: list[ReplayEvent] = [
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 15).astimezone(UTC),
            event_type=ReplayEventType.SESSION_SNAPSHOT,
            payload=manager.evaluate(
                now=msk(2026, 6, 12, 10, 15),
                schedule=schedule,
                broker_status=broker_status,
            ),
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 59).astimezone(UTC),
            event_type=ReplayEventType.SESSION_SNAPSHOT,
            payload=manager.evaluate(
                now=msk(2026, 6, 12, 10, 59),
                schedule=schedule,
                broker_status=broker_status,
            ),
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 11).astimezone(UTC),
            event_type=ReplayEventType.SESSION_SNAPSHOT,
            payload=manager.evaluate(
                now=msk(2026, 6, 12, 11),
                schedule=schedule,
                broker_status=broker_status,
            ),
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 56).astimezone(UTC),
            event_type=ReplayEventType.BLOCKER_TRIGGERED,
            payload={"reason_code": "spread_too_wide"},
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 56).astimezone(UTC),
            event_type=ReplayEventType.COUNTERFACTUAL_SOURCE,
            payload=ReplayCounterfactualCase(
                source_event_type="blocked_candidate",
                instrument_id="MOEX:SBER",
                strategy_id="baseline",
                side="buy",
                event_ts=msk(2026, 6, 12, 10, 56).astimezone(UTC),
                entry_price=Decimal("100"),
                lot_qty=1,
                blocker_code="spread_too_wide",
            ),
        ),
    ]
    events.extend(replay_candles())

    result = ReplayHarness(counterfactual_callback=counterfactual_callback).run(events)

    assert result.session_rollover_verified
    assert result.blocker_pipeline_verified
    assert result.counterfactual_pipeline_verified
    assert len(result.closed_bars) >= 1


def test_sandbox_smoke_plan_is_dry_run_and_not_live_execution_quality() -> None:
    policy = LaunchModePolicy.from_mode(RuntimeMode.SANDBOX)
    config = TBankBrokerConfig.from_launch_policy(policy)
    plan = build_sandbox_smoke_plan(
        policy=policy,
        config=config,
        tokens=TBankTokenBundle(full_access_token=None, readonly_token=None),
        dry_run=True,
    )

    assert plan.target == config.sandbox_target
    assert plan.full_access_token_configured is False
    assert "not real execution-quality evidence" in str(plan.as_payload()["note"])


def replay_candles() -> list[ReplayEvent]:
    events: list[ReplayEvent] = []
    start = msk(2026, 6, 12, 10, 56)
    for offset in range(5):
        open_ts = start + timedelta(minutes=offset)
        close_ts = open_ts + timedelta(minutes=1)
        candle = Candle(
            instrument_id="MOEX:SBER",
            timeframe=Timeframe.M1,
            open_ts_utc=open_ts.astimezone(UTC),
            close_ts_utc=close_ts.astimezone(UTC),
            exchange_open_ts=open_ts,
            exchange_close_ts=close_ts,
            open_price=Decimal("100"),
            high_price=Decimal("101"),
            low_price=Decimal("99.5"),
            close_price=Decimal("100.5"),
            volume_lots=Decimal("10"),
            is_closed=True,
            source="test_replay",
        )
        events.append(
            ReplayEvent(
                ts_utc=close_ts.astimezone(UTC),
                event_type=ReplayEventType.CANDLE,
                payload=candle,
            )
        )
    return events

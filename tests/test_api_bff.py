from __future__ import annotations

import asyncio
import importlib
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Executable, select
from sqlalchemy.exc import OperationalError
from starlette.websockets import WebSocketDisconnect

from trade_core.broker_gateway import BrokerUnaryResponse
from trade_core.session.moex_calendar import MoexCalendarDecision
from trading_api import create_fastapi_app
from trading_api.read_service import BffReadService
from trading_api.schemas import (
    DailyReportRunRequest,
    MarketInstrumentOverview,
    MarketOverviewResponse,
    ReportJobResponse,
    ReportJobStatusResponse,
    ReportRebuildRequest,
    SessionPreflightResponse,
    SessionSnapshotResponse,
)
from trading_common import RuntimeMode
from trading_common.db.base import Base
from trading_common.db.models import (
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    CounterfactualResult,
    DailyReport,
    DividendSyncRun,
    FillEvent,
    HourlyReport,
    InstrumentRegistry,
    MarketCandle,
    MarketMicrostructureSnapshot,
    MarketTradeSample,
    OrderBookSummary,
    OrderIntent,
    PositionSnapshot,
    RobotCommand,
    SessionRun,
    SignalCandidate,
    StrategyConfig,
    StrategyStateEvent,
)
from trading_common.db.service import DatabaseService


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class ClosedMoexCalendarService:
    def decision(
        self,
        calendar_date: date,
        *,
        market: str = "stock",
        now_msk: datetime | None = None,
    ) -> MoexCalendarDecision:
        del now_msk
        return MoexCalendarDecision(
            official_exchange_open=False,
            official_exchange_closed=True,
            exchange="MOEX",
            market=market,
            calendar_date=calendar_date,
            session_type="weekend",
            reason_code="moex_dsvd_cancelled_platform_update",
            source="test_closed_calendar",
            message="test exchange closed",
            next_possible_session_at=utc(2026, 6, 22, 4),
            affected_markets=(market,),
            is_exception_day=True,
        )


class OpenMoexCalendarService:
    def decision(
        self,
        calendar_date: date,
        *,
        market: str = "stock",
        now_msk: datetime | None = None,
    ) -> MoexCalendarDecision:
        del now_msk
        return MoexCalendarDecision(
            official_exchange_open=True,
            official_exchange_closed=False,
            exchange="MOEX",
            market=market,
            calendar_date=calendar_date,
            session_type="weekday_main",
            reason_code="market_open",
            source="test_open_calendar",
            message="test exchange open",
            next_possible_session_at=None,
            affected_markets=(market,),
            is_exception_day=False,
        )


def force_exchange_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    read_service_module = importlib.import_module("trading_api.read_service")
    monkeypatch.setattr(
        read_service_module,
        "MoexCalendarService",
        ClosedMoexCalendarService,
    )


def force_exchange_open(monkeypatch: pytest.MonkeyPatch) -> None:
    read_service_module = importlib.import_module("trading_api.read_service")
    monkeypatch.setattr(
        read_service_module,
        "MoexCalendarService",
        OpenMoexCalendarService,
    )


class FakeReportTaskClient:
    def enqueue_daily_report(self, request: DailyReportRunRequest) -> ReportJobResponse:
        return self.enqueue_report_rebuild(
            ReportRebuildRequest(
                trading_date=request.trading_date,
                strategy_id=request.strategy_id,
                include_counterfactual=request.include_counterfactual,
            )
        )

    def enqueue_report_rebuild(self, request: ReportRebuildRequest) -> ReportJobResponse:
        task_name = (
            "report_worker.build_hourly_report"
            if request.scope == "hourly"
            else "report_worker.rebuild_reports_for_date"
        )
        return ReportJobResponse(
            job_id="job-1",
            task_name=task_name,
            status="queued",
            payload={
                "trading_date": request.trading_date.isoformat(),
                "strategy_id": request.strategy_id,
                "scope": request.scope,
                "include_counterfactual": request.include_counterfactual,
            },
        )

    def job_status(self, job_id: str) -> ReportJobStatusResponse:
        return ReportJobStatusResponse(
            job_id=job_id,
            task_name="report_worker.rebuild_reports_for_date",
            status="success",
            ready=True,
            successful=True,
            failed=False,
            result={"ok": True},
            payload={},
        )


class FakeBalanceGateway:
    async def get_accounts(self, request: object) -> BrokerUnaryResponse:
        del request
        return BrokerUnaryResponse(
            method_name="GetAccounts",
            data={
                "accounts": [
                    {
                        "account_id": "account-123456",
                        "type": "broker",
                        "status": "open",
                    }
                ]
            },
        )

    async def get_positions(self, request: object, metadata: object = None) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetPositions",
            data={
                "account_id": "account-123456",
                "money": [{"currency": "RUB", "units": 125000, "nano": 0}],
                "blocked": [{"currency": "RUB", "units": 500, "nano": 0}],
                "positions": [],
            },
        )

    async def get_portfolio(self, request: object, metadata: object = None) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetPortfolio",
            data={
                "account_id": "account-123456",
                "positions": [],
                "total_amount_portfolio": "220000",
                "expected_yield": "1234",
                "available_margin": "75000",
            },
        )


class FakeQuoteGateway:
    async def get_last_prices(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(method_name="GetLastPrices", data={"prices": []})

    async def get_order_book(self, request: object, metadata: object = None) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetOrderBook",
            data={
                "instrument_uid": "uid-sber",
                "exchange_ts": "2026-06-21T10:00:00+00:00",
                "bids": [
                    {"price": "312.98", "quantity_lots": "19"},
                    {"price": "312.95", "quantity_lots": "306"},
                ],
                "asks": [
                    {"price": "313.40", "quantity_lots": "195"},
                    {"price": "313.45", "quantity_lots": "637"},
                ],
            },
        )

    async def get_last_trades(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetLastTrades",
            data={
                "trades": [
                    {
                        "instrument_uid": "uid-sber",
                        "figi": "figi-sber",
                        "price": "313.20",
                        "quantity_lots": "5",
                        "side": "buy",
                        "ts_utc": "2026-06-21T10:00:02+00:00",
                    }
                ]
            },
        )


class FakeDashboardFeedGateway(FakeQuoteGateway):
    async def get_last_prices(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetLastPrices",
            data={
                "prices": [
                    {
                        "instrument_uid": "uid-sber",
                        "figi": "figi-sber",
                        "price": "313.21",
                        "exchange_ts": "2026-06-21T10:00:01+00:00",
                    }
                ]
            },
        )

    async def get_trading_status(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetTradingStatus",
            data={
                "trading_status": "normal_trading",
                "status": "normal_trading",
                "api_trade_available": True,
            },
        )


class FakeCoreDashboardFeedGateway(FakeDashboardFeedGateway):
    def __init__(self) -> None:
        self.order_book_calls: list[str] = []

    async def get_last_prices(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del metadata
        prices = []
        now = datetime.now(tz=UTC).isoformat()
        for ref in getattr(request, "instruments", ()):
            ticker = str(getattr(ref, "ticker", "") or "").upper()
            price = _test_price_for_ticker(ticker)
            prices.append(
                {
                    "instrument_uid": getattr(ref, "instrument_uid", None),
                    "figi": getattr(ref, "figi", None),
                    "price": str(price),
                    "exchange_ts": now,
                }
            )
        return BrokerUnaryResponse(method_name="GetLastPrices", data={"prices": prices})

    async def get_order_book(self, request: object, metadata: object = None) -> BrokerUnaryResponse:
        del metadata
        ref = request.instrument  # type: ignore[attr-defined]
        ticker = str(getattr(ref, "ticker", "") or "").upper()
        self.order_book_calls.append(str(getattr(ref, "instrument_id", "")))
        mid = _test_price_for_ticker(ticker)
        bids = [
            {
                "price": str((mid - Decimal("0.01") * (index + 1)).quantize(Decimal("0.01"))),
                "quantity_lots": str(100 + index),
            }
            for index in range(10)
        ]
        asks = [
            {
                "price": str((mid + Decimal("0.01") * (index + 1)).quantize(Decimal("0.01"))),
                "quantity_lots": str(120 + index),
            }
            for index in range(10)
        ]
        return BrokerUnaryResponse(
            method_name="GetOrderBook",
            data={
                "instrument_uid": getattr(ref, "instrument_uid", None),
                "figi": getattr(ref, "figi", None),
                "exchange_ts": datetime.now(tz=UTC).isoformat(),
                "bids": bids,
                "asks": asks,
            },
        )

    async def get_last_trades(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(method_name="GetLastTrades", data={"trades": []})


class FakePartialOrderBookGateway(FakeDashboardFeedGateway):
    def __init__(self) -> None:
        self.order_book_calls = 0

    async def get_order_book(self, request: object, metadata: object = None) -> BrokerUnaryResponse:
        del request, metadata
        self.order_book_calls += 1
        if self.order_book_calls == 1:
            bids = [
                {"price": f"{100 - index * 0.01:.2f}", "quantity_lots": str(100 + index)}
                for index in range(5)
            ]
            asks = [
                {"price": f"{100.10 + index * 0.01:.2f}", "quantity_lots": str(80 + index)}
                for index in range(5)
            ]
        else:
            bids = [{"price": "100.01", "quantity_lots": "7"}]
            asks = [{"price": "100.11", "quantity_lots": "8"}]
        return BrokerUnaryResponse(
            method_name="GetOrderBook",
            data={
                "instrument_uid": "uid-sber",
                "exchange_ts": datetime.now(tz=UTC).isoformat(),
                "bids": bids,
                "asks": asks,
            },
        )


class FakeOneLevelOrderBookGateway(FakeDashboardFeedGateway):
    async def get_order_book(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetOrderBook",
            data={
                "instrument_uid": "uid-sber",
                "exchange_ts": datetime.now(tz=UTC).isoformat(),
                "bids": [{"price": "100.01", "quantity_lots": "7"}],
                "asks": [{"price": "100.11", "quantity_lots": "8"}],
            },
        )


class FakeOldFirstTradesGateway(FakeDashboardFeedGateway):
    async def get_last_trades(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        now = datetime.now(tz=UTC)
        return BrokerUnaryResponse(
            method_name="GetLastTrades",
            data={
                "trades": [
                    {
                        "instrument_uid": "uid-sber",
                        "figi": "figi-sber",
                        "price": "312.00",
                        "quantity_lots": "1",
                        "side": "sell",
                        "ts_utc": (now - timedelta(minutes=29)).isoformat(),
                    },
                    {
                        "instrument_uid": "uid-sber",
                        "figi": "figi-sber",
                        "price": "314.01",
                        "quantity_lots": "3",
                        "side": "buy",
                        "ts_utc": (now - timedelta(seconds=1)).isoformat(),
                    },
                ]
            },
        )


class FakeDelayedTradesGateway(FakeDashboardFeedGateway):
    async def get_last_trades(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        now = datetime.now(tz=UTC)
        return BrokerUnaryResponse(
            method_name="GetLastTrades",
            data={
                "trades": [
                    {
                        "instrument_uid": "uid-sber",
                        "figi": "figi-sber",
                        "price": "313.30",
                        "quantity_lots": "7",
                        "side": "buy",
                        "ts_utc": (now - timedelta(seconds=30)).isoformat(),
                    }
                ]
            },
        )


class FakeLongDelayedTradesGateway(FakeDashboardFeedGateway):
    async def get_last_trades(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        now = datetime.now(tz=UTC)
        return BrokerUnaryResponse(
            method_name="GetLastTrades",
            data={
                "trades": [
                    {
                        "instrument_uid": "uid-sber",
                        "figi": "figi-sber",
                        "price": "313.25",
                        "quantity_lots": "11",
                        "side": "sell",
                        "ts_utc": (now - timedelta(minutes=2)).isoformat(),
                    }
                ]
            },
        )


class FakeIntermittentTradesGateway(FakeDashboardFeedGateway):
    def __init__(self) -> None:
        self.trade_calls = 0

    async def get_last_trades(
        self,
        request: object,
        metadata: object = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        self.trade_calls += 1
        now = datetime.now(tz=UTC)
        trades: list[dict[str, object]]
        if self.trade_calls == 1:
            trades = [
                {
                    "instrument_uid": "uid-sber",
                    "figi": "figi-sber",
                    "price": "313.40",
                    "quantity_lots": "9",
                    "side": "buy",
                    "ts_utc": (now - timedelta(seconds=2)).isoformat(),
                }
            ]
        else:
            trades = []
        return BrokerUnaryResponse(method_name="GetLastTrades", data={"trades": trades})


def make_client(tmp_path: Path) -> TestClient:
    os.environ.setdefault("DASHBOARD_MARKET_FEED_ENABLED", "false")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'api-bff.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    return TestClient(app)


def session_context() -> dict[str, object]:
    return {
        "calendar_date": date(2026, 6, 12),
        "trading_date": date(2026, 6, 12),
        "session_type": "weekday_main",
        "session_phase": "continuous_trading",
        "micro_session_id": "2026-06-12:weekday_main:1000",
        "broker_trading_status": "normal_trading",
    }


def preflight_response(*, market_open: bool, reason_code: str) -> SessionPreflightResponse:
    official_exchange_closed = reason_code == "moex_dsvd_cancelled_platform_update"
    return SessionPreflightResponse(
        market_open=market_open,
        market_closed_expected=not market_open,
        now_msk=utc(2026, 6, 20, 19),
        trading_date=date(2026, 6, 20),
        calendar_date=date(2026, 6, 20),
        session_type="weekend" if not market_open else "weekday_main",
        session_phase="closed" if not market_open else "continuous_trading",
        broker_trading_status="closed" if not market_open else "normal_trading",
        api_trade_available=market_open,
        official_exchange_open=market_open and not official_exchange_closed,
        official_exchange_closed=official_exchange_closed,
        official_exchange_reason_code=reason_code if official_exchange_closed else None,
        official_exchange_source=(
            "official_moex_news_2026_06_17"
            if official_exchange_closed
            else "test_preflight"
        ),
        broker_stream_available=official_exchange_closed,
        broker_otc_or_indicative_available=official_exchange_closed,
        api_trade_available_raw=market_open or official_exchange_closed,
        api_trade_available_for_exchange=market_open and not official_exchange_closed,
        quote_source_allowed_for_data_collection=market_open and not official_exchange_closed,
        data_only_collection_allowed=market_open and not official_exchange_closed,
        streams_for_display_allowed=True,
        streams_for_calibration_allowed=market_open and not official_exchange_closed,
        venue_type="broker_otc" if official_exchange_closed else "official_exchange",
        trading_mode="broker_otc_only" if official_exchange_closed else "standard_exchange",
        broker_availability_ignored_because_official_exchange_closed=official_exchange_closed,
        next_session_at=utc(2026, 6, 21, 7) if not market_open else None,
        next_session_type="weekend" if not market_open else None,
        current_window_start_at=None,
        current_window_end_at=None,
        reason_code=reason_code,
        source="test_preflight",
        instruments_checked=["MOEX:SBER"],
        per_instrument_status={},
        warnings=[],
    )


def seed_database(database: DatabaseService) -> None:
    now = utc(2026, 6, 12, 7)
    candidate_id = uuid4()
    request_order_id = uuid4()
    order_intent_id = uuid4()
    canceled_request_order_id = uuid4()
    canceled_order_intent_id = uuid4()

    with database.session_scope() as session:
        session.add_all(
            [
                InstrumentRegistry(
                    instrument_id="MOEX:SBER",
                    ticker="SBER",
                    class_code="TQBR",
                    figi=None,
                    instrument_uid="uid-sber",
                    name="Sberbank ordinary shares",
                    lot_size=10,
                    min_price_increment=Decimal("0.01"),
                    currency="RUB",
                    is_enabled=True,
                    supports_morning=True,
                    supports_evening=True,
                    supports_weekend=False,
                    source="tbank_resolved",
                    resolved_at=now,
                    resolution_status="resolved",
                    broker_payload={"source": "test"},
                    instrument_payload={},
                ),
                SessionRun(
                    **session_context(),
                    strategy_id="baseline",
                    strategy_version=1,
                    status="open",
                    started_at=now,
                    ended_at=None,
                    freeze_started_at=None,
                    report_requested_at=None,
                    close_reason_code=None,
                    run_payload={},
                ),
                StrategyStateEvent(
                    **session_context(),
                    ts_utc=now,
                    exchange_ts=None,
                    received_ts=None,
                    strategy_id="baseline",
                    strategy_version=1,
                    instrument_id="MOEX:SBER",
                    previous_state="wait",
                    new_state="candidate",
                    event_type="strategy_state_changed",
                    reason_code=None,
                    state_payload={},
                ),
                PositionSnapshot(
                    **session_context(),
                    snapshot_ts=now,
                    instrument_id="MOEX:SBER",
                    account_id="account-1",
                    position_side="long",
                    qty_lots=10,
                    avg_price=Decimal("100"),
                    market_price=Decimal("101"),
                    unrealized_pnl=Decimal("10"),
                    realised_pnl=Decimal("0"),
                    exposure=Decimal("1010"),
                    snapshot_reason="test",
                    snapshot_payload={
                        "broker_balance": {
                            "account_id_masked": "acc***t-1",
                            "balance_currency": "RUB",
                            "total_portfolio_value_rub": "150000",
                            "available_cash_rub": "120000",
                            "blocked_cash_rub": "1000",
                            "expected_yield_rub": "2500",
                            "free_collateral_rub": "90000",
                            "last_balance_refresh_at": now.isoformat(),
                            "source": "test_broker_payload",
                        }
                    },
                ),
                SignalCandidate(
                    **session_context(),
                    candidate_id=candidate_id,
                    ts_utc=now,
                    exchange_ts=None,
                    received_ts=None,
                    run_id=None,
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    strategy_version=1,
                    timeframe="5m",
                    side="buy",
                    signal_type="entry",
                    candidate_status="blocked",
                    expected_edge_bps=Decimal("20"),
                    expected_holding_minutes=5,
                    last_price=Decimal("100"),
                    mid_price=Decimal("100"),
                    spread_abs=Decimal("0.1"),
                    spread_bps=Decimal("10"),
                    market_quality_score=Decimal("0.9"),
                    book_imbalance=Decimal("0"),
                    candle_age_ms=100,
                    data_freshness_ms=100,
                    signal_fingerprint="sig",
                    signal_payload={
                        "reason": "spread exceeded threshold",
                        "explanation": "spread gate failed",
                    },
                ),
                CandidateStageResult(
                    **session_context(),
                    ts_utc=now,
                    exchange_ts=None,
                    received_ts=None,
                    candidate_id=candidate_id,
                    instrument_id="MOEX:SBER",
                    timeframe="5m",
                    strategy_id="baseline",
                    strategy_version=1,
                    stage_seq=1,
                    stage_name="spread_gate",
                    stage_outcome="blocked",
                    passed=False,
                    blocker_code="spread_too_wide",
                    blocker_family="market_quality",
                    measured_value=Decimal("10"),
                    threshold_value=Decimal("5"),
                    explanation_payload={"summary": "spread above configured threshold"},
                ),
                BlockerEvent(
                    **session_context(),
                    ts_utc=now,
                    exchange_ts=None,
                    received_ts=None,
                    candidate_id=candidate_id,
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    gate_name="spread_limit",
                    gate_rank=1,
                    stage_seq=1,
                    stage_name="spread_gate",
                    stage_outcome="blocked",
                    passed=False,
                    reason_code="spread_too_wide",
                    blocker_code="spread_too_wide",
                    blocker_family="market_quality",
                    measured_value=Decimal("10"),
                    threshold_value=Decimal("5"),
                    reason_payload={"summary": "spread above threshold"},
                    explanation_payload={"summary": "spread above configured threshold"},
                    is_final_blocker=True,
                    blocker_rank=1,
                    market_quality_score=Decimal("0.9"),
                    spread_bps=Decimal("10"),
                    expected_edge_bps=Decimal("20"),
                ),
                OrderIntent(
                    **session_context(),
                    order_intent_id=order_intent_id,
                    candidate_id=candidate_id,
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    side="buy",
                    order_action="place",
                    order_type="limit",
                    lot_qty=10,
                    intended_price=Decimal("100"),
                    time_in_force="day",
                    request_order_id=request_order_id,
                    idempotency_key="baseline:test",
                    execution_policy_version=1,
                    status="submitted",
                    cancel_reason_code=None,
                    reject_reason_code=None,
                    created_ts=now,
                    submitted_ts=now + timedelta(seconds=1),
                    terminal_ts=None,
                    intent_payload={},
                ),
                BrokerOrder(
                    **session_context(),
                    order_intent_id=order_intent_id,
                    request_order_id=request_order_id,
                    exchange_order_id="exchange-1",
                    broker_status="posted",
                    lifecycle_seq=1,
                    posted_at=now,
                    cancelled_at=None,
                    rejected_at=None,
                    reject_reason_code=None,
                    broker_tracking_id="tracking",
                    last_observed_at=now + timedelta(seconds=1),
                    broker_payload={},
                ),
                FillEvent(
                    **session_context(),
                    ts_utc=now + timedelta(seconds=2),
                    exchange_ts=None,
                    received_ts=None,
                    candidate_id=candidate_id,
                    order_intent_id=order_intent_id,
                    request_order_id=request_order_id,
                    exchange_order_id="exchange-1",
                    tracking_id="tracking",
                    broker_fill_id="fill-1",
                    instrument_id="MOEX:SBER",
                    timeframe="5m",
                    side="buy",
                    lot_qty=10,
                    price=Decimal("100.02"),
                    commission=Decimal("0.5"),
                    commission_gross=Decimal("0.5"),
                    commission_net=Decimal("0.5"),
                    slippage_bp=Decimal("2"),
                    pnl_gross=Decimal("12"),
                    pnl_net=Decimal("11.5"),
                    liquidity_flag="maker",
                    fill_payload={},
                ),
                OrderIntent(
                    **session_context(),
                    order_intent_id=canceled_order_intent_id,
                    candidate_id=candidate_id,
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    strategy_version=1,
                    timeframe="5m",
                    side="buy",
                    order_action="cancel",
                    order_type="limit",
                    lot_qty=10,
                    intended_price=Decimal("99.9"),
                    time_in_force="day",
                    request_order_id=canceled_request_order_id,
                    idempotency_key="baseline:test:cancel",
                    execution_policy_version=1,
                    status="cancelled",
                    cancel_reason_code="stale_order",
                    reject_reason_code=None,
                    created_ts=now,
                    submitted_ts=now + timedelta(seconds=3),
                    terminal_ts=now + timedelta(seconds=4),
                    intent_payload={"summary": "limit order became stale"},
                ),
                OrderBookSummary(
                    **session_context(),
                    ts_utc=now,
                    exchange_ts=now,
                    received_ts=now,
                    instrument_id="MOEX:SBER",
                    depth_levels=2,
                    best_bid_price=Decimal("100"),
                    best_bid_qty_lots=Decimal("10"),
                    best_ask_price=Decimal("100.1"),
                    best_ask_qty_lots=Decimal("8"),
                    mid_price=Decimal("100.05"),
                    spread_abs=Decimal("0.1"),
                    spread_bps=Decimal("9.995"),
                    bid_depth_lots=Decimal("20"),
                    ask_depth_lots=Decimal("16"),
                    book_imbalance=Decimal("0.1111"),
                    market_quality_score=Decimal("0.9"),
                    summary_payload={
                        "bids": [
                            {"price": "100", "quantity_lots": "10"},
                            {"price": "99.9", "quantity_lots": "10"},
                        ],
                        "asks": [
                            {"price": "100.1", "quantity_lots": "8"},
                            {"price": "100.2", "quantity_lots": "8"},
                        ],
                        "recent_market_trades": [
                            {"side": "buy", "price": "100.04", "qty_lots": 5}
                        ]
                    },
                ),
                MarketMicrostructureSnapshot(
                    **session_context(),
                    ts_utc=now,
                    exchange_ts=now,
                    received_ts=now,
                    instrument_id="MOEX:SBER",
                    best_bid=Decimal("100"),
                    best_ask=Decimal("100.10"),
                    mid_price=Decimal("100.05"),
                    spread_abs=Decimal("0.10"),
                    spread_bps=Decimal("9.9950"),
                    bid_depth_lots=Decimal("20"),
                    ask_depth_lots=Decimal("16"),
                    book_imbalance=Decimal("0.1111"),
                    market_quality_score=Decimal("0.9000"),
                    feed_freshness_age_ms=100,
                    is_stale=False,
                    source="data_only_shadow",
                    snapshot_payload={"source": "test"},
                ),
                HourlyReport(
                    **session_context(),
                    run_id=None,
                    strategy_id="baseline",
                    instrument_id="MOEX:SBER",
                    started_at=now,
                    ended_at=now + timedelta(hours=1),
                    realised_pnl=Decimal("10"),
                    unrealised_pnl=Decimal("5"),
                    commission=Decimal("1"),
                    signal_count=1,
                    entry_count=1,
                    exit_count=0,
                    blocked_count=1,
                    reject_count=0,
                    cancel_count=0,
                    reconnect_count=0,
                    risk_event_count=1,
                    fill_ratio=Decimal("1"),
                    report_payload={
                        "format": "hourly_report_v1",
                        "risk_blockers": {"spread_too_wide": 1},
                    },
                    generated_at=now,
                ),
                DailyReport(
                    calendar_date=date(2026, 6, 12),
                    trading_date=date(2026, 6, 12),
                    session_type=None,
                    session_phase=None,
                    micro_session_id=None,
                    broker_trading_status=None,
                    strategy_id="baseline",
                    instrument_id=None,
                    market_regime="long_bias",
                    realised_pnl=Decimal("10"),
                    commission=Decimal("1"),
                    signal_count=1,
                    blocked_count=1,
                    fill_ratio=Decimal("1"),
                    report_payload={
                        "format": "daily_report_v1",
                        "trend": {
                            "market_regime": "long_bias",
                            "average_return_bps": "40",
                            "algorithm": "first_open_last_close",
                            "explanation": "close above open by more than threshold",
                        },
                        "execution_quality": {"fill_ratio": "1"},
                        "funnel": {
                            "candidates": 1,
                            "passed_gates": 0,
                            "blockers": 1,
                            "order_intent": 2,
                            "posted": 1,
                            "filled": 1,
                            "exited": 1,
                        },
                        "blocker_ranking": [
                            {
                                "reason_code": "spread_too_wide",
                                "count": 1,
                                "missed_pnl_net": "21",
                            }
                        ],
                        "canceled_order_analytics": [
                            {
                                "cancel_reason_code": "stale_order",
                                "count": 1,
                                "missed_pnl_net": "7",
                            }
                        ],
                        "summary_by_session_type": {"weekday_main": {"signal_count": 1}},
                        "summary_by_instrument": {"MOEX:SBER": {"signal_count": 1}},
                        "summary_by_timeframe": {"5m": {"signal_count": 1}},
                    },
                    generated_at=now,
                ),
                CounterfactualResult(
                    **session_context(),
                    candidate_id=candidate_id,
                    order_intent_id=None,
                    source_event_type="blocked_candidate",
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    timeframe="5m",
                    blocker_code="spread_too_wide",
                    cancel_reason_code=None,
                    fee_bps_assumed=Decimal("2"),
                    slippage_bps_assumed=Decimal("2"),
                    slippage_bp=Decimal("2"),
                    pnl_gross=Decimal("22"),
                    pnl_net=Decimal("21"),
                    mfe_5m_bps=Decimal("100"),
                    mae_5m_bps=Decimal("-50"),
                    mfe_10m_bps=Decimal("150"),
                    mae_10m_bps=Decimal("-50"),
                    mfe_15m_bps=Decimal("200"),
                    mae_15m_bps=Decimal("-50"),
                    would_profit_5m=True,
                    would_profit_10m=True,
                    would_profit_15m=True,
                    result_payload={"algorithm": "test"},
                    generated_at=now,
                ),
                CounterfactualResult(
                    **session_context(),
                    candidate_id=candidate_id,
                    order_intent_id=canceled_order_intent_id,
                    source_event_type="cancelled_order",
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    timeframe="5m",
                    blocker_code=None,
                    cancel_reason_code="stale_order",
                    fee_bps_assumed=Decimal("2"),
                    slippage_bps_assumed=Decimal("2"),
                    slippage_bp=Decimal("1"),
                    pnl_gross=Decimal("8"),
                    pnl_net=Decimal("7"),
                    mfe_5m_bps=Decimal("20"),
                    mae_5m_bps=Decimal("-10"),
                    mfe_10m_bps=Decimal("30"),
                    mae_10m_bps=Decimal("-15"),
                    mfe_15m_bps=Decimal("40"),
                    mae_15m_bps=Decimal("-20"),
                    would_profit_5m=True,
                    would_profit_10m=True,
                    would_profit_15m=True,
                    result_payload={"summary": "cancelled order would have made money"},
                    generated_at=now,
                ),
                StrategyConfig(
                    strategy_id="baseline",
                    version=1,
                    session_template="weekday_main",
                    is_active=True,
                    valid_from=now,
                    valid_to=None,
                    config_payload={"enabled": True},
                    risk_limits={"max_position_lots": 10},
                ),
            ]
        )


def seed_persisted_trade_sample(
    database: DatabaseService,
    *,
    exchange_ts: datetime,
    price: Decimal = Decimal("313.70"),
    instrument_id: str = "uid-sber",
) -> None:
    received_ts = exchange_ts + timedelta(milliseconds=100)
    with database.session_scope() as session:
        session.add(
            MarketTradeSample(
                **session_context(),
                instrument_id=instrument_id,
                exchange_ts=exchange_ts,
                received_ts=received_ts,
                price=price,
                quantity_lots=Decimal("4"),
                side="buy",
                source="tbank_get_last_trades_polling_fallback",
                venue_type="official_exchange",
                trade_id=f"trade-{price}-{int(exchange_ts.timestamp())}",
                include_in_calibration=False,
                payload={
                    "source": "tbank_get_last_trades_polling_fallback",
                    "exchange_ts": exchange_ts.isoformat(),
                    "received_ts": received_ts.isoformat(),
                },
            )
        )


CORE_TEST_TICKERS = ("SBER", "GAZP", "LKOH", "YDEX", "TATN", "GMKN", "OZON", "VTBR")


def seed_core_universe_registry(database: DatabaseService) -> None:
    now = utc(2026, 6, 12, 7)
    with database.session_scope() as session:
        for ticker in CORE_TEST_TICKERS:
            session.merge(
                InstrumentRegistry(
                    instrument_id=f"MOEX:{ticker}",
                    ticker=ticker,
                    class_code="TQBR",
                    figi=f"figi-{ticker.lower()}",
                    instrument_uid=f"uid-{ticker.lower()}",
                    name=f"{ticker} test instrument",
                    lot_size=10,
                    min_price_increment=Decimal("0.01"),
                    currency="RUB",
                    is_enabled=True,
                    supports_morning=True,
                    supports_evening=True,
                    supports_weekend=False,
                    source="tbank_resolved",
                    resolved_at=now,
                    resolution_status="resolved",
                    broker_payload={"source": "test"},
                    instrument_payload={},
                )
            )


def _test_price_for_ticker(ticker: str) -> Decimal:
    prices = {
        "SBER": Decimal("306.75"),
        "GAZP": Decimal("101.15"),
        "LKOH": Decimal("4531.75"),
        "YDEX": Decimal("3673.00"),
        "TATN": Decimal("490.05"),
        "GMKN": Decimal("127.59"),
        "OZON": Decimal("3427.25"),
        "VTBR": Decimal("73.49"),
    }
    return prices.get(ticker, Decimal("100.00"))


def test_current_session_reconciles_stale_runtime_snapshot_with_preflight(
    tmp_path: Path,
) -> None:
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'session-reconcile.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    with database.session_scope() as session:
        service = BffReadService(session)
        closed = preflight_response(market_open=False, reason_code="weekend_session_closed")
        current = service.current_session(preflight=closed)

        assert current.session_type == "weekend"
        assert current.session_phase == "closed"
        assert current.source == "fresh_preflight"
        assert current.stale is True
        assert current.stale_reason == "runtime_snapshot_mismatch"
        assert current.micro_session_id is None

        evening = preflight_response(
            market_open=True,
            reason_code="market_open",
        ).model_copy(
            update={
                "session_type": "weekday_evening",
                "session_phase": "continuous_trading",
            }
        )
        current = service.current_session(preflight=evening)
        assert current.session_type == "weekday_evening"
        assert current.session_phase == "continuous_trading"


def test_robot_status_and_market_read_model_use_preflight_gate(
    tmp_path: Path,
) -> None:
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'preflight-gate.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    with database.session_scope() as session:
        service = BffReadService(session)
        closed = preflight_response(market_open=False, reason_code="weekend_session_closed")
        status = service.robot_status(robot_control_state="stopped", preflight=closed)
        market = service.market_overview(preflight=closed, include_details=True)
        sber = next(row for row in market.instruments if row.instrument_id == "MOEX:SBER")

        assert status.session_type == closed.session_type
        assert status.session_phase == closed.session_phase
        assert status.session_source == "fresh_preflight"
        assert status.session_stale is True
        assert sber.official_exchange_open is False
        assert sber.quote_allowed_for_data_collection is False
        assert sber.calibration_market_quality_score == Decimal("0.000")
        assert sber.quote_source not in {"live_order_book_mid", "live_exchange_last_price"}
        assert sber.venue_type != "official_exchange"
        assert sber.order_book_summary["include_in_calibration"] is False


def test_robot_status_and_market_overview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def closed_preflight(*args: object, **kwargs: object) -> SessionPreflightResponse:
        del args, kwargs
        return preflight_response(
            market_open=False,
            reason_code="moex_dsvd_cancelled_platform_update",
        )

    async def dashboard_preflight(*args: object, **kwargs: object) -> SessionPreflightResponse:
        del args, kwargs
        return preflight_response(
            market_open=True,
            reason_code="market_open",
        ).model_copy(update={"session_type": "weekday_morning"})

    app_module = importlib.import_module("trading_api.app")
    monkeypatch.setattr(app_module, "_run_session_preflight", closed_preflight)
    monkeypatch.setattr(
        app_module,
        "_run_dashboard_session_preflight_from_app",
        dashboard_preflight,
    )
    monkeypatch.setenv("TRADING_DATA_ONLY_SHADOW", "true")
    force_exchange_closed(monkeypatch)
    client = make_client(tmp_path)

    status = client.get("/robot/status").json()
    market = client.get("/market/overview").json()
    sber_details = client.get("/market/instruments/MOEX%3ASBER/details").json()
    sber_details = client.get("/market/instruments/MOEX%3ASBER/details").json()
    latest_microstructure = client.get("/market/microstructure/latest").json()
    seed_ts = utc(2026, 6, 12, 7)
    lookback_minutes = (
        int((datetime.now(tz=UTC) - seed_ts).total_seconds() // 60) + 60
    )
    microstructure_summary = client.get(
        "/market/microstructure/summary",
        params={"lookback_minutes": lookback_minutes},
    ).json()
    data_shadow_status = client.get("/runtime/data-shadow/status").json()

    assert status["strategy_state"] == "candidate"
    assert status["session_type"] == "weekday_morning"
    assert status["session_phase"] == "continuous_trading"
    assert status["session_source"] == "fresh_preflight"
    assert status["session_stale"] is True
    assert status["session_stale_reason"] == "runtime_snapshot_mismatch"
    assert status["open_orders_count"] == 1
    assert status["active_positions_count"] == 1
    assert status["balance"]["total_portfolio_value_rub"] == "150000"
    assert status["balance"]["available_cash_rub"] == "120000"
    assert status["balance"]["account_id_masked"] == "acc***t-1"
    assert status["balance"]["balance_degraded"] is False
    assert "account-1" not in str(status["balance"])
    assert market["instruments"][0]["instrument_id"] == "MOEX:SBER"
    assert len(market["instruments"]) == 8
    assert market["instruments"][0]["last_price"] == "100.05000000"
    assert market["instruments"][0]["last_price_source"] == "broker_quote_exchange_closed"
    assert market["instruments"][0]["quote_source"] == "broker_quote_exchange_closed"
    assert market["instruments"][0]["venue_type"] == "broker_otc"
    assert market["instruments"][0]["official_exchange_closed"] is True
    assert market["instruments"][0]["quote_allowed_for_data_collection"] is False
    assert market["instruments"][0]["quote_status"] == "broker_quote"
    assert market["instruments"][0]["is_price_stale"] is True
    assert market["instruments"][0]["mid_price"] == "100.05000000"
    assert Decimal(str(market["instruments"][0]["spread_bps"])).quantize(
        Decimal("0.0001")
    ) == Decimal("9.9950")
    assert "bids" not in market["instruments"][0]["order_book_summary"]
    assert "asks" not in market["instruments"][0]["order_book_summary"]
    assert sber_details["instrument_id"] == "MOEX:SBER"
    assert sber_details["order_book_summary"]["bids"][0]["price"] == "100"
    assert sber_details["order_book_summary"]["asks"][0]["price"] == "100.1"
    assert latest_microstructure[0]["source"] == "data_only_shadow"
    assert latest_microstructure[0]["spread_bps"] == "9.9950"
    assert microstructure_summary["snapshots_count"] == 1
    assert data_shadow_status["real_orders_disabled"] is True
    assert data_shadow_status["collector_state"] in {"stopped", "collecting", "starting"}
    assert data_shadow_status["supervisor_enabled"] is True
    assert data_shadow_status["supervisor_state"] in {
        "stopped",
        "running",
        "watching_stale_stream",
    }
    assert data_shadow_status["stream_restart_count"] == 0
    assert set(data_shadow_status["per_stream_status"]) >= {"order_book", "last_price"}
    assert data_shadow_status["instruments"][:2] == ["MOEX:SBER", "MOEX:GAZP"]
    assert "Strategy trading disabled" in data_shadow_status["warning"]


def test_data_shadow_status_reports_auto_stopped_even_with_recent_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_DATA_ONLY_SHADOW", "true")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'data-shadow-status.db'}")
    Base.metadata.create_all(database.engine)
    now = datetime.now(tz=UTC)
    with database.session_scope() as session:
        command_id = uuid4()
        session.add(
            RobotCommand(
                command_id=command_id,
                command_type="start",
                requested_by="frontend_operator",
                requested_role="operator",
                requested_at=now - timedelta(minutes=10),
                status="applied",
                reason_code="data_only_collection_started",
                accepted_at=now - timedelta(minutes=10),
                applied_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=10),
                payload={
                    "preflight_result": {
                        "market_open": True,
                        "market_closed_expected": False,
                        "session_type": "weekend",
                        "session_phase": "continuous_trading",
                        "reason_code": "market_open",
                        "next_session_at": "2026-06-29T10:00:00+03:00",
                    }
                },
                result_payload={
                    "collector_state": "collecting",
                    "started_at": (now - timedelta(minutes=10)).isoformat(),
                },
            )
        )
        session.add(
            MarketMicrostructureSnapshot(
                **session_context(),
                snapshot_id=uuid4(),
                ts_utc=now - timedelta(seconds=5),
                exchange_ts=now - timedelta(seconds=5),
                received_ts=now - timedelta(seconds=5),
                instrument_id="uid-sber",
                best_bid=Decimal("100"),
                best_ask=Decimal("100.1"),
                mid_price=Decimal("100.05"),
                spread_abs=Decimal("0.1"),
                spread_bps=Decimal("9.995"),
                bid_depth_lots=Decimal("10"),
                ask_depth_lots=Decimal("8"),
                book_imbalance=Decimal("0.1111"),
                market_quality_score=Decimal("0.9"),
                feed_freshness_age_ms=0,
                is_stale=False,
                source="data_only_shadow",
                snapshot_payload={"include_in_calibration": True},
            )
        )
        session.add(
            AuditEvent(
                **session_context(),
                audit_event_id=uuid4(),
                ts_utc=now - timedelta(seconds=3),
                exchange_ts=None,
                received_ts=now - timedelta(seconds=3),
                service="trade-core",
                actor="runtime",
                action="data_only_shadow_collection_auto_stopped",
                entity_type="runtime",
                entity_id=str(command_id),
                severity="warning",
                correlation_id=str(command_id),
                audit_payload={
                    "collector_state": "stopped_session_closed",
                    "reason_code": "data_only_session_window_closed",
                    "stopped_at": (now - timedelta(seconds=3)).isoformat(),
                },
            )
        )

    with database.session_factory() as session:
        status = BffReadService(session).data_shadow_status()

    assert status.collector_state == "stopped_session_closed"
    assert status.stream_alive is False
    assert status.reason_code == "data_only_session_window_closed"
    assert status.supervisor_state == "stopped"
    assert status.stopped_at == now - timedelta(seconds=3)


def test_data_shadow_day_complete_suppresses_stale_next_collection_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_DATA_ONLY_SHADOW", "true")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'day-complete.db'}")
    Base.metadata.create_all(database.engine)
    now = datetime.now(tz=UTC)
    next_session = "2026-07-01T07:00:00+03:00"
    stale_runtime_window = now + timedelta(seconds=30)
    with database.session_scope() as session:
        command_id = uuid4()
        session.add(
            RobotCommand(
                command_id=command_id,
                command_type="start",
                requested_by="frontend_operator",
                requested_role="operator",
                requested_at=now - timedelta(minutes=20),
                status="applied",
                reason_code="data_only_collection_started",
                accepted_at=now - timedelta(minutes=20),
                applied_at=now - timedelta(minutes=20),
                finished_at=now - timedelta(minutes=20),
                payload={
                    "preflight_result": {
                        "market_open": False,
                        "market_closed_expected": True,
                        "session_type": "closed",
                        "session_phase": "closed",
                        "reason_code": "no_trading_window",
                        "next_session_at": next_session,
                    }
                },
                result_payload={
                    "collector_state": "stopped_day_complete",
                    "stopped_at": now.isoformat(),
                },
            )
        )
        session.add(
            AuditEvent(
                **session_context(),
                audit_event_id=uuid4(),
                ts_utc=now,
                exchange_ts=None,
                received_ts=now,
                service="trade-core",
                actor="runtime",
                action="data_only_shadow_collection_day_complete",
                entity_type="runtime",
                entity_id=str(command_id),
                severity="info",
                correlation_id=str(command_id),
                audit_payload={
                    "collector_state": "stopped_day_complete",
                    "day_collection_state": "completed_for_day",
                    "daily_collection_active": False,
                    "current_window_state": "stopped_day_complete",
                    "next_collection_window_at": stale_runtime_window.isoformat(),
                    "completed_for_day_at": now.isoformat(),
                    "last_window_completed_at": now.isoformat(),
                    "reason_code": "data_only_session_window_closed",
                },
            )
        )

    with database.session_factory() as session:
        status = BffReadService(session).data_shadow_status()

    assert status.collector_state == "stopped_day_complete"
    assert status.next_collection_window_at is None
    assert status.next_session_at == datetime.fromisoformat(next_session)


def test_data_shadow_lifecycle_status_distinguishes_paused_collector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_DATA_ONLY_SHADOW", "true")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'paused-data-shadow.db'}")
    Base.metadata.create_all(database.engine)
    now = datetime.now(tz=UTC)
    command_id = uuid4()
    next_window_at = now + timedelta(minutes=5)
    paused_at = now - timedelta(seconds=30)
    with database.session_scope() as session:
        session.add(
            RobotCommand(
                command_id=command_id,
                command_type="start",
                requested_by="frontend_operator",
                requested_role="operator",
                requested_at=now - timedelta(minutes=20),
                status="applied",
                reason_code="data_only_collection_started",
                accepted_at=now - timedelta(minutes=20),
                applied_at=now - timedelta(minutes=20),
                finished_at=now - timedelta(minutes=20),
                payload={
                    "preflight_result": {
                        "market_open": True,
                        "session_type": "weekday_morning",
                        "session_phase": "morning_trading",
                    }
                },
                result_payload={
                    "collector_state": "collecting",
                    "started_at": (now - timedelta(minutes=20)).isoformat(),
                },
            )
        )
        session.add(
            AuditEvent(
                **session_context(),
                audit_event_id=uuid4(),
                ts_utc=paused_at,
                exchange_ts=None,
                received_ts=paused_at,
                service="trade-core",
                actor="runtime",
                action="data_only_shadow_collection_paused_until_next_window",
                entity_type="runtime",
                entity_id=str(command_id),
                severity="info",
                correlation_id=str(command_id),
                audit_payload={
                    "collector_state": "paused_until_next_window",
                    "day_collection_state": "active",
                    "daily_collection_active": True,
                    "current_window_state": "paused_until_next_window",
                    "next_collection_window_at": next_window_at.isoformat(),
                    "remaining_windows_today": 2,
                    "paused_at": paused_at.isoformat(),
                    "last_pause_reason": "data_only_session_window_closed",
                    "reason_code": "data_only_session_window_closed",
                },
            )
        )

    with database.session_factory() as session:
        service = BffReadService(session)
        data_shadow_status = service.data_shadow_status()
        robot_status = service.robot_status(robot_control_state="running")

    assert data_shadow_status.collector_state == "paused_until_next_window"
    assert data_shadow_status.day_collection_state == "active"
    assert data_shadow_status.daily_collection_active is True
    assert data_shadow_status.stream_alive is False
    assert data_shadow_status.supervisor_state == "paused"
    assert data_shadow_status.next_collection_window_at == next_window_at
    assert data_shadow_status.paused_at == paused_at
    assert data_shadow_status.stopped_at is None
    assert "collector_paused_until_next_window" in data_shadow_status.warnings
    assert robot_status.robot_control_state == "running"
    assert robot_status.data_shadow_collector_state == "paused_until_next_window"
    assert robot_status.daily_collection_active is True
    assert robot_status.effective_logging_state == "paused_until_next_window"


def test_portfolio_summary_degrades_when_balance_missing(tmp_path: Path) -> None:
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'empty-api-bff.db'}")
    Base.metadata.create_all(database.engine)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    summary = client.get("/portfolio/summary").json()
    status = client.get("/robot/status").json()

    assert summary["balance"]["balance_degraded"] is True
    assert summary["balance"]["balance_degraded_reason_code"] == "broker_balance_unavailable"
    assert "balance_unavailable" in status["degraded_flags"]


def test_market_overview_uses_latest_candle_when_order_book_missing(tmp_path: Path) -> None:
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'quotes-fallback.db'}")
    Base.metadata.create_all(database.engine)
    now = utc(2026, 6, 19, 18)
    with database.session_scope() as session:
        session.add(
            InstrumentRegistry(
                instrument_id="MOEX:SBER",
                ticker="SBER",
                class_code="TQBR",
                figi="figi-sber",
                instrument_uid="uid-sber",
                name="Sber",
                lot_size=10,
                min_price_increment=Decimal("0.01"),
                currency="RUB",
                is_enabled=True,
                source="test",
                resolution_status="resolved",
                instrument_payload={},
            )
        )
        session.add(
            MarketCandle(
                **session_context(),
                instrument_id="MOEX:SBER",
                timeframe="1m",
                open_ts_utc=now - timedelta(minutes=1),
                close_ts_utc=now,
                exchange_open_ts=now - timedelta(minutes=1),
                exchange_close_ts=now,
                open_price=Decimal("312.10"),
                high_price=Decimal("312.50"),
                low_price=Decimal("312.00"),
                close_price=Decimal("312.42"),
                volume_lots=Decimal("1000"),
                is_closed=True,
                source="historical",
                candle_payload={},
            )
        )
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    market = client.get("/market/overview").json()

    assert market["instruments"][0]["instrument_id"] == "MOEX:SBER"
    assert market["instruments"][0]["last_price"] == "312.42000000"
    assert market["instruments"][0]["last_price_source"] == "latest_market_candle_close"
    assert market["instruments"][0]["quote_status"] in {"live", "stale"}
    assert market["instruments"][0]["is_price_stale"] is True
    assert market["instruments"][0]["display_market_quality_score"] is None
    assert market["instruments"][0]["market_quality_label"] == "no_order_book_samples"


def test_market_overview_resolves_order_book_summary_by_broker_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    force_exchange_open(monkeypatch)
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'quotes-alias.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    now = datetime.now(tz=UTC)
    with database.session_scope() as session:
        summary = session.execute(select(OrderBookSummary).limit(1)).scalars().one()
        summary.instrument_id = "uid-sber"
        summary.ts_utc = now
        summary.exchange_ts = now
        summary.received_ts = now

    with database.session_scope() as session:
        service = BffReadService(session)
        overview = service.market_overview(
            preflight=preflight_response(market_open=True, reason_code="market_open"),
        )

    sber = next(row for row in overview.instruments if row.instrument_id == "MOEX:SBER")
    assert sber.order_book_source == "live_exchange_order_book"
    assert sber.best_bid == Decimal("100.0000")
    assert sber.best_ask == Decimal("100.1000")
    assert sber.mid_price == Decimal("100.0500")
    assert sber.freshness_status == "fresh"


def test_market_overview_keeps_order_book_when_candle_fallback_query_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    force_exchange_open(monkeypatch)
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'quotes-candle-error.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    now = datetime.now(tz=UTC)
    with database.session_scope() as session:
        summary = session.execute(
            select(OrderBookSummary).where(OrderBookSummary.instrument_id == "MOEX:SBER")
        ).scalars().one()
        summary.ts_utc = now
        summary.exchange_ts = now
        summary.received_ts = now

    with database.session_scope() as session:
        original_execute = session.execute

        def flaky_execute(
            statement: Executable,
            params: Any = None,
            **kwargs: Any,
        ) -> Any:
            entities = [
                description.get("entity")
                for description in getattr(statement, "column_descriptions", [])
            ]
            if MarketCandle in entities:
                raise OperationalError(
                    "select market_candle",
                    {},
                    RuntimeError("candle fallback unavailable"),
                )
            return original_execute(statement, params, **kwargs)

        monkeypatch.setattr(session, "execute", flaky_execute)
        service = BffReadService(session)
        overview = service.market_overview(
            preflight=preflight_response(market_open=True, reason_code="market_open"),
        )

    sber = next(row for row in overview.instruments if row.instrument_id == "MOEX:SBER")
    assert sber.last_price_source == "live_exchange_order_book"
    assert sber.order_book_source == "live_exchange_order_book"
    assert sber.best_bid == Decimal("100.0000")
    assert sber.best_ask == Decimal("100.1000")


def test_market_overview_uses_dashboard_order_book_freshness_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    force_exchange_open(monkeypatch)
    monkeypatch.setenv("DASHBOARD_ORDER_BOOK_MAX_EXCHANGE_AGE_SECONDS", "5")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'quotes-stale.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=6)
    with database.session_scope() as session:
        summary = session.execute(select(OrderBookSummary).limit(1)).scalars().one()
        summary.ts_utc = stale_ts
        summary.exchange_ts = stale_ts
        summary.received_ts = stale_ts

    with database.session_scope() as session:
        service = BffReadService(session)
        overview = service.market_overview(
            preflight=preflight_response(market_open=True, reason_code="market_open"),
        )

    sber = next(row for row in overview.instruments if row.instrument_id == "MOEX:SBER")
    assert sber.order_book_stale is True
    assert sber.freshness_status == "stale"
    assert sber.freshness_reason == "received_ts_too_old"
    assert sber.quote_status == "stale"


def test_market_overview_treats_recently_received_live_book_as_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    force_exchange_open(monkeypatch)
    monkeypatch.setenv("DASHBOARD_ORDER_BOOK_MAX_EXCHANGE_AGE_SECONDS", "5")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'quotes-received-fresh.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    now = datetime.now().astimezone().replace(tzinfo=None)
    with database.session_scope() as session:
        summary = session.execute(select(OrderBookSummary).limit(1)).scalars().one()
        summary.ts_utc = now
        summary.exchange_ts = now - timedelta(seconds=30)
        summary.received_ts = now

    with database.session_scope() as session:
        service = BffReadService(session)
        overview = service.market_overview(
            preflight=preflight_response(market_open=True, reason_code="market_open"),
        )

    sber = next(row for row in overview.instruments if row.instrument_id == "MOEX:SBER")
    assert sber.order_book_stale is False
    assert sber.quote_status == "live"
    assert sber.freshness_status == "fresh"
    assert sber.freshness_reason == "fresh"


def test_dashboard_market_feed_merge_does_not_let_stale_cache_override_fresh_base() -> None:
    feed_module = importlib.import_module("trading_api.dashboard_market_feed")
    now = datetime.now(tz=UTC)
    fresh_row = MarketInstrumentOverview(
        instrument_id="MOEX:SBER",
        ticker="SBER",
        official_exchange_open=True,
        quote_source="live_exchange_order_book",
        last_price_source="live_exchange_order_book",
        last_price=Decimal("307.00"),
        last_price_at=now,
        received_ts=now,
        exchange_ts=now,
        stale_by_received_time=False,
        stale_by_exchange_time=False,
        is_price_stale=False,
        freshness_status="fresh",
        quote_status="live",
        order_book_source="live_exchange_order_book",
        order_book_ts=now,
        order_book_stale=False,
    )
    stale_cached_row = fresh_row.model_copy(
        update={
            "last_price": Decimal("306.50"),
            "last_price_at": now - timedelta(seconds=45),
            "received_ts": now - timedelta(seconds=45),
            "exchange_ts": now - timedelta(seconds=45),
            "stale_by_received_time": True,
            "stale_by_exchange_time": True,
            "is_price_stale": True,
            "freshness_status": "stale",
            "freshness_reason": "received_ts_too_old",
            "quote_status": "stale",
            "order_book_ts": now - timedelta(seconds=45),
            "order_book_stale": True,
        }
    )

    merged = feed_module._merge_overviews(
        MarketOverviewResponse(generated_at=now, instruments=[fresh_row]),
        MarketOverviewResponse(
            generated_at=now - timedelta(seconds=45),
            instruments=[stale_cached_row],
        ),
    )

    assert merged.instruments[0].last_price == Decimal("307.00")
    assert merged.instruments[0].quote_status == "live"


def test_dashboard_market_feed_does_not_preserve_live_ladder_after_close() -> None:
    feed_module = importlib.import_module("trading_api.dashboard_market_feed")
    now = datetime.now(tz=UTC)
    closed_row = MarketInstrumentOverview(
        instrument_id="MOEX:SBER",
        ticker="SBER",
        official_exchange_open=False,
        official_exchange_closed=True,
        quote_allowed_for_data_collection=False,
        quote_source="broker_indicative_quote",
        last_price_source="broker_indicative_quote",
        last_price=Decimal("308.00"),
        last_price_at=now,
        received_ts=now,
        exchange_ts=now,
        stale_by_received_time=False,
        stale_by_exchange_time=False,
        is_price_stale=False,
        freshness_status="display_only",
        quote_status="indicative",
        order_book_source=None,
        order_book_ts=None,
        order_book_age_ms=None,
        order_book_stale=True,
        order_book_summary={},
    )
    cached_live_row = closed_row.model_copy(
        update={
            "official_exchange_open": True,
            "official_exchange_closed": False,
            "quote_allowed_for_data_collection": True,
            "quote_source": "live_order_book_mid",
            "last_price_source": "live_order_book_mid",
            "quote_status": "live",
            "order_book_source": "live_order_book_mid",
            "order_book_ts": now,
            "order_book_age_ms": 300,
            "order_book_stale": False,
            "order_book_summary": {
                "include_in_calibration": True,
                "depth_levels": 20,
                "bids": [{"price": "307.99", "quantity_lots": "100"}],
                "asks": [{"price": "308.01", "quantity_lots": "100"}],
            },
        }
    )

    preferred = feed_module._prefer_selected_details(closed_row, cached_live_row)

    assert preferred.official_exchange_open is False
    assert preferred.quote_allowed_for_data_collection is False
    assert preferred.quote_source == "broker_indicative_quote"
    assert preferred.order_book_source is None
    assert preferred.order_book_summary == {}


def test_dashboard_market_feed_timeout_snapshot_is_retry_warning() -> None:
    app_module = importlib.import_module("trading_api.app")
    now = datetime.now(tz=UTC)
    row = MarketInstrumentOverview(
        instrument_id="MOEX:SBER",
        ticker="SBER",
        official_exchange_open=True,
        quote_source="live_exchange_order_book",
        last_price_source="live_exchange_order_book",
        last_price=Decimal("307.00"),
        last_price_at=now,
        received_ts=now,
        exchange_ts=now,
        stale_by_received_time=False,
        stale_by_exchange_time=False,
        is_price_stale=False,
        freshness_status="fresh",
        quote_status="live",
        order_book_source="live_exchange_order_book",
        order_book_ts=now,
        order_book_stale=False,
    )

    class Feed:
        def status(self) -> dict[str, object]:
            return {
                "enabled": True,
                "running": True,
                "market_open": True,
                "session_type": "weekday_evening",
                "session_phase": "continuous_trading",
                "venue_type": "official_exchange",
                "last_refresh_at": now.isoformat(),
                "selected_instrument": "MOEX:SBER",
                "quote_rows_count": 1,
                "order_book_available": True,
                "trade_tape_available": False,
                "errors": [],
                "warnings": [],
            }

    snapshot = app_module._dashboard_market_feed_timeout_snapshot(
        cast(Any, Feed()),
        base_overview=MarketOverviewResponse(generated_at=now, instruments=[row]),
        selected_instrument="MOEX:SBER",
    )

    assert snapshot["errors"] == []
    assert "dashboard_market_feed_timeout" in snapshot["warnings"]
    assert snapshot["status"]["errors"] == []
    assert "dashboard_market_feed_timeout" in snapshot["status"]["warnings"]
    assert snapshot["quote_rows"][0]["quote_status"] == "live"


def test_market_overview_falls_back_when_dashboard_feed_gateway_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import trade_core.infra.tbank as tbank_module

    class FailingGateway:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("readonly gateway unavailable")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setattr(tbank_module, "TBankBrokerGateway", FailingGateway)
    client = make_client(tmp_path)

    market = client.get("/market/overview").json()
    feed_status = client.get(
        "/dashboard/market-feed/status",
        headers={"X-API-Role": "observer"},
    ).json()

    assert len(market["instruments"]) == 8
    assert {row["instrument_id"] for row in market["instruments"]} >= {"MOEX:SBER", "MOEX:GAZP"}
    assert "dashboard_gateway_unavailable" in feed_status["errors"] or feed_status["errors"]


def test_market_overview_filter_and_selected_details_endpoint(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    market = client.get(
        "/market/overview",
        params={"instruments": "SBER,GAZP", "include_details": False},
    ).json()
    details = client.get("/market/instruments/MOEX%3ASBER/details").json()

    assert [row["instrument_id"] for row in market["instruments"]] == ["MOEX:SBER", "MOEX:GAZP"]
    assert "bids" not in market["instruments"][0]["order_book_summary"]
    assert details["instrument_id"] == "MOEX:SBER"
    assert details["order_book_summary"]["bids"][0]["price"] == "100"
    assert details["recent_market_trades"][0]["price"] == "100.04"


def test_dashboard_market_feed_snapshot_does_not_require_collector_or_write_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")
    from sqlalchemy import text

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_closed(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeDashboardFeedGateway())
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    tables = (
        "market_microstructure_snapshot",
        "signal_candidate",
        "order_intent",
        "broker_order",
    )
    with database.session_scope() as session:
        before = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }
    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": True,
            "include_trades": True,
        },
    ).json()
    with database.session_scope() as session:
        after = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }

    assert snapshot["data_only_collection_required"] is False
    assert len(snapshot["quote_rows"]) == 8
    assert snapshot["session"]["market_open"] is False
    assert snapshot["selected_details"]["instrument_id"] == "MOEX:SBER"
    assert snapshot["selected_details"]["order_book_source"] == "broker_quote_exchange_closed"
    assert snapshot["selected_details"]["recent_market_trades"] == []
    assert snapshot["selected_details"]["market_trades_source"] == "tbank_get_last_trades"
    assert snapshot["selected_details"]["trade_tape_status"] == "stale"
    assert snapshot["selected_details"]["trade_tape_reason"] == "trade_exchange_ts_too_old"
    assert before == after


def test_dashboard_quote_board_refreshes_order_books_without_collector_or_db_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")
    from sqlalchemy import text

    gateway = FakeCoreDashboardFeedGateway()
    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_QUOTE_ORDER_BOOK_REFRESH_SECONDS", "0.1")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: gateway)
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-board-feed.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    seed_core_universe_registry(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    tables = (
        "market_microstructure_snapshot",
        "order_book_summary",
        "signal_candidate",
        "order_intent",
        "broker_order",
    )
    with database.session_scope() as session:
        before = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }
    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": False,
            "include_trades": False,
        },
    ).json()
    with database.session_scope() as session:
        after = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }

    assert before == after
    assert snapshot["data_only_collection_required"] is False
    assert len(snapshot["quote_rows"]) == 8
    assert set(gateway.order_book_calls) == {f"MOEX:{ticker}" for ticker in CORE_TEST_TICKERS}
    for row in snapshot["quote_rows"]:
        assert row["quote_status"] == "live"
        assert row["order_book_stale"] is False
        assert row["last_price_source"] == "live_order_book_mid"
        assert row["order_book_source"] == "live_order_book_mid"
        assert row["order_book_summary"]["bids"]
        assert row["order_book_summary"]["asks"]


def test_dashboard_market_feed_sorts_last_trades_newest_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeOldFirstTradesGateway())
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-trades.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": True,
            "include_trades": True,
        },
    ).json()

    selected = snapshot["selected_details"]
    assert selected["recent_market_trades"][0]["price"] == "314.01"
    assert selected["trade_tape_status"] == "live"
    assert selected["trade_tape_reason"] == "fresh"


def test_dashboard_market_feed_returns_short_delayed_trades_as_stale_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeDelayedTradesGateway())
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-delayed.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": True,
            "include_trades": True,
        },
    ).json()

    selected = snapshot["selected_details"]
    assert selected["recent_market_trades"][0]["price"] == "313.30"
    assert selected["market_trades_source"] == "tbank_get_last_trades"
    assert selected["trade_tape_status"] == "stale"
    assert selected["trade_tape_reason"] == "trade_exchange_ts_too_old"


def test_dashboard_market_feed_returns_broker_delayed_trades_up_to_display_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_TRADES_DELAYED_DISPLAY_SECONDS", "300")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "_readonly_tbank_gateway",
        lambda: FakeLongDelayedTradesGateway(),
    )
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-long-delayed.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": True,
            "include_trades": True,
        },
    ).json()

    selected = snapshot["selected_details"]
    assert selected["recent_market_trades"][0]["price"] == "313.25"
    assert selected["market_trades_source"] == "tbank_get_last_trades"
    assert selected["trade_tape_status"] == "stale"
    assert selected["trade_tape_reason"] == "trade_exchange_ts_too_old"
    assert snapshot["status"]["trade_tape_available"] is True


def test_dashboard_market_feed_preserves_trade_tape_over_empty_get_last_trades(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_TRADES_REFRESH_SECONDS", "0")
    force_exchange_open(monkeypatch)
    gateway = FakeIntermittentTradesGateway()
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: gateway)
    database = DatabaseService(
        f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-intermittent-trades.db'}"
    )
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    params = {
        "selected_instrument": "MOEX:SBER",
        "include_order_book": False,
        "include_trades": True,
    }
    first = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params=params,
    ).json()
    second = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params=params,
    ).json()

    assert first["selected_details"]["recent_market_trades"][0]["price"] == "313.40"
    assert second["selected_details"]["recent_market_trades"][0]["price"] == "313.40"
    assert second["status"]["trade_tape_available"] is True
    assert "no_market_trades_samples" not in second["warnings"]


def test_dashboard_market_feed_uses_persisted_trade_tape_when_live_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")
    from sqlalchemy import text

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_TRADES_REFRESH_SECONDS", "0")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "_readonly_tbank_gateway",
        lambda: FakeCoreDashboardFeedGateway(),
    )
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-persisted.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    seed_persisted_trade_sample(
        database,
        exchange_ts=datetime.now(tz=UTC) - timedelta(seconds=2),
        price=Decimal("313.70"),
    )
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    tables = ("market_trade_sample", "signal_candidate", "order_intent", "broker_order")
    with database.session_scope() as session:
        before = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": False,
            "include_trades": True,
        },
    ).json()

    with database.session_scope() as session:
        after = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }

    selected = snapshot["selected_details"]
    assert before == after
    assert selected["recent_market_trades"][0]["price"] == "313.70000000"
    assert selected["market_trades_source"] == "persisted_data_only_trade_tape"
    assert selected["trade_tape_source"] == "persisted_data_only_trade_tape"
    assert selected["trade_tape_status"] == "live"
    assert selected["trade_tape_reason"] == "fresh"
    assert selected["persisted_trade_tape_available"] is True
    assert selected["latest_persisted_trade_ts"] is not None
    assert selected["dashboard_trade_tape_fallback"] == "persisted"
    assert snapshot["status"]["dashboard_trade_tape_fallback"] == "persisted"


def test_dashboard_market_feed_live_trades_take_precedence_over_persisted_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_TRADES_REFRESH_SECONDS", "0")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeOldFirstTradesGateway())
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-live-wins.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    seed_persisted_trade_sample(
        database,
        exchange_ts=datetime.now(tz=UTC) - timedelta(seconds=1),
        price=Decimal("313.70"),
    )
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": False,
            "include_trades": True,
        },
    ).json()

    selected = snapshot["selected_details"]
    assert selected["recent_market_trades"][0]["price"] == "314.01"
    assert selected["market_trades_source"] == "tbank_get_last_trades"
    assert selected["trade_tape_status"] == "live"
    assert selected["dashboard_trade_tape_fallback"] is None


def test_dashboard_market_feed_stale_persisted_trade_tape_does_not_masquerade_as_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_TRADES_REFRESH_SECONDS", "0")
    monkeypatch.setenv("DASHBOARD_TRADES_MAX_EXCHANGE_AGE_SECONDS", "15")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "_readonly_tbank_gateway",
        lambda: FakeCoreDashboardFeedGateway(),
    )
    database = DatabaseService(
        f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-stale-persisted.db'}"
    )
    Base.metadata.create_all(database.engine)
    seed_database(database)
    seed_persisted_trade_sample(
        database,
        exchange_ts=datetime.now(tz=UTC) - timedelta(seconds=45),
        price=Decimal("313.70"),
    )
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": False,
            "include_trades": True,
        },
    ).json()

    selected = snapshot["selected_details"]
    assert selected["recent_market_trades"] == []
    assert selected["market_trades_source"] == "persisted_data_only_trade_tape"
    assert selected["trade_tape_status"] == "stale"
    assert selected["trade_tape_reason"] == "trade_exchange_ts_too_old"
    assert selected["persisted_trade_tape_available"] is True
    assert selected["dashboard_trade_tape_fallback"] is None


def test_dashboard_market_feed_waits_for_selected_refresh_when_cache_incomplete() -> None:
    feed_module = importlib.import_module("trading_api.dashboard_market_feed")
    now = datetime.now(tz=UTC)
    base_row = MarketInstrumentOverview(
        instrument_id="MOEX:SBER",
        ticker="SBER",
        official_exchange_open=True,
        quote_source="live_exchange_order_book",
        last_price_source="live_exchange_order_book",
        last_price=Decimal("100.05"),
        last_price_at=now,
        received_ts=now,
        exchange_ts=now,
        stale_by_received_time=False,
        stale_by_exchange_time=False,
        is_price_stale=False,
        freshness_status="fresh",
        quote_status="live",
        order_book_source="live_exchange_order_book",
        order_book_ts=now,
        order_book_age_ms=500,
        order_book_stale=False,
        order_book_summary={
            "depth_levels": 10,
            "best_bid_qty_lots": "10",
            "best_ask_qty_lots": "10",
        },
    )
    overview = MarketOverviewResponse(generated_at=now, instruments=[base_row])
    ref = SimpleNamespace(
        instrument_id="MOEX:SBER",
        ticker="SBER",
        figi="figi-sber",
        instrument_uid="uid-sber",
    )
    gateway = FakePartialOrderBookGateway()
    feed = feed_module.DashboardMarketFeedService(
        config=feed_module.DashboardMarketFeedConfig(
            quote_refresh_seconds=999,
            selected_book_refresh_seconds=0,
            trades_refresh_seconds=999,
        )
    )

    async def run() -> dict[str, object]:
        await feed._refresh_lock.acquire()
        task = asyncio.create_task(
            feed.snapshot(
                base_overview=overview,
                refs=[ref],
                selected_instrument="MOEX:SBER",
                gateway_factory=lambda: gateway,
                include_order_book=True,
                include_trades=False,
            )
        )
        await asyncio.sleep(0)
        assert not task.done()
        feed._refresh_lock.release()
        return await asyncio.wait_for(task, timeout=2)

    snapshot = asyncio.run(run())
    selected = cast(dict[str, Any], snapshot["selected_details"])
    status = cast(dict[str, Any], snapshot["status"])

    assert gateway.order_book_calls == 1
    assert len(selected["order_book_summary"]["bids"]) == 5
    assert len(selected["order_book_summary"]["asks"]) == 5
    assert status["order_book_available"] is True


def test_dashboard_market_feed_keeps_full_selected_ladder_over_partial_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_SELECTED_BOOK_REFRESH_SECONDS", "0")
    force_exchange_open(monkeypatch)
    gateway = FakePartialOrderBookGateway()
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: gateway)
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-partial-book.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    params = {
        "selected_instrument": "MOEX:SBER",
        "include_order_book": True,
        "include_trades": False,
    }
    first = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params=params,
    ).json()
    second = client.post(
        "/dashboard/market-feed/refresh",
        headers={"X-API-Role": "observer"},
        params=params,
    ).json()

    assert gateway.order_book_calls == 2
    assert len(first["selected_details"]["order_book_summary"]["bids"]) == 5
    assert len(first["selected_details"]["order_book_summary"]["asks"]) == 5
    assert len(second["selected_details"]["order_book_summary"]["bids"]) == 5
    assert len(second["selected_details"]["order_book_summary"]["asks"]) == 5
    assert second["selected_details"]["best_bid"] == first["selected_details"]["best_bid"]


def test_dashboard_market_feed_does_not_mark_one_level_book_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "_readonly_tbank_gateway",
        lambda: FakeOneLevelOrderBookGateway(),
    )
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-one-level.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": True,
            "include_trades": False,
        },
    ).json()

    selected = snapshot["selected_details"]
    assert len(selected["order_book_summary"]["bids"]) == 1
    assert len(selected["order_book_summary"]["asks"]) == 1
    assert snapshot["status"]["order_book_available"] is False
    assert snapshot["selected_details"]["trade_tape_status"] in {
        "stale",
        "no_market_trades_samples",
    }
    assert snapshot["selected_details"]["trade_tape_reason"] is not None

    feed_status = client.get(
        "/dashboard/market-feed/status",
        headers={"X-API-Role": "observer"},
    ).json()
    assert feed_status["order_book_available"] is False
    assert feed_status["trade_tape_status"] in {"stale", "no_market_trades_samples"}
    assert feed_status["trade_tape_reason"] is not None


def test_market_websocket_uses_dashboard_feed_and_preserves_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")
    from sqlalchemy import text

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_closed(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeDashboardFeedGateway())
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-ws.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    app.state.ws_push_interval_seconds = 0.05
    client = TestClient(app)

    tables = (
        "market_microstructure_snapshot",
        "signal_candidate",
        "order_intent",
        "broker_order",
    )
    with database.session_scope() as session:
        before = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }

    with client.websocket_connect(
        "/ws/market-feed?selected_instrument=MOEX:SBER&include_order_book=true&include_trades=true"
    ) as websocket:
        first = websocket.receive_json()
        websocket.send_json({"type": "market.select", "selected_instrument": "MOEX:GAZP"})
        second = websocket.receive_json()

    assert first["type"] == "market.snapshot"
    first_snapshot = first["payload"]["data"]
    second_snapshot = second["payload"]["data"]
    assert first_snapshot["source"] == "dashboard_market_feed"
    assert len(first_snapshot["quote_rows"]) == 8
    assert first_snapshot["selected_details"]["instrument_id"] == "MOEX:SBER"
    assert first_snapshot["selected_details"]["market_trades_source"] is not None
    assert second_snapshot["selected_instrument"] == "MOEX:GAZP"
    assert second_snapshot["selected_details"]["instrument_id"] == "MOEX:GAZP"

    with database.session_scope() as session:
        after = {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in tables
        }
    assert before == after


def test_dashboard_market_feed_normalizes_live_session_label_from_broker_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")
    feed_module = importlib.import_module("trading_api.dashboard_market_feed")
    from sqlalchemy import text

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(
        BffReadService,
        "current_session",
        lambda self, preflight=None: SessionSnapshotResponse(
            calendar_date=date(2026, 6, 28),
            trading_date=date(2026, 6, 28),
            session_type="weekday_evening",
            session_phase="continuous_trading",
            micro_session_id="2026-06-28:weekday_evening:test",
            broker_trading_status="normal_trading",
            observed_at=utc(2026, 6, 28, 16),
            source="fresh_preflight",
        ),
    )
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeDashboardFeedGateway())
    monkeypatch.setattr(
        feed_module,
        "_clock_session_context",
        lambda *, market_open: (
            "weekday_evening",
            "continuous_trading" if market_open else "closed",
        ),
    )
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'dashboard-feed-live.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    with database.session_scope() as session:
        session.execute(
            text(
                "update session_run "
                "set session_type = 'weekend', session_phase = 'continuous_trading'"
            )
        )
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": True,
            "include_trades": False,
        },
    ).json()

    assert snapshot["session"]["market_open"] is True
    assert snapshot["session"]["session_type"] != "weekend"
    assert snapshot["session"]["session_phase"] == "continuous_trading"
    assert snapshot["selected_details"]["broker_trading_status"] == "normal_trading"
    assert snapshot["selected_details"]["order_book_source"] == "live_order_book_mid"


def test_market_overview_uses_dashboard_feed_with_readonly_quote_board_books(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_closed(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeDashboardFeedGateway())
    client = make_client(tmp_path)

    market = client.get("/market/overview").json()
    sber = next(row for row in market["instruments"] if row["instrument_id"] == "MOEX:SBER")

    assert len(market["instruments"]) == 8
    assert sber["last_price"] == "313.19"
    assert sber["last_price_source"] == "broker_quote_exchange_closed"
    assert sber["order_book_summary"]["bids"]
    assert sber["quote_payload"]["dashboard_live_feed"] is True


def test_dashboard_display_endpoints_do_not_run_operator_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    async def fail_operator_preflight(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("operator preflight must not run for dashboard display")

    monkeypatch.setenv("DASHBOARD_MARKET_FEED_ENABLED", "true")
    force_exchange_open(monkeypatch)
    monkeypatch.setattr(app_module, "_run_session_preflight", fail_operator_preflight)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeDashboardFeedGateway())
    client = make_client(tmp_path)

    snapshot = client.get(
        "/dashboard/market-feed/snapshot",
        headers={"X-API-Role": "observer"},
        params={
            "selected_instrument": "MOEX:SBER",
            "include_order_book": True,
            "include_trades": True,
        },
    )
    overview = client.get("/market/overview")
    details = client.get("/market/instruments/MOEX%3ASBER/details")
    refresh = client.post(
        "/market/quotes/refresh?instruments=SBER&details=true",
        headers={"X-API-Role": "observer"},
    )

    assert snapshot.status_code == 200
    assert overview.status_code == 200
    assert details.status_code == 200
    assert refresh.status_code == 200
    assert snapshot.json()["selected_details"]["instrument_id"] == "MOEX:SBER"


def test_market_quotes_refresh_falls_back_when_broker_gateway_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    def unavailable_gateway() -> object:
        raise RuntimeError("readonly gateway unavailable")

    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", unavailable_gateway)
    client = make_client(tmp_path)

    response = client.post(
        "/market/quotes/refresh?instruments=SBER&details=true",
        headers={"X-API-Role": "observer"},
    )

    assert response.status_code == 200
    market = response.json()
    assert [row["instrument_id"] for row in market["instruments"]] == ["MOEX:SBER"]


def test_market_quotes_refresh_marks_successful_order_book_refresh_display_only_when_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    force_exchange_closed(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeQuoteGateway())
    client = make_client(tmp_path)

    response = client.post(
        "/market/quotes/refresh?instruments=SBER&details=true",
        headers={"X-API-Role": "observer"},
    )

    assert response.status_code == 200
    market = response.json()
    sber = next(row for row in market["instruments"] if row["instrument_id"] == "MOEX:SBER")
    assert sber["last_price_source"] == "broker_quote_exchange_closed"
    assert sber["quote_source"] == "broker_quote_exchange_closed"
    assert sber["venue_type"] == "broker_otc"
    assert sber["official_exchange_closed"] is True
    assert sber["quote_allowed_for_data_collection"] is False
    assert sber["quote_status"] == "broker_quote"
    assert sber["is_price_stale"] is False
    assert sber["order_book_stale"] is False
    assert sber["price_staleness_seconds"] == 0
    assert sber["order_book_summary"]["exchange_ts"] == "2026-06-21T10:00:00+00:00"
    assert sber["order_book_summary"]["exchange_age_seconds"] is not None
    assert sber["market_trades_source"] == "tbank_get_last_trades"
    assert sber["recent_market_trades"] == []
    assert sber["trade_tape_status"] == "stale"
    assert sber["trade_tape_reason"] == "trade_exchange_ts_too_old"


def test_order_book_payload_on_official_closed_is_display_only_broker_quote() -> None:
    app_module = importlib.import_module("trading_api.app")

    payload = app_module._order_book_overview_payload(
        {
            "instrument_uid": "uid-sber",
            "exchange_ts": "2026-06-21T10:00:00+00:00",
            "bids": [{"price": "312.99", "quantity_lots": "20"}],
            "asks": [{"price": "313.34", "quantity_lots": "10"}],
        },
        official_exchange_open=False,
        official_exchange_closed=True,
    )

    assert payload is not None
    assert payload["quote_source"] == "broker_quote_exchange_closed"
    assert payload["venue_type"] == "broker_otc"
    assert payload["trading_mode"] == "broker_otc_only"
    assert payload["quote_allowed_for_data_collection"] is False
    assert payload["warning"] == "broker_quote_not_for_calibration"
    assert Decimal(str(payload["spread_abs"])) == Decimal("0.35")
    assert Decimal(str(payload["spread_abs_rub"])) == Decimal("0.35")
    assert Decimal(str(payload["spread_bps"])).quantize(Decimal("0.01")) == Decimal(
        "11.18"
    )
    assert payload["market_quality_label"] == "not_for_calibration"
    assert payload["calibration_market_quality_score"] == Decimal("0")
    assert payload["order_book_summary"]["include_in_calibration"] is False
    assert payload["market_trades_source"] == "no_market_trades_samples"


def test_market_overview_uses_recent_readonly_quote_refresh_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_api.app")

    force_exchange_closed(monkeypatch)
    monkeypatch.setattr(app_module, "_readonly_tbank_gateway", lambda: FakeQuoteGateway())
    client = make_client(tmp_path)

    refreshed = client.post(
        "/market/quotes/refresh?instruments=SBER&details=true",
        headers={"X-API-Role": "observer"},
    )
    overview = client.get("/market/overview")

    assert refreshed.status_code == 200
    assert overview.status_code == 200
    sber = next(
        row for row in overview.json()["instruments"] if row["instrument_id"] == "MOEX:SBER"
    )
    assert sber["last_price_source"] == "broker_quote_exchange_closed"
    assert sber["quote_source"] == "broker_quote_exchange_closed"
    assert sber["quote_status"] == "broker_quote"
    assert sber["quote_allowed_for_data_collection"] is False
    assert sber["is_price_stale"] is False
    assert sber["best_bid"] == "312.98"
    assert sber["best_ask"] == "313.40"
    assert sber["market_trades_source"] == "tbank_get_last_trades"
    assert sber["recent_market_trades"] == []
    assert sber["trade_tape_status"] == "stale"
    assert sber["trade_tape_reason"] == "trade_exchange_ts_too_old"


def test_portfolio_refresh_masks_account_id_and_updates_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import trade_core.infra.tbank as tbank_module

    monkeypatch.setattr(tbank_module, "TBankBrokerGateway", FakeBalanceGateway)
    client = make_client(tmp_path)

    refreshed = client.post(
        "/portfolio/refresh",
        headers={"X-API-Role": "operator"},
        json={},
    ).json()
    summary = client.get("/portfolio/summary").json()

    assert refreshed["balance"]["balance_degraded"] is False
    assert refreshed["balance"]["total_portfolio_value_rub"] == "220000"
    assert refreshed["balance"]["available_cash_rub"] == "125000"
    assert refreshed["balance"]["account_id_masked"] == "acc***456"
    assert "account-123456" not in str(refreshed)
    assert summary["balance"]["account_id_masked"] == "acc***456"


def test_management_auth_and_daily_report_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def open_preflight(*args: object, **kwargs: object) -> SessionPreflightResponse:
        del args, kwargs
        return preflight_response(market_open=True, reason_code="market_open")

    app_module = importlib.import_module("trading_api.app")
    monkeypatch.setattr(app_module, "_run_session_preflight", open_preflight)
    client = make_client(tmp_path)

    assert client.post("/robot/start").status_code == 403
    started = client.post("/robot/start", headers={"X-API-Role": "operator"}).json()
    job = client.post(
        "/reports/daily/run",
        headers={"X-API-Role": "operator"},
        json={"trading_date": "2026-06-12", "strategy_id": "baseline"},
    ).json()

    assert started["status"] == "preflight_pending"
    assert started["queued"] is True
    assert started["reason_code"] == "preflight_pending"
    assert started["command_id"]
    assert started["requested_by"] == "local-dev:operator"
    assert job["status"] == "queued"
    assert job["task_name"] == "report_worker.rebuild_reports_for_date"
    assert job["payload"]["include_counterfactual"] is True
    rebuild = client.post(
        "/reports/rebuild/run",
        headers={"X-API-Role": "operator"},
        json={
            "scope": "hourly",
            "trading_date": "2026-06-12",
            "strategy_id": "baseline",
            "micro_session_id": "2026-06-12:weekday_main:1000",
        },
    ).json()
    job_status = client.get("/reports/jobs/job-1").json()
    assert rebuild["task_name"] == "report_worker.build_hourly_report"
    assert job_status["status"] == "success"
    assert job_status["successful"] is True


def test_robot_start_closed_market_queues_async_preflight_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def closed_preflight(*args: object, **kwargs: object) -> SessionPreflightResponse:
        del args, kwargs
        return preflight_response(market_open=False, reason_code="weekend_session_closed")

    app_module = importlib.import_module("trading_api.app")
    monkeypatch.setattr(app_module, "_run_session_preflight", closed_preflight)
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'closed-start.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    response = client.post("/robot/start", headers={"X-API-Role": "operator"}).json()

    assert response["accepted"] is True
    assert response["queued"] is True
    assert response["status"] == "preflight_pending"
    assert response["reason_code"] == "preflight_pending"
    assert response["effective_logging_state"] == "start_pending"
    with database.session_scope() as session:
        command = session.get(RobotCommand, UUID(response["command_id"]))
        audit = (
            session.query(AuditEvent)
            .filter(AuditEvent.entity_id == response["command_id"])
            .one()
        )
        assert command is not None
        assert command.status == "requested"
        assert command.reason_code == "preflight_pending"
        assert audit.action == "robot_command_start_requested"


def test_session_preflight_endpoint_returns_closed_market(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    use_cache_values: list[object] = []

    async def closed_preflight(*args: object, **kwargs: object) -> SessionPreflightResponse:
        del args
        use_cache_values.append(kwargs.get("use_cache"))
        return preflight_response(market_open=False, reason_code="market_closed_expected")

    app_module = importlib.import_module("trading_api.app")
    monkeypatch.setattr(app_module, "_run_session_preflight", closed_preflight)
    client = make_client(tmp_path)

    response = client.get(
        "/session/preflight",
        headers={"X-API-Role": "observer"},
        params={"instruments": "SBER,GAZP", "cache": "false"},
    ).json()

    assert response["market_open"] is False
    assert response["market_closed_expected"] is True
    assert response["reason_code"] == "market_closed_expected"
    assert use_cache_values == [False]


def test_intraday_and_calibration_observatory_api_roles(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    intraday = client.get(
        "/analytics/intraday",
        params={"trading_date": "2026-06-12", "session_type": "weekday_main"},
    )
    status = client.get("/calibration/observatory/status")
    run_request = {
        "universe": "SBER",
        "lookback_days": 20,
        "windows": "7d,20d",
        "mode": "all",
        "trigger_type": "manual",
        "create_candidate_config": True,
    }
    forbidden_run = client.post("/calibration/observatory/run", json=run_request)
    before_config = client.get(
        "/config/strategy",
        params={"strategy_id": "baseline", "session_template": "weekday_main"},
    ).json()

    run = client.post(
        "/calibration/observatory/run",
        headers={"X-API-Role": "operator"},
        json=run_request,
    )
    run_payload = run.json()
    candidate_id = run_payload["candidate_config_id"]
    operator_approve = client.post(
        f"/calibration/config-candidates/{candidate_id}/approve-for-shadow",
        headers={"X-API-Role": "operator"},
    )
    approved = client.post(
        f"/calibration/config-candidates/{candidate_id}/approve-for-shadow",
        headers={"X-API-Role": "admin"},
    )
    candidates = client.get("/calibration/config-candidates").json()
    after_config = client.get(
        "/config/strategy",
        params={"strategy_id": "baseline", "session_template": "weekday_main"},
    ).json()

    assert intraday.status_code == 200
    assert intraday.json()["market_activity"] in {"low", "normal", "high", "unknown"}
    assert status.status_code == 200
    assert forbidden_run.status_code == 403
    assert run.status_code == 200
    assert run_payload["diagnostic_run_id"]
    assert run_payload["rolling_cube_rows"] >= 0
    assert candidate_id
    assert operator_approve.status_code == 403
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved_for_shadow"
    assert approved.json()["validation_payload"]["runtime_config_changed"] is False
    assert any(row["candidate_config_id"] == candidate_id for row in candidates)
    assert after_config["version"] == before_config["version"] == 1
    assert after_config["config_payload"] == before_config["config_payload"]


def test_robot_control_persists_command_and_audit(tmp_path: Path) -> None:
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'api-control.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    response = client.post(
        "/robot/stop",
        headers={"X-API-Role": "operator", "X-API-Actor": "desk-operator"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "stop_requested"
    with database.session_scope() as session:
        command = session.get(RobotCommand, UUID(payload["command_id"]))
        assert command is not None
        assert command.command_type == "stop"
        assert command.requested_by == "desk-operator"
        audit = session.query(AuditEvent).filter_by(entity_id=payload["command_id"]).one()
        assert audit.action == "robot_command_stop_requested"


def test_production_refuses_dev_auth_without_auth_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("TRADING_API_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("TRADING_API_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("TRADING_API_OBSERVER_TOKEN", raising=False)
    monkeypatch.setenv("TRADING_AUTH_MODE", "dev")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'api-prod.db'}")

    try:
        create_fastapi_app(database=database, runtime_mode=RuntimeMode.PRODUCTION)
    except RuntimeError as exc:
        assert "refuses dev auth" in str(exc)
    else:
        raise AssertionError("production startup accepted dev auth")


def test_reports_config_and_openapi(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/reports/hourly").json()[0]["payload"]["format"] == "hourly_report_v1"
    assert client.get("/reports/daily").json()[0]["market_regime"] == "long_bias"
    counterfactual = client.get("/reports/counterfactual?blocker_code=spread_too_wide").json()
    assert counterfactual[0]["would_profit_5m"] is True
    assert counterfactual[0]["pnl_net"] == "21.000000"
    config = client.get("/config/strategy?strategy_id=baseline&session_template=weekday_main")
    assert config.json()["version"] == 1
    updated = client.put(
        "/config/strategy",
        headers={"X-API-Role": "admin"},
        json={
            "strategy_id": "baseline",
            "session_template": "weekday_main",
            "config_payload": {"enabled": False},
            "risk_limits": {"max_position_lots": 5},
        },
    ).json()
    assert updated["version"] == 2
    paths = client.get("/openapi.json").json()["paths"]
    assert "/robot/status" in paths
    assert "/dashboard/market-feed/status" in paths
    assert "/dashboard/market-feed/snapshot" in paths
    assert "/dashboard/market-feed/refresh" in paths
    assert "/market/instruments/{instrument_id}/details" in paths
    assert "/reports/daily/run" in paths
    assert "/analytics/blockers" in paths


def test_logging_analytics_read_models(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    blockers = client.get(
        "/analytics/blockers?trading_date=2026-06-12&strategy_id=baseline"
    ).json()
    funnel = client.get(
        "/analytics/candidate-funnel?trading_date=2026-06-12&strategy_id=baseline"
    ).json()
    canceled = client.get(
        "/analytics/canceled-orders?trading_date=2026-06-12&strategy_id=baseline"
    ).json()
    market = client.get("/market/overview").json()
    sber_details = client.get("/market/instruments/MOEX%3ASBER/details").json()

    assert blockers["rows"][0]["blocker_code"] == "spread_too_wide"
    assert blockers["rows"][0]["missed_pnl_net"] == "21.000000"
    assert {stage["stage_name"]: stage["count"] for stage in funnel["stages"]}["created"] == 1
    assert {stage["stage_name"]: stage["count"] for stage in funnel["stages"]}["filled"] == 1
    assert canceled["rows"][0]["cancel_reason_code"] == "stale_order"
    assert "recent_market_trades" in market["instruments"][0]
    assert market["instruments"][0]["recent_market_trades"] == []
    assert sber_details["recent_market_trades"][0]["price"] == "100.04"


def test_websocket_channels_send_smoke_snapshots(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    expected_types = {
        "/ws/dashboard": "dashboard.snapshot",
        "/ws/orders": "orders.snapshot",
        "/ws/market": "market.snapshot",
        "/ws/reports": "reports.snapshot",
    }
    for path, expected_type in expected_types.items():
        with client.websocket_connect(path) as websocket:
            message = websocket.receive_json()
            assert message["type"] == expected_type
            assert "message_id" in message
            assert "payload" in message


def test_websocket_dashboard_stays_live(tmp_path: Path) -> None:
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'api-ws.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    app.state.ws_push_interval_seconds = 0.05
    client = TestClient(app)

    with client.websocket_connect("/ws/dashboard") as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "dashboard.snapshot"
    assert second["type"] == "dashboard.snapshot"
    assert second["payload"]["sequence"] == 1


def test_dividend_sync_api_requires_operator_and_returns_status(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    observer_response = client.post(
        "/corporate-actions/dividends/sync",
        headers={"X-API-Role": "observer"},
        json={"dry_run": True},
    )
    operator_response = client.post(
        "/corporate-actions/dividends/sync",
        headers={"X-API-Role": "operator"},
        json={"dry_run": True, "instruments": "SBER"},
    )
    status_response = client.get(
        "/corporate-actions/dividends/sync/status",
        headers={"X-API-Role": "observer"},
    )
    dividends_response = client.get(
        "/corporate-actions/dividends",
        headers={"X-API-Role": "observer"},
    )
    future_response = client.get(
        "/market-special-days/future",
        headers={"X-API-Role": "observer"},
    )

    assert observer_response.status_code == 403
    assert operator_response.status_code == 200
    assert operator_response.json()["source"] == "api_import"
    assert operator_response.json()["real_orders_disabled"] is True
    assert status_response.status_code == 200
    assert "status" in status_response.json()
    assert dividends_response.status_code == 200
    assert future_response.status_code == 200


def test_dividend_sync_status_endpoint_returns_unclean_latest_run(tmp_path: Path) -> None:
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'api-dividend-status.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    now = utc(2026, 6, 18, 12)
    with database.session_scope() as session:
        session.add(
            DividendSyncRun(
                started_at=now - timedelta(seconds=2),
                finished_at=now,
                status="completed_with_errors",
                clean=False,
                from_date=date(2026, 1, 1),
                to_date=date(2027, 1, 1),
                instruments={"values": ["MOEX:SBER", "MOEX:GAZP"]},
                instruments_processed=2,
                successful_instruments=1,
                failed_instruments=1,
                dividends_fetched=1,
                dividends_inserted=1,
                dividends_updated=0,
                existing_unchanged=0,
                special_days_created=1,
                future_risk_windows_created=0,
                error_count=1,
                result_payload={"status": "completed_with_errors", "clean": False},
            )
        )
    app = create_fastapi_app(database=database, report_task_client=FakeReportTaskClient())
    client = TestClient(app)

    response = client.get(
        "/corporate-actions/dividends/sync/status",
        headers={"X-API-Role": "observer"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed_with_errors"
    assert payload["clean"] is False
    assert payload["failed_instruments"] == 1
    assert payload["error_count"] == 1
    assert payload["ready_for_shadow"] is False
    assert payload["ready_for_production"] is False


def test_instrument_registry_api_and_resolve_roles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import trade_core.infra.tbank as tbank_module
    from trade_core.broker_gateway import BrokerUnaryResponse, InstrumentResolveRequest

    class FakeResolveGateway:
        async def resolve_instruments(
            self,
            request: InstrumentResolveRequest,
            metadata: object | None = None,
        ) -> BrokerUnaryResponse:
            del metadata
            return BrokerUnaryResponse(
                method_name="ResolveInstruments",
                data={
                    "instruments": [
                        {
                            "instrument_id": "uid-sber",
                            "instrument_uid": "uid-sber",
                            "figi": "figi-sber",
                            "ticker": "SBER",
                            "class_code": request.class_code,
                            "name": "SBER",
                            "lot_size": 10,
                            "min_price_increment": "0.01",
                            "currency": "RUB",
                            "api_trade_available": True,
                            "short_available": True,
                            "supports_weekend": False,
                        }
                    ]
                },
                headers={},
            )

    monkeypatch.setattr(tbank_module, "TBankBrokerGateway", FakeResolveGateway)
    client = make_client(tmp_path)

    registry = client.get(
        "/instruments/registry",
        headers={"X-API-Role": "observer"},
    )
    forbidden = client.post(
        "/instruments/resolve",
        headers={"X-API-Role": "observer"},
        json={"instruments": "SBER"},
    )
    resolved = client.post(
        "/instruments/resolve",
        headers={"X-API-Role": "operator"},
        json={"instruments": "SBER"},
    )

    assert registry.status_code == 200
    assert registry.json()[0]["resolution_status"] == "resolved"
    assert forbidden.status_code == 403
    assert resolved.status_code == 200
    assert resolved.json()["ready_for_broker_calls"] is True


def test_production_auth_rejects_role_header_without_bearer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRADING_AUTH_MODE", "static_bearer")
    monkeypatch.setenv("TRADING_API_OBSERVER_TOKEN", "observer-token")
    monkeypatch.setenv("TRADING_WS_TICKET_SECRET", "test-ws-secret")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'api-prod-auth.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(
        database=database,
        report_task_client=FakeReportTaskClient(),
        runtime_mode=RuntimeMode.PRODUCTION,
    )
    client = TestClient(app)

    assert client.get("/auth/status").status_code == 401
    role_only = client.get("/auth/status", headers={"X-API-Role": "admin"})
    assert role_only.status_code == 401
    authenticated = client.get(
        "/auth/status",
        headers={"Authorization": "Bearer observer-token"},
    )
    assert authenticated.status_code == 200
    assert authenticated.json()["auth_mode"] == "static_bearer"


def test_websocket_ticket_auth_accepts_valid_and_rejects_bad_ticket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRADING_AUTH_MODE", "static_bearer")
    monkeypatch.setenv("TRADING_API_OBSERVER_TOKEN", "observer-token")
    monkeypatch.setenv("TRADING_WS_TICKET_SECRET", "test-ws-secret")
    database = DatabaseService(f"sqlite+pysqlite:///{tmp_path / 'api-ws-ticket.db'}")
    Base.metadata.create_all(database.engine)
    seed_database(database)
    app = create_fastapi_app(
        database=database,
        report_task_client=FakeReportTaskClient(),
        runtime_mode=RuntimeMode.PRODUCTION,
    )
    app.state.ws_push_interval_seconds = 0.05
    client = TestClient(app)

    ticket_response = client.post(
        "/auth/ws-ticket",
        headers={"Authorization": "Bearer observer-token"},
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]

    with client.websocket_connect(f"/ws/dashboard?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "dashboard.snapshot"

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/ws/dashboard?ticket=bad-ticket") as websocket,
    ):
        websocket.receive_json()
    assert exc_info.value.code == 1008

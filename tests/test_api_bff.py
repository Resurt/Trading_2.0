from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_api import create_fastapi_app
from trading_api.schemas import DailyReportRunRequest, ReportJobResponse
from trading_common.db.base import Base
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CounterfactualResult,
    DailyReport,
    HourlyReport,
    InstrumentRegistry,
    OrderBookSummary,
    OrderIntent,
    PositionSnapshot,
    SessionRun,
    SignalCandidate,
    StrategyConfig,
    StrategyStateEvent,
)
from trading_common.db.service import DatabaseService


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class FakeReportTaskClient:
    def enqueue_daily_report(self, request: DailyReportRunRequest) -> ReportJobResponse:
        return ReportJobResponse(
            job_id="job-1",
            task_name="report_worker.rebuild_reports_for_date",
            status="queued",
            payload={
                "trading_date": request.trading_date.isoformat(),
                "strategy_id": request.strategy_id,
                "include_counterfactual": request.include_counterfactual,
            },
        )


def make_client(tmp_path: Path) -> TestClient:
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


def seed_database(database: DatabaseService) -> None:
    now = utc(2026, 6, 12, 7)
    candidate_id = uuid4()
    request_order_id = uuid4()
    order_intent_id = uuid4()

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
                    snapshot_payload={},
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
                    signal_payload={"reason": "test"},
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
                    passed=False,
                    reason_code="spread_too_wide",
                    reason_payload={},
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
                    summary_payload={},
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
                    report_payload={"format": "hourly_report_v1"},
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
                    report_payload={"format": "daily_report_v1"},
                    generated_at=now,
                ),
                CounterfactualResult(
                    **session_context(),
                    candidate_id=candidate_id,
                    order_intent_id=None,
                    source_event_type="blocked_candidate",
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    blocker_code="spread_too_wide",
                    cancel_reason_code=None,
                    fee_bps_assumed=Decimal("2"),
                    slippage_bps_assumed=Decimal("2"),
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


def test_robot_status_and_market_overview(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    status = client.get("/robot/status").json()
    market = client.get("/market/overview").json()

    assert status["strategy_state"] == "candidate"
    assert status["session_type"] == "weekday_main"
    assert status["open_orders_count"] == 1
    assert status["active_positions_count"] == 1
    assert market["instruments"][0]["instrument_id"] == "MOEX:SBER"
    assert market["instruments"][0]["mid_price"] == "100.05000000"


def test_management_auth_and_daily_report_job(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.post("/robot/start").status_code == 403
    started = client.post("/robot/start", headers={"X-API-Role": "operator"}).json()
    job = client.post(
        "/reports/daily/run",
        headers={"X-API-Role": "operator"},
        json={"trading_date": "2026-06-12", "strategy_id": "baseline"},
    ).json()

    assert started["status"] == "start_requested"
    assert job["status"] == "queued"
    assert job["task_name"] == "report_worker.rebuild_reports_for_date"
    assert job["payload"]["include_counterfactual"] is True


def test_reports_config_and_openapi(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/reports/hourly").json()[0]["payload"]["format"] == "hourly_report_v1"
    assert client.get("/reports/daily").json()[0]["market_regime"] == "long_bias"
    assert client.get("/reports/counterfactual").json()[0]["would_profit_5m"] is True
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
    assert "/reports/daily/run" in paths


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

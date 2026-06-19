from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from trading_api import create_fastapi_app
from trading_api.schemas import (
    DailyReportRunRequest,
    ReportJobResponse,
    ReportJobStatusResponse,
    ReportRebuildRequest,
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
                        "recent_market_trades": [
                            {"side": "buy", "price": "100.04", "qty_lots": 5}
                        ]
                    },
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

    assert started["status"] == "requested"
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
    assert payload["status"] == "requested"
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

    assert blockers["rows"][0]["blocker_code"] == "spread_too_wide"
    assert blockers["rows"][0]["missed_pnl_net"] == "21.000000"
    assert {stage["stage_name"]: stage["count"] for stage in funnel["stages"]}["created"] == 1
    assert {stage["stage_name"]: stage["count"] for stage in funnel["stages"]}["filled"] == 1
    assert canceled["rows"][0]["cancel_reason_code"] == "stale_order"
    assert market["instruments"][0]["recent_market_trades"][0]["price"] == "100.04"


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

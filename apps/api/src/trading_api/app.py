"""FastAPI BFF for live trading, control, and reports."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Annotated, cast
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware

from trading_api.auth import (
    AuthContext,
    auth_context_from_request,
    authenticate_websocket,
    build_auth_provider,
    require_role,
)
from trading_api.read_service import BffReadService
from trading_api.report_tasks import CeleryReportTaskClient, ReportTaskClient
from trading_api.robot_control import RobotControlService
from trading_api.schemas import (
    ApiRole,
    BlockerAnalyticsResponse,
    CanceledOrderDiagnosticsResponse,
    CandidateFunnelResponse,
    CounterfactualResponse,
    DailyReportResponse,
    DailyReportRunRequest,
    HourlyReportResponse,
    MarketOverviewResponse,
    OrderResponse,
    PositionResponse,
    ReportJobResponse,
    ReportJobStatusResponse,
    ReportRebuildRequest,
    ReportScope,
    RobotCommandResponse,
    RobotStatusResponse,
    SessionSnapshotResponse,
    SignalResponse,
    StrategyConfigResponse,
    StrategyConfigUpdateRequest,
    WebSocketEnvelope,
)
from trading_common import AppIdentity, RuntimeMode, ServiceHealth, ServiceName, parse_runtime_mode
from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService
from trading_common.http_health import CONTENT_TYPE_TEXT, render_health, render_metrics
from trading_common.models import HealthStatus
from trading_common.observability import TradingMetrics

AuthDep = Annotated[AuthContext, Depends(auth_context_from_request)]


def runtime_mode_from_env(value: str | None) -> RuntimeMode:
    """Parse runtime mode for local service startup."""

    return parse_runtime_mode(value)


def create_identity(runtime_mode: RuntimeMode = RuntimeMode.HISTORICAL_REPLAY) -> AppIdentity:
    """Return the service identity used by health checks and logs."""

    return AppIdentity(
        service=ServiceName.API,
        version="0.1.0",
        runtime_mode=runtime_mode,
    )


def health() -> ServiceHealth:
    """Return API health."""

    return ServiceHealth(identity=create_identity(), status=HealthStatus.OK)


def create_fastapi_app(
    *,
    database: DatabaseService | None = None,
    report_task_client: ReportTaskClient | None = None,
    runtime_mode: RuntimeMode = RuntimeMode.HISTORICAL_REPLAY,
) -> FastAPI:
    identity = create_identity(runtime_mode)
    app = FastAPI(
        title="Trading 2.0 BFF",
        version=identity.version,
        description=(
            "FastAPI backend-for-frontend for live trading state, management, "
            "reports and WebSocket dashboard channels."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins_from_env(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-API-Actor", "X-API-Role"],
    )
    app.state.identity = identity
    app.state.database = database
    app.state.auth_provider = build_auth_provider(runtime_mode=runtime_mode)
    app.state.report_task_client = report_task_client or CeleryReportTaskClient.from_env(
        database=database
    )
    app.state.robot_control = None
    app.state.metrics = TradingMetrics(identity)
    app.state.ws_push_interval_seconds = _ws_push_interval_from_env()

    @app.get("/health", tags=["health"])
    def get_health() -> Response:
        return Response(
            content=render_health(ServiceHealth(identity=identity, status=HealthStatus.OK)),
            media_type="application/json; charset=utf-8",
        )

    @app.get("/metrics", tags=["health"])
    def get_metrics(request: Request) -> Response:
        metrics = _metrics(request)
        metrics.set_service_health(HealthStatus.OK)
        return Response(
            content=render_metrics(metrics),
            media_type=CONTENT_TYPE_TEXT,
        )

    @app.post("/robot/start", response_model=RobotCommandResponse, tags=["robot"])
    def robot_start(request: Request, auth: AuthDep) -> RobotCommandResponse:
        allowed_auth = require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        control = _robot_control(request)
        return control.start(auth=allowed_auth)

    @app.post("/robot/stop", response_model=RobotCommandResponse, tags=["robot"])
    def robot_stop(request: Request, auth: AuthDep) -> RobotCommandResponse:
        allowed_auth = require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        control = _robot_control(request)
        return control.stop(auth=allowed_auth)

    @app.post("/robot/pause", response_model=RobotCommandResponse, tags=["robot"])
    def robot_pause(request: Request, auth: AuthDep) -> RobotCommandResponse:
        allowed_auth = require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        return _robot_control(request).pause(auth=allowed_auth)

    @app.post("/robot/resume", response_model=RobotCommandResponse, tags=["robot"])
    def robot_resume(request: Request, auth: AuthDep) -> RobotCommandResponse:
        allowed_auth = require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        return _robot_control(request).resume(auth=allowed_auth)

    @app.post("/robot/emergency-stop", response_model=RobotCommandResponse, tags=["robot"])
    def robot_emergency_stop(request: Request, auth: AuthDep) -> RobotCommandResponse:
        allowed_auth = require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        return _robot_control(request).emergency_stop(auth=allowed_auth)

    @app.get("/robot/status", response_model=RobotStatusResponse, tags=["robot"])
    def robot_status(
        request: Request,
        service: ReadServiceDep,
    ) -> RobotStatusResponse:
        return service.robot_status(robot_control_state=_robot_control(request).current_state())

    @app.get("/session/current", response_model=SessionSnapshotResponse, tags=["session"])
    def current_session(service: ReadServiceDep) -> SessionSnapshotResponse:
        return service.current_session()

    @app.get("/positions", response_model=list[PositionResponse], tags=["portfolio"])
    def positions(service: ReadServiceDep) -> list[PositionResponse]:
        return service.positions()

    @app.get("/orders/open", response_model=list[OrderResponse], tags=["orders"])
    def open_orders(service: ReadServiceDep) -> list[OrderResponse]:
        return service.open_orders()

    @app.get("/signals/current", response_model=list[SignalResponse], tags=["signals"])
    def current_signals(service: ReadServiceDep) -> list[SignalResponse]:
        return service.current_signals()

    @app.get("/market/overview", response_model=MarketOverviewResponse, tags=["market"])
    def market_overview(service: ReadServiceDep) -> MarketOverviewResponse:
        return service.market_overview()

    @app.get("/reports/hourly", response_model=list[HourlyReportResponse], tags=["reports"])
    def hourly_reports(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        instrument_id: Annotated[str | None, Query()] = None,
        timeframe: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        blocker_code: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[HourlyReportResponse]:
        return service.hourly_reports(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            blocker_code=blocker_code,
            limit=limit,
        )

    @app.get("/reports/daily", response_model=list[DailyReportResponse], tags=["reports"])
    def daily_reports(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        instrument_id: Annotated[str | None, Query()] = None,
        timeframe: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        blocker_code: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[DailyReportResponse]:
        return service.daily_reports(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            blocker_code=blocker_code,
            limit=limit,
        )

    @app.post("/reports/daily/run", response_model=ReportJobResponse, tags=["reports"])
    def run_daily_report(
        payload: DailyReportRunRequest,
        request: Request,
        auth: AuthDep,
    ) -> ReportJobResponse:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        return _report_task_client(request).enqueue_daily_report(payload)

    @app.post("/reports/rebuild/run", response_model=ReportJobResponse, tags=["reports"])
    def run_report_rebuild(
        payload: ReportRebuildRequest,
        request: Request,
        auth: AuthDep,
    ) -> ReportJobResponse:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        if payload.scope == ReportScope.HOURLY and payload.micro_session_id is None:
            raise HTTPException(
                status_code=400,
                detail="micro_session_id is required for hourly rebuild",
            )
        return _report_task_client(request).enqueue_report_rebuild(payload)

    @app.get("/reports/jobs/{job_id}", response_model=ReportJobStatusResponse, tags=["reports"])
    def report_job_status(job_id: str, request: Request) -> ReportJobStatusResponse:
        return _report_task_client(request).job_status(job_id)

    @app.get(
        "/reports/counterfactual",
        response_model=list[CounterfactualResponse],
        tags=["reports"],
    )
    def counterfactual_reports(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        instrument_id: Annotated[str | None, Query()] = None,
        timeframe: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        blocker_code: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[CounterfactualResponse]:
        return service.counterfactual_reports(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            blocker_code=blocker_code,
            limit=limit,
        )

    @app.get(
        "/analytics/blockers",
        response_model=BlockerAnalyticsResponse,
        tags=["analytics"],
    )
    def blocker_analytics(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        instrument_id: Annotated[str | None, Query()] = None,
        timeframe: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        blocker_code: Annotated[str | None, Query()] = None,
        strategy_version: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> BlockerAnalyticsResponse:
        return service.blocker_analytics(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            blocker_code=blocker_code,
            strategy_version=strategy_version,
            limit=limit,
        )

    @app.get(
        "/analytics/candidate-funnel",
        response_model=CandidateFunnelResponse,
        tags=["analytics"],
    )
    def candidate_funnel(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        instrument_id: Annotated[str | None, Query()] = None,
        timeframe: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        blocker_code: Annotated[str | None, Query()] = None,
        strategy_version: Annotated[int | None, Query()] = None,
    ) -> CandidateFunnelResponse:
        return service.candidate_funnel(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            blocker_code=blocker_code,
            strategy_version=strategy_version,
        )

    @app.get(
        "/analytics/canceled-orders",
        response_model=CanceledOrderDiagnosticsResponse,
        tags=["analytics"],
    )
    def canceled_order_diagnostics(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        instrument_id: Annotated[str | None, Query()] = None,
        timeframe: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        strategy_version: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> CanceledOrderDiagnosticsResponse:
        return service.canceled_order_diagnostics(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            strategy_version=strategy_version,
            limit=limit,
        )

    @app.get("/config/strategy", response_model=StrategyConfigResponse, tags=["config"])
    def get_strategy_config(
        service: ReadServiceDep,
        strategy_id: Annotated[str, Query()] = "baseline",
        session_template: Annotated[str, Query()] = "weekday_main",
    ) -> StrategyConfigResponse:
        return service.get_strategy_config(
            strategy_id=strategy_id,
            session_template=session_template,
        )

    @app.put("/config/strategy", response_model=StrategyConfigResponse, tags=["config"])
    def put_strategy_config(
        payload: StrategyConfigUpdateRequest,
        auth: AuthDep,
        service: ReadServiceDep,
    ) -> StrategyConfigResponse:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.update_strategy_config(payload)

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(websocket: WebSocket) -> None:
        await _stream_ws_snapshots(
            websocket,
            "dashboard.snapshot",
            lambda: _dashboard_payload(cast(FastAPI, websocket.app)),
        )

    @app.websocket("/ws/orders")
    async def ws_orders(websocket: WebSocket) -> None:
        await _stream_ws_snapshots(
            websocket,
            "orders.snapshot",
            lambda: _orders_payload(cast(FastAPI, websocket.app)),
        )

    @app.websocket("/ws/market")
    async def ws_market(websocket: WebSocket) -> None:
        await _stream_ws_snapshots(
            websocket,
            "market.snapshot",
            lambda: _market_payload(cast(FastAPI, websocket.app)),
        )

    @app.websocket("/ws/reports")
    async def ws_reports(websocket: WebSocket) -> None:
        await _stream_ws_snapshots(
            websocket,
            "reports.snapshot",
            lambda: _reports_payload(cast(FastAPI, websocket.app)),
        )

    return app


def _read_service(request: Request) -> Iterator[BffReadService]:
    database = _database_service(request)
    with database.session_scope() as session:
        yield BffReadService(session)


def _read_service_dependency(request: Request) -> Iterator[BffReadService]:
    yield from _read_service(request)


ReadServiceDep = Annotated[BffReadService, Depends(_read_service_dependency)]


def _database_service(request: Request | WebSocket) -> DatabaseService:
    return _database_service_from_app(cast(FastAPI, request.app))


def _database_service_from_app(app: FastAPI) -> DatabaseService:
    database = getattr(app.state, "database", None)
    if database is None:
        database = DatabaseService(build_database_url_from_env())
        app.state.database = database
    return database


def _robot_control(request: Request) -> RobotControlService:
    return _robot_control_from_app(cast(FastAPI, request.app))


def _robot_control_from_app(app: FastAPI) -> RobotControlService:
    control = getattr(app.state, "robot_control", None)
    if control is None:
        control = RobotControlService(_database_service_from_app(app))
        app.state.robot_control = control
    return cast(RobotControlService, control)


def _report_task_client(request: Request) -> ReportTaskClient:
    return cast(ReportTaskClient, request.app.state.report_task_client)


def _metrics(request: Request) -> TradingMetrics:
    return cast(TradingMetrics, request.app.state.metrics)


def _cors_origins_from_env() -> list[str]:
    raw_origins = os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]


def _ws_push_interval_from_env() -> float:
    return max(0.05, float(os.getenv("TRADING_WS_PUSH_INTERVAL_SECONDS", "1.0")))


def _dashboard_payload(app: FastAPI) -> dict[str, object]:
    database = _database_service_from_app(app)
    with database.session_scope() as session:
        service = BffReadService(session)
        status = service.robot_status(
            robot_control_state=_robot_control_from_app(app).current_state()
        )
        return {
            "robot_status": status,
            "market": service.market_overview(),
            "open_orders": service.open_orders(),
            "positions": service.positions(),
            "signals": service.current_signals(),
            "blockers": service.blocker_analytics(limit=5),
            "candidate_funnel": service.candidate_funnel(),
        }


def _orders_payload(app: FastAPI) -> dict[str, object]:
    database = _database_service_from_app(app)
    with database.session_scope() as session:
        return {"orders": BffReadService(session).open_orders()}


def _market_payload(app: FastAPI) -> MarketOverviewResponse:
    database = _database_service_from_app(app)
    with database.session_scope() as session:
        return BffReadService(session).market_overview()


def _reports_payload(app: FastAPI) -> dict[str, object]:
    database = _database_service_from_app(app)
    with database.session_scope() as session:
        service = BffReadService(session)
        return {
            "hourly": service.hourly_reports(limit=5),
            "daily": service.daily_reports(limit=5),
            "blockers": service.blocker_analytics(limit=5),
            "candidate_funnel": service.candidate_funnel(),
            "counterfactual": service.counterfactual_reports(limit=10),
            "canceled_orders": service.canceled_order_diagnostics(limit=5),
        }


async def _stream_ws_snapshots(
    websocket: WebSocket,
    message_type: str,
    payload_factory: Callable[[], object],
) -> None:
    try:
        require_role(
            authenticate_websocket(websocket),
            (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN),
        )
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    interval = float(getattr(websocket.app.state, "ws_push_interval_seconds", 1.0))
    sequence = 0
    try:
        while True:
            payload = payload_factory()
            await _send_ws_envelope(websocket, message_type, payload, sequence=sequence)
            sequence += 1
            if sequence % 10 == 0:
                await _send_ws_envelope(
                    websocket,
                    "heartbeat",
                    {"sequence": sequence},
                    sequence=sequence,
                )
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        return
    except TimeoutError:
        await _close_ws_quietly(websocket, code=1011)


async def _send_ws_envelope(
    websocket: WebSocket,
    message_type: str,
    payload: object,
    *,
    sequence: int,
) -> None:
    micro_session_id = _payload_micro_session_id(payload)
    envelope = WebSocketEnvelope(
        message_id=uuid4(),
        ts_utc=datetime.now(tz=UTC),
        type=message_type,
        micro_session_id=micro_session_id,
        payload={"data": jsonable_encoder(payload), "sequence": sequence},
    )
    await asyncio.wait_for(websocket.send_json(envelope.model_dump(mode="json")), timeout=5.0)


async def _close_ws_quietly(websocket: WebSocket, *, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        return


def _payload_micro_session_id(payload: object) -> str | None:
    if isinstance(payload, dict):
        status = payload.get("robot_status")
        return getattr(status, "micro_session_id", None)
    return getattr(payload, "micro_session_id", None)


app = create_fastapi_app()

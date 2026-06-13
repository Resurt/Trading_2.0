"""FastAPI BFF for live trading, control, and reports."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Annotated, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request, Response, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware

from trading_api.auth import require_role, role_from_header
from trading_api.read_service import BffReadService
from trading_api.report_tasks import CeleryReportTaskClient, ReportTaskClient
from trading_api.robot_control import RobotControlState
from trading_api.schemas import (
    ApiRole,
    CounterfactualResponse,
    DailyReportResponse,
    DailyReportRunRequest,
    HourlyReportResponse,
    MarketOverviewResponse,
    OrderResponse,
    PositionResponse,
    ReportJobResponse,
    RobotCommandResponse,
    RobotStatusResponse,
    SessionSnapshotResponse,
    SignalResponse,
    StrategyConfigResponse,
    StrategyConfigUpdateRequest,
    WebSocketEnvelope,
)
from trading_common import AppIdentity, RuntimeMode, ServiceHealth, ServiceName
from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService
from trading_common.http_health import CONTENT_TYPE_TEXT, render_health, render_metrics
from trading_common.models import HealthStatus

RoleDep = Annotated[ApiRole, Depends(role_from_header)]


def runtime_mode_from_env(value: str | None) -> RuntimeMode:
    """Parse runtime mode for local service startup."""

    if value is None:
        return RuntimeMode.HISTORICAL_REPLAY
    return RuntimeMode(value)


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
        allow_headers=["Accept", "Content-Type", "X-API-Role"],
    )
    app.state.identity = identity
    app.state.database = database
    app.state.report_task_client = report_task_client or CeleryReportTaskClient.from_env()
    app.state.robot_control = RobotControlState()

    @app.get("/health", tags=["health"])
    def get_health() -> Response:
        return Response(
            content=render_health(ServiceHealth(identity=identity, status=HealthStatus.OK)),
            media_type="application/json; charset=utf-8",
        )

    @app.get("/metrics", tags=["health"])
    def get_metrics() -> Response:
        return Response(
            content=render_metrics(ServiceHealth(identity=identity, status=HealthStatus.OK)),
            media_type=CONTENT_TYPE_TEXT,
        )

    @app.post("/robot/start", response_model=RobotCommandResponse, tags=["robot"])
    def robot_start(request: Request, role: RoleDep) -> RobotCommandResponse:
        allowed_role = require_role(role, (ApiRole.OPERATOR, ApiRole.ADMIN))
        control = _robot_control(request)
        return control.start(role=allowed_role)

    @app.post("/robot/stop", response_model=RobotCommandResponse, tags=["robot"])
    def robot_stop(request: Request, role: RoleDep) -> RobotCommandResponse:
        allowed_role = require_role(role, (ApiRole.OPERATOR, ApiRole.ADMIN))
        control = _robot_control(request)
        return control.stop(role=allowed_role)

    @app.get("/robot/status", response_model=RobotStatusResponse, tags=["robot"])
    def robot_status(
        request: Request,
        service: ReadServiceDep,
    ) -> RobotStatusResponse:
        return service.robot_status(robot_control_state=_robot_control(request).state)

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
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[HourlyReportResponse]:
        return service.hourly_reports(
            trading_date=trading_date,
            strategy_id=strategy_id,
            limit=limit,
        )

    @app.get("/reports/daily", response_model=list[DailyReportResponse], tags=["reports"])
    def daily_reports(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[DailyReportResponse]:
        return service.daily_reports(
            trading_date=trading_date,
            strategy_id=strategy_id,
            limit=limit,
        )

    @app.post("/reports/daily/run", response_model=ReportJobResponse, tags=["reports"])
    def run_daily_report(
        payload: DailyReportRunRequest,
        request: Request,
        role: RoleDep,
    ) -> ReportJobResponse:
        require_role(role, (ApiRole.OPERATOR, ApiRole.ADMIN))
        return _report_task_client(request).enqueue_daily_report(payload)

    @app.get(
        "/reports/counterfactual",
        response_model=list[CounterfactualResponse],
        tags=["reports"],
    )
    def counterfactual_reports(
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        strategy_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[CounterfactualResponse]:
        return service.counterfactual_reports(
            trading_date=trading_date,
            strategy_id=strategy_id,
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
        role: RoleDep,
        service: ReadServiceDep,
    ) -> StrategyConfigResponse:
        require_role(role, (ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.update_strategy_config(payload)

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(websocket: WebSocket) -> None:
        await _send_ws_snapshot(websocket, "dashboard.snapshot", _dashboard_payload(websocket))

    @app.websocket("/ws/orders")
    async def ws_orders(websocket: WebSocket) -> None:
        service = _read_service_for_websocket(websocket)
        await _send_ws_snapshot(websocket, "orders.snapshot", {"orders": service.open_orders()})

    @app.websocket("/ws/market")
    async def ws_market(websocket: WebSocket) -> None:
        service = _read_service_for_websocket(websocket)
        await _send_ws_snapshot(websocket, "market.snapshot", service.market_overview())

    @app.websocket("/ws/reports")
    async def ws_reports(websocket: WebSocket) -> None:
        service = _read_service_for_websocket(websocket)
        await _send_ws_snapshot(
            websocket,
            "reports.snapshot",
            {
                "hourly": service.hourly_reports(limit=5),
                "daily": service.daily_reports(limit=5),
            },
        )

    return app


def _read_service(request: Request) -> Iterator[BffReadService]:
    database = _database_service(request)
    with database.session_scope() as session:
        yield BffReadService(session)


def _read_service_dependency(request: Request) -> Iterator[BffReadService]:
    yield from _read_service(request)


ReadServiceDep = Annotated[BffReadService, Depends(_read_service_dependency)]


def _read_service_for_websocket(websocket: WebSocket) -> BffReadService:
    database = _database_service(websocket)
    session = database.session_factory()
    websocket.state.db_session = session
    return BffReadService(session)


def _database_service(request: Request | WebSocket) -> DatabaseService:
    database = getattr(request.app.state, "database", None)
    if database is None:
        database = DatabaseService(build_database_url_from_env())
        request.app.state.database = database
    return database


def _robot_control(request: Request) -> RobotControlState:
    return cast(RobotControlState, request.app.state.robot_control)


def _report_task_client(request: Request) -> ReportTaskClient:
    return cast(ReportTaskClient, request.app.state.report_task_client)


def _cors_origins_from_env() -> list[str]:
    raw_origins = os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]


def _dashboard_payload(websocket: WebSocket) -> dict[str, object]:
    service = _read_service_for_websocket(websocket)
    status = service.robot_status(robot_control_state=websocket.app.state.robot_control.state)
    return {
        "robot_status": status,
        "market": service.market_overview(),
        "open_orders": service.open_orders(),
        "signals": service.current_signals(),
    }


async def _send_ws_snapshot(websocket: WebSocket, message_type: str, payload: object) -> None:
    await websocket.accept()
    try:
        micro_session_id = _payload_micro_session_id(payload)
        envelope = WebSocketEnvelope(
            message_id=uuid4(),
            ts_utc=datetime.now(tz=UTC),
            type=message_type,
            micro_session_id=micro_session_id,
            payload={"data": jsonable_encoder(payload)},
        )
        await websocket.send_json(envelope.model_dump(mode="json"))
    finally:
        session = getattr(websocket.state, "db_session", None)
        if session is not None:
            session.close()
        await websocket.close()


def _payload_micro_session_id(payload: object) -> str | None:
    if isinstance(payload, dict):
        status = payload.get("robot_status")
        return getattr(status, "micro_session_id", None)
    return getattr(payload, "micro_session_id", None)


app = create_fastapi_app()

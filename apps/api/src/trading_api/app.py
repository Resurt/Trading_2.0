"""FastAPI BFF for live trading, control, and reports."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import (
    Body,
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
    build_ws_ticket_manager,
    require_role,
)
from trading_api.dashboard_market_feed import (
    DashboardMarketFeedConfig,
    DashboardMarketFeedService,
)
from trading_api.market_quality import calculate_market_quality, calculate_spread_metrics
from trading_api.read_service import BffReadService
from trading_api.report_tasks import CeleryReportTaskClient, ReportTaskClient
from trading_api.robot_control import RobotControlService
from trading_api.schemas import (
    ApiRole,
    AuthStatusResponse,
    BlockerAnalyticsResponse,
    CalibrationDiagnosticRunResponse,
    CalibrationObservatoryRunRequest,
    CalibrationObservatoryRunResponse,
    CalibrationObservatoryStatusResponse,
    CanceledOrderDiagnosticsResponse,
    CandidateFunnelResponse,
    CounterfactualResponse,
    DailyReportResponse,
    DailyReportRunRequest,
    DataShadowStatusResponse,
    HourlyReportResponse,
    IntradayAnalyticsSnapshotResponse,
    MarketInstrumentOverview,
    MarketMicrostructureSnapshotResponse,
    MarketMicrostructureSummaryResponse,
    MarketOverviewResponse,
    MarketRegimeSnapshotResponse,
    MoneyBalance,
    OrderResponse,
    PortfolioRefreshRequest,
    PortfolioSummaryResponse,
    PositionResponse,
    ReportJobResponse,
    ReportJobStatusResponse,
    ReportRebuildRequest,
    ReportScope,
    RobotCommand,
    RobotCommandResponse,
    RobotStatusResponse,
    RollingPerformanceCubeResponse,
    SessionPreflightResponse,
    SessionSnapshotResponse,
    SignalResponse,
    StrategyConfigCandidateRejectRequest,
    StrategyConfigCandidateResponse,
    StrategyConfigResponse,
    StrategyConfigUpdateRequest,
    WebSocketEnvelope,
    WebSocketTicketResponse,
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
    app.state.ws_ticket_manager = build_ws_ticket_manager(runtime_mode=runtime_mode)
    app.state.report_task_client = report_task_client or CeleryReportTaskClient.from_env(
        database=database
    )
    app.state.robot_control = None
    app.state.metrics = TradingMetrics(identity)
    app.state.ws_push_interval_seconds = _ws_push_interval_from_env()
    app.state.dashboard_market_feed = DashboardMarketFeedService(
        DashboardMarketFeedConfig.from_env()
    )

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

    @app.get("/auth/status", response_model=AuthStatusResponse, tags=["auth"])
    def auth_status(auth: AuthDep) -> AuthStatusResponse:
        return AuthStatusResponse(
            auth_mode=auth.auth_mode,
            role=auth.role,
            subject=auth.subject,
            production_like=runtime_mode is RuntimeMode.PRODUCTION,
        )

    @app.post("/auth/ws-ticket", response_model=WebSocketTicketResponse, tags=["auth"])
    def ws_ticket(request: Request, auth: AuthDep) -> WebSocketTicketResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        manager = getattr(request.app.state, "ws_ticket_manager", None)
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail="WebSocket tickets are not configured in this runtime mode",
            )
        ticket = manager.issue(auth)
        return WebSocketTicketResponse(
            ticket=ticket.ticket,
            expires_at=ticket.expires_at,
            auth_mode=auth.auth_mode,
        )

    @app.post("/robot/start", response_model=RobotCommandResponse, tags=["robot"])
    async def robot_start(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> RobotCommandResponse:
        allowed_auth = require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        control = _robot_control(request)
        requested_instruments = _requested_instruments_csv(
            dict(payload or {}).get("requested_instruments")
        )
        preflight = await _run_session_preflight(request, instruments=requested_instruments)
        preflight_payload = preflight.model_dump(mode="json")
        command_payload: dict[str, object] = {
            **dict(payload or {}),
            "preflight_result": preflight_payload,
            "mode": "data_shadow",
            "trading_disabled": True,
            "data_only_shadow": True,
            "requested_instruments": [
                item.strip()
                for item in requested_instruments.split(",")
                if item.strip()
            ],
        }
        if not preflight.data_only_collection_allowed:
            if preflight.official_exchange_closed:
                message = (
                    "Официальная сессия MOEX закрыта: ДСВД 20-21 июня отменена. "
                    "Брокерские котировки могут отображаться только как "
                    "внебиржевые/индикативные. Data-only сбор для калибровки "
                    "не запущен."
                )
            else:
                message = (
                    "Рынок закрыт. Data-only сбор не запущен. "
                    f"Причина: {preflight.reason_code}."
                )
            return control.reject(
                command=RobotCommand.START,
                auth=allowed_auth,
                reason_code=preflight.reason_code,
                message=message,
                payload=command_payload,
            )
        response = control.request(
            command=RobotCommand.START,
            auth=allowed_auth,
            payload=command_payload,
        )
        response.message = "Data-only collection start requested. Trading disabled."
        return response

    @app.post("/robot/stop", response_model=RobotCommandResponse, tags=["robot"])
    def robot_stop(request: Request, auth: AuthDep) -> RobotCommandResponse:
        allowed_auth = require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        control = _robot_control(request)
        response = control.request(
            command=RobotCommand.STOP,
            auth=allowed_auth,
            payload={
                "mode": "data_shadow",
                "trading_disabled": True,
                "data_only_shadow": True,
                "reason_code": "operator_stop_requested",
            },
        )
        response.message = "Data-only collection stop requested. Trading disabled."
        return response

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
    async def robot_status(
        request: Request,
        service: ReadServiceDep,
    ) -> RobotStatusResponse:
        preflight = await _fresh_operator_preflight_or_none(request)
        return service.robot_status(
            robot_control_state=_robot_control(request).current_state(),
            preflight=preflight,
        )

    @app.get("/dashboard/state", tags=["dashboard"])
    async def dashboard_state(request: Request, auth: AuthDep) -> dict[str, object]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        payload = _dashboard_payload(cast(FastAPI, request.app))
        try:
            preflight = await _run_dashboard_session_preflight(request)
            payload["session_preflight"] = preflight.model_dump(mode="json")
        except Exception as exc:
            payload["session_preflight_error"] = _reason_from_exception(
                exc,
                default="session_preflight_unavailable",
            )
        return {"data": jsonable_encoder(payload), "sequence": 0}

    @app.get("/session/current", response_model=SessionSnapshotResponse, tags=["session"])
    async def current_session(
        request: Request,
        service: ReadServiceDep,
    ) -> SessionSnapshotResponse:
        preflight = await _fresh_operator_preflight_or_none(request)
        return service.current_session(preflight=preflight)

    @app.get("/session/preflight", response_model=SessionPreflightResponse, tags=["session"])
    async def session_preflight(
        request: Request,
        auth: AuthDep,
        instruments: Annotated[str | None, Query()] = None,
        mode: Annotated[str, Query()] = "data_shadow",
        broker_checks: Annotated[bool, Query()] = True,
        cache: Annotated[bool, Query()] = True,
    ) -> SessionPreflightResponse:
        del mode
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        if not broker_checks:
            return await _run_dashboard_session_preflight(request)
        return await _run_session_preflight(request, instruments=instruments, use_cache=cache)

    @app.get("/positions", response_model=list[PositionResponse], tags=["portfolio"])
    def positions(service: ReadServiceDep) -> list[PositionResponse]:
        return service.positions()

    @app.get("/portfolio/summary", response_model=PortfolioSummaryResponse, tags=["portfolio"])
    def portfolio_summary(service: ReadServiceDep) -> PortfolioSummaryResponse:
        return service.portfolio_summary()

    @app.post("/portfolio/refresh", response_model=PortfolioSummaryResponse, tags=["portfolio"])
    async def portfolio_refresh(
        request: Request,
        auth: AuthDep,
        payload: Annotated[PortfolioRefreshRequest | None, Body()] = None,
    ) -> PortfolioSummaryResponse:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        result = await _run_broker_balance_refresh(
            request,
            account_id=payload.account_id if payload else None,
        )
        database = _database_service(request)
        with database.session_scope() as session:
            summary = BffReadService(session).portfolio_summary()
        if result.balance_refreshed or not summary.balance.balance_degraded:
            return summary
        return PortfolioSummaryResponse(
            balance=MoneyBalance(
                account_id_masked=result.account_id_masked,
                balance_degraded=True,
                balance_degraded_reason_code=result.balance_degraded_reason_code,
            ),
            positions_count=0,
            source=result.source,
        )

    @app.get("/orders/open", response_model=list[OrderResponse], tags=["orders"])
    def open_orders(service: ReadServiceDep) -> list[OrderResponse]:
        return service.open_orders()

    @app.get("/signals/current", response_model=list[SignalResponse], tags=["signals"])
    def current_signals(service: ReadServiceDep) -> list[SignalResponse]:
        return service.current_signals()

    @app.get("/dashboard/market-feed/status", tags=["dashboard"])
    def dashboard_market_feed_status(request: Request, auth: AuthDep) -> dict[str, object]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return _dashboard_market_feed(request.app).status()

    @app.get("/dashboard/market-feed/snapshot", tags=["dashboard"])
    async def dashboard_market_feed_snapshot(
        request: Request,
        auth: AuthDep,
        service: ReadServiceDep,
        instruments: Annotated[str | None, Query()] = None,
        selected_instrument: Annotated[str, Query()] = "MOEX:SBER",
        include_order_book: Annotated[bool, Query()] = True,
        include_trades: Annotated[bool, Query()] = True,
    ) -> dict[str, object]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        preflight = await _dashboard_preflight_or_none(request)
        return await _dashboard_market_feed_snapshot(
            request,
            service=service,
            instruments=instruments,
            selected_instrument=selected_instrument,
            include_order_book=include_order_book,
            include_trades=include_trades,
            preflight=preflight,
        )

    @app.post("/dashboard/market-feed/refresh", tags=["dashboard"])
    async def dashboard_market_feed_refresh(
        request: Request,
        auth: AuthDep,
        service: ReadServiceDep,
        instruments: Annotated[str | None, Query()] = None,
        selected_instrument: Annotated[str, Query()] = "MOEX:SBER",
        include_order_book: Annotated[bool, Query()] = True,
        include_trades: Annotated[bool, Query()] = True,
    ) -> dict[str, object]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        preflight = await _dashboard_preflight_or_none(request)
        return await _dashboard_market_feed_snapshot(
            request,
            service=service,
            instruments=instruments,
            selected_instrument=selected_instrument,
            include_order_book=include_order_book,
            include_trades=include_trades,
            force=True,
            preflight=preflight,
        )

    @app.get("/market/overview", response_model=MarketOverviewResponse, tags=["market"])
    async def market_overview(
        request: Request,
        service: ReadServiceDep,
        refresh: Annotated[bool, Query()] = True,
        instruments: Annotated[str | None, Query()] = None,
        include_details: Annotated[bool, Query()] = False,
    ) -> MarketOverviewResponse:
        del refresh
        preflight = await _dashboard_preflight_or_none(request)
        snapshot = await _dashboard_market_feed_snapshot(
            request,
            service=service,
            instruments=instruments,
            selected_instrument="MOEX:SBER",
            include_order_book=include_details,
            include_trades=include_details,
            preflight=preflight,
        )
        overview = MarketOverviewResponse(**cast(dict[str, Any], snapshot["market_overview"]))
        _store_market_quote_cache(request.app, overview)
        return overview

    @app.get(
        "/market/instruments/{instrument_id}/details",
        response_model=MarketInstrumentOverview,
        tags=["market"],
    )
    async def market_instrument_details(
        instrument_id: str,
        request: Request,
        service: ReadServiceDep,
    ) -> MarketInstrumentOverview:
        preflight = await _dashboard_preflight_or_none(request)
        selected_base = service.market_instrument_details(instrument_id, preflight=preflight)
        quote_overview = service.market_overview(
            instruments=None,
            include_details=False,
            preflight=preflight,
        )
        base = quote_overview.model_copy(
            update={
                "generated_at": datetime.now(tz=UTC),
                "instruments": [
                    selected_base
                    if row.instrument_id == selected_base.instrument_id
                    else row
                    for row in quote_overview.instruments
                ],
            }
        )
        snapshot = await _dashboard_market_feed(request.app).snapshot(
            base_overview=base,
            refs=_instrument_refs_for_preflight(
                _database_service(request),
                _default_preflight_instruments(),
            ),
            selected_instrument=instrument_id,
            gateway_factory=_readonly_tbank_gateway,
            include_order_book=True,
            include_trades=True,
        )
        details = snapshot.get("selected_details")
        if not isinstance(details, dict):
            return selected_base
        return MarketInstrumentOverview(**details)

    @app.post("/market/quotes/refresh", response_model=MarketOverviewResponse, tags=["market"])
    async def market_quotes_refresh(
        request: Request,
        auth: AuthDep,
        service: ReadServiceDep,
        instruments: Annotated[str | None, Query()] = None,
        details: Annotated[bool, Query()] = False,
        quotes_only: Annotated[bool, Query()] = True,
        include_order_book: Annotated[bool, Query()] = False,
    ) -> MarketOverviewResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        include_details = details or include_order_book or not quotes_only
        preflight = await _dashboard_preflight_or_none(request)
        base_overview = service.market_overview(
            instruments=instruments,
            include_details=include_details,
            preflight=preflight,
        )
        lock = _market_quote_refresh_lock(cast(FastAPI, request.app))
        if lock.locked():
            return _market_overview_with_cached_quotes(request.app, base_overview)
        async with lock:
            snapshot = await _dashboard_market_feed(request.app).snapshot(
                base_overview=base_overview,
                refs=_instrument_refs_for_preflight(
                    _database_service(request),
                    instruments or _default_preflight_instruments(),
                ),
                selected_instrument="MOEX:SBER",
                gateway_factory=_readonly_tbank_gateway,
                include_order_book=include_details,
                include_trades=include_details,
                force=True,
            )
            refreshed = MarketOverviewResponse(
                **cast(dict[str, Any], snapshot["market_overview"])
            )
            if include_details:
                refreshed = await _market_overview_with_broker_quotes(
                    request,
                    overview=refreshed,
                    instruments=instruments or _default_preflight_instruments(),
                    include_details=include_details,
                )
            else:
                del snapshot
            _store_market_quote_cache(request.app, refreshed)
            return refreshed

    @app.get(
        "/market/microstructure/latest",
        response_model=list[MarketMicrostructureSnapshotResponse],
        tags=["market"],
    )
    def latest_microstructure(
        service: ReadServiceDep,
        instrument_id: str | None = None,
        limit: int = 20,
    ) -> list[MarketMicrostructureSnapshotResponse]:
        return service.latest_microstructure(instrument_id=instrument_id, limit=limit)

    @app.get(
        "/market/microstructure/summary",
        response_model=MarketMicrostructureSummaryResponse,
        tags=["market"],
    )
    def microstructure_summary(
        service: ReadServiceDep,
        lookback_minutes: int = 60,
        instrument_id: str | None = None,
    ) -> MarketMicrostructureSummaryResponse:
        return service.microstructure_summary(
            lookback_minutes=lookback_minutes,
            instrument_id=instrument_id,
        )

    @app.get(
        "/runtime/data-shadow/status",
        response_model=DataShadowStatusResponse,
        tags=["runtime"],
    )
    def data_shadow_status(service: ReadServiceDep) -> DataShadowStatusResponse:
        return service.data_shadow_status()

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

    @app.get(
        "/analytics/intraday/today",
        response_model=IntradayAnalyticsSnapshotResponse,
        tags=["analytics"],
    )
    def intraday_today(auth: AuthDep, service: ReadServiceDep) -> IntradayAnalyticsSnapshotResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.intraday_analytics_snapshot(trading_date=datetime.now(tz=UTC).date())

    @app.get(
        "/analytics/intraday",
        response_model=IntradayAnalyticsSnapshotResponse,
        tags=["analytics"],
    )
    def intraday_analytics(
        auth: AuthDep,
        service: ReadServiceDep,
        trading_date: Annotated[date | None, Query()] = None,
        mode: Annotated[str, Query()] = "all",
    ) -> IntradayAnalyticsSnapshotResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.intraday_analytics_snapshot(trading_date=trading_date, mode=mode)

    @app.get(
        "/analytics/intraday/session",
        response_model=IntradayAnalyticsSnapshotResponse,
        tags=["analytics"],
    )
    def intraday_session(
        auth: AuthDep,
        service: ReadServiceDep,
        trading_date: Annotated[date, Query()],
        session_type: Annotated[str, Query()],
        mode: Annotated[str, Query()] = "all",
    ) -> IntradayAnalyticsSnapshotResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.intraday_analytics_snapshot(
            trading_date=trading_date,
            session_type=session_type,
            mode=mode,
        )

    @app.get(
        "/analytics/intraday/micro-session/{micro_session_id}",
        response_model=IntradayAnalyticsSnapshotResponse,
        tags=["analytics"],
    )
    def intraday_micro_session(
        micro_session_id: str,
        auth: AuthDep,
        service: ReadServiceDep,
    ) -> IntradayAnalyticsSnapshotResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.intraday_analytics_snapshot(micro_session_id=micro_session_id)

    @app.get(
        "/calibration/observatory/status",
        response_model=CalibrationObservatoryStatusResponse,
        tags=["calibration"],
    )
    def calibration_observatory_status(
        auth: AuthDep,
        service: ReadServiceDep,
    ) -> CalibrationObservatoryStatusResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.calibration_observatory_status()

    @app.post(
        "/calibration/observatory/run",
        response_model=CalibrationObservatoryRunResponse,
        tags=["calibration"],
    )
    def calibration_observatory_run(
        payload: CalibrationObservatoryRunRequest,
        request: Request,
        auth: AuthDep,
    ) -> CalibrationObservatoryRunResponse:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        from report_worker.analytics.calibration_observatory import (
            CalibrationDiagnosticService,
            RollingPerformanceCubeService,
            StrategyConfigProposalService,
        )

        universe = _split_csv(payload.universe)
        windows = _split_csv(payload.windows)
        database = _database_service(request)
        try:
            with database.session_scope() as session:
                diagnostic = CalibrationDiagnosticService(session).run_diagnostics(
                    universe,
                    payload.lookback_days,
                    trigger_type=payload.trigger_type,
                    requested_by=payload.requested_by or auth.subject,
                    mode=payload.mode,
                )
                cube_rows = RollingPerformanceCubeService(session).build_rolling_cube(
                    windows,
                    universe=universe,
                    mode=payload.mode,
                )
                candidate_config_id: UUID | None = None
                if payload.create_candidate_config:
                    proposal = StrategyConfigProposalService(
                        session
                    ).create_strategy_config_candidate(
                        base_strategy_id="baseline",
                        proposed_strategy_id="baseline_candidate_draft",
                        source_diagnostic_run_id=UUID(str(diagnostic["diagnostic_run_id"])),
                        proposal_payload={
                            "source": "calibration_observatory",
                            "diagnosis": diagnostic["diagnosis"],
                            "apply_automatically": False,
                        },
                        validation_payload={"rolling_cube_rows": len(cube_rows)},
                        proposed_by="system",
                    )
                    candidate_config_id = UUID(str(proposal["candidate_config_id"]))
                return _observatory_run_response(
                    diagnostic=diagnostic,
                    cube_rows=cube_rows,
                    candidate_config_id=candidate_config_id,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(
        "/calibration/diagnostics",
        response_model=list[CalibrationDiagnosticRunResponse],
        tags=["calibration"],
    )
    def calibration_diagnostics(
        auth: AuthDep,
        service: ReadServiceDep,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[CalibrationDiagnosticRunResponse]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.calibration_diagnostics(limit=limit)

    @app.get(
        "/calibration/diagnostics/{diagnostic_run_id}",
        response_model=CalibrationDiagnosticRunResponse,
        tags=["calibration"],
    )
    def calibration_diagnostic(
        diagnostic_run_id: UUID,
        auth: AuthDep,
        service: ReadServiceDep,
    ) -> CalibrationDiagnosticRunResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        try:
            return service.calibration_diagnostic(diagnostic_run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get(
        "/calibration/rolling-performance",
        response_model=list[RollingPerformanceCubeResponse],
        tags=["calibration"],
    )
    def calibration_rolling_performance(
        auth: AuthDep,
        service: ReadServiceDep,
        window_name: Annotated[str | None, Query()] = None,
        instrument_id: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        timeframe: Annotated[str | None, Query()] = None,
        side: Annotated[str | None, Query()] = None,
        mode: Annotated[str | None, Query()] = None,
        contour_status: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> list[RollingPerformanceCubeResponse]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.rolling_performance(
            window_name=window_name,
            instrument_id=instrument_id,
            session_type=session_type,
            timeframe=timeframe,
            side=side,
            mode=mode,
            contour_status=contour_status,
            limit=limit,
        )

    @app.get(
        "/calibration/regime",
        response_model=list[MarketRegimeSnapshotResponse],
        tags=["calibration"],
    )
    def calibration_regime(
        auth: AuthDep,
        service: ReadServiceDep,
        instrument_id: Annotated[str | None, Query()] = None,
        session_type: Annotated[str | None, Query()] = None,
        market_regime: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[MarketRegimeSnapshotResponse]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.market_regime_snapshots(
            instrument_id=instrument_id,
            session_type=session_type,
            market_regime=market_regime,
            limit=limit,
        )

    @app.get(
        "/calibration/config-candidates",
        response_model=list[StrategyConfigCandidateResponse],
        tags=["calibration"],
    )
    def calibration_config_candidates(
        auth: AuthDep,
        service: ReadServiceDep,
        status: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[StrategyConfigCandidateResponse]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        return service.config_candidates(status=status, limit=limit)

    @app.get(
        "/calibration/config-candidates/{candidate_config_id}",
        response_model=StrategyConfigCandidateResponse,
        tags=["calibration"],
    )
    def calibration_config_candidate(
        candidate_config_id: UUID,
        auth: AuthDep,
        service: ReadServiceDep,
    ) -> StrategyConfigCandidateResponse:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        try:
            return service.config_candidate(candidate_config_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/calibration/config-candidates/{candidate_config_id}/approve-for-shadow",
        response_model=StrategyConfigCandidateResponse,
        tags=["calibration"],
    )
    def calibration_config_candidate_approve_for_shadow(
        candidate_config_id: UUID,
        auth: AuthDep,
        service: ReadServiceDep,
    ) -> StrategyConfigCandidateResponse:
        require_role(auth, (ApiRole.ADMIN,))
        try:
            return service.approve_config_candidate_for_shadow(
                candidate_config_id,
                approved_by=auth.subject,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/calibration/config-candidates/{candidate_config_id}/reject",
        response_model=StrategyConfigCandidateResponse,
        tags=["calibration"],
    )
    def calibration_config_candidate_reject(
        candidate_config_id: UUID,
        payload: StrategyConfigCandidateRejectRequest,
        auth: AuthDep,
        service: ReadServiceDep,
    ) -> StrategyConfigCandidateResponse:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        try:
            return service.reject_config_candidate(
                candidate_config_id,
                rejected_by=auth.subject,
                reason=payload.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/historical/data-quality", tags=["historical"])
    def historical_data_quality(
        request: Request,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        lookback_days: Annotated[int, Query(ge=1, le=3660)] = 90,
        instruments: Annotated[str, Query()] = "SBER,GAZP",
        timeframes: Annotated[str, Query()] = "1m,5m,10m,15m",
        fail_on_missing: Annotated[bool, Query()] = False,
        fail_on_invalid_ohlc: Annotated[bool, Query()] = False,
        max_missing_intervals: Annotated[int | None, Query()] = None,
        require_special_day_classification: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        from trade_core.market_data.events import parse_timeframe
        from trade_core.market_data.quality import (
            HistoricalDataQualityConfig,
            HistoricalDataQualityService,
            default_quality_window,
        )

        window_from, window_to = default_quality_window(
            from_date=from_date,
            to_date=to_date,
            lookback_days=lookback_days,
        )
        config = HistoricalDataQualityConfig(
            from_date=window_from,
            to_date=window_to,
            instruments=_split_csv(instruments),
            timeframes=tuple(parse_timeframe(item) for item in _split_csv(timeframes)),
            fail_on_missing=fail_on_missing,
            fail_on_invalid_ohlc=fail_on_invalid_ohlc,
            max_missing_intervals=max_missing_intervals,
            require_special_day_classification=require_special_day_classification,
        )
        database = _database_service(request)
        with database.session_scope() as session:
            return HistoricalDataQualityService(session).build_report(config).as_payload()

    @app.get("/instruments/registry", tags=["instruments"])
    def instruments_registry(request: Request, auth: AuthDep) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from trade_core.instruments import is_broker_resolved_instrument
        from trading_common.db.models import InstrumentRegistry

        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        database = _database_service(request)
        with database.session_scope() as session:
            rows = session.execute(
                select(InstrumentRegistry).order_by(InstrumentRegistry.ticker)
            ).scalars()
            return [
                _instrument_registry_payload(row, is_broker_resolved_instrument(row))
                for row in rows
            ]

    @app.get("/instruments/unresolved", tags=["instruments"])
    def unresolved_instruments(request: Request, auth: AuthDep) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from trade_core.instruments import is_broker_resolved_instrument
        from trading_common.db.models import InstrumentRegistry

        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        database = _database_service(request)
        with database.session_scope() as session:
            rows = session.execute(
                select(InstrumentRegistry)
                .where(InstrumentRegistry.is_enabled.is_(True))
                .order_by(InstrumentRegistry.ticker)
            ).scalars()
            return [
                _instrument_registry_payload(row, False)
                for row in rows
                if not is_broker_resolved_instrument(row)
            ]

    @app.post("/instruments/resolve", tags=["instruments"])
    def resolve_instruments(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        from trade_core.broker_gateway import InstrumentRef
        from trade_core.infra.tbank import TBankBrokerGateway
        from trade_core.instruments import InstrumentResolverService, is_broker_resolved_instrument
        from trading_common import LaunchModePolicy, RuntimeMode

        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        data = payload or {}
        instruments = _split_csv(str(data.get("instruments", "SBER,GAZP")))
        class_code = str(data.get("class_code", "TQBR"))
        requested = tuple(
            InstrumentRef(
                instrument_id=f"MOEX:{ticker}",
                ticker=ticker,
                class_code=class_code,
            )
            for ticker in instruments
        )
        database = _database_service(request)
        with database.session_scope() as session:
            resolved = asyncio.run(
                InstrumentResolverService(
                    broker_gateway=TBankBrokerGateway(),
                    session=session,
                    launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
                    exchange="MOEX",
                ).resolve_startup_instruments(requested)
            )
            return {
                "source": "tbank_resolved",
                "instruments_requested": len(instruments),
                "instruments_resolved": len(resolved),
                "ready_for_broker_calls": all(
                    is_broker_resolved_instrument(item) for item in resolved
                ),
                "real_orders_disabled": True,
            }

    @app.post("/corporate-actions/import", tags=["corporate-actions"])
    def corporate_actions_import(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        from decimal import Decimal
        from pathlib import Path

        from trade_core.corporate_actions import (
            CorporateActionEvent,
            CorporateActionImportConfig,
            CorporateActionService,
        )

        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        data = payload or {}
        source = str(data.get("source", "manual"))
        confidence = str(data.get("confidence", "manual_unverified"))
        database = _database_service(request)
        with database.session_scope() as session:
            service = CorporateActionService(session)
            file_value = data.get("file")
            if file_value:
                file_path = Path(str(file_value))
                config = CorporateActionImportConfig(source=source, confidence=confidence)
                rows = (
                    service.import_json(file_path, config=config)
                    if file_path.suffix.lower() == ".json"
                    else service.import_csv(file_path, config=config)
                )
            else:
                ticker = str(data.get("ticker", "SBER")).upper()
                row = service.upsert_event(
                    CorporateActionEvent(
                        instrument_id=str(data.get("instrument_id", f"MOEX:{ticker}")),
                        ticker=ticker,
                        action_type=str(data.get("action_type", "dividend")),
                        ex_date=_date_from_payload(data.get("ex_date")),
                        registry_close_date=_date_from_payload(data.get("registry_close_date")),
                        payment_date=_date_from_payload(data.get("payment_date")),
                        amount_per_share=(
                            Decimal(str(data["amount_per_share"]))
                            if data.get("amount_per_share") is not None
                            else None
                        ),
                        currency=cast(str | None, data.get("currency", "RUB")),
                        source=source,
                        confidence=confidence,
                        action_payload={"source": source, "confidence": confidence},
                    )
                )
                rows = (row,)
            return {
                "source": "corporate_actions_import",
                "rows_imported": len(rows),
                "corporate_action_ids": [str(row.corporate_action_id) for row in rows],
            }

    @app.get("/corporate-actions", tags=["corporate-actions"])
    def corporate_actions(
        request: Request,
        auth: AuthDep,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        instruments: Annotated[str, Query()] = "",
        source: Annotated[str | None, Query()] = None,
        action_type: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        from trade_core.corporate_actions import CorporateActionService

        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        database = _database_service(request)
        with database.session_scope() as session:
            return [
                {
                    "corporate_action_id": str(row.corporate_action_id),
                    "instrument_id": row.instrument_id,
                    "ticker": row.ticker,
                    "action_type": row.action_type,
                    "ex_date": row.ex_date.isoformat() if row.ex_date else None,
                    "amount_per_share": str(row.amount_per_share)
                    if row.amount_per_share is not None
                    else None,
                    "currency": row.currency,
                    "source": row.source,
                    "confidence": row.confidence,
                    "payload": row.action_payload,
                }
                for row in CorporateActionService(session).list_events(
                    from_date=from_date,
                    to_date=to_date,
                    instruments=_split_csv(instruments),
                    source=source,
                    action_type=action_type,
                )
            ]

    @app.post("/corporate-actions/dividends/sync", tags=["corporate-actions"])
    def tbank_dividend_sync(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        from decimal import Decimal

        from trade_core.corporate_actions import DividendSyncConfig, DividendSyncService
        from trade_core.infra.tbank import TBankBrokerGateway
        from trade_core.runtime import SafeNoopBrokerGateway

        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        _ensure_historical_api_mode(runtime_mode)
        data = payload or {}
        dry_run = bool(data.get("dry_run", False))
        config = DividendSyncConfig(
            instruments=_split_csv(str(data.get("instruments", "SBER,GAZP"))),
            from_date=_date_from_payload(data.get("from_date")),
            to_date=_date_from_payload(data.get("to_date")),
            lookback_days=int(data.get("lookback_days", 730)),
            lookahead_days=int(data.get("lookahead_days", 365)),
            dry_run=dry_run,
            force_rebuild=bool(data.get("force_rebuild", False)),
            classify_special_days=bool(data.get("classify_special_days", True)),
            gap_threshold_bps=Decimal(str(data.get("gap_threshold_bps", "150"))),
            dividend_gap_threshold_bps=Decimal(
                str(data.get("dividend_gap_threshold_bps", "50"))
            ),
            runtime_mode=runtime_mode.value,
        )
        gateway = (
            SafeNoopBrokerGateway()
            if dry_run
            else TBankBrokerGateway()
        )
        database = _database_service(request)
        with database.session_scope() as session:
            return asyncio.run(
                DividendSyncService(session=session, broker_gateway=gateway).run(config)
            ).as_payload()

    @app.get("/corporate-actions/dividends/sync/status", tags=["corporate-actions"])
    def tbank_dividend_sync_status(
        request: Request,
        auth: AuthDep,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        lookback_days: Annotated[int, Query(ge=1, le=3660)] = 730,
        instruments: Annotated[str, Query()] = "SBER,GAZP",
        max_age_hours: Annotated[int, Query(ge=1, le=720)] = 24,
    ) -> dict[str, Any]:
        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        window_from, window_to = _historical_window(
            from_date=from_date,
            to_date=to_date,
            lookback_days=lookback_days,
        )
        database = _database_service(request)
        with database.session_scope() as session:
            return _dividend_sync_status_payload(
                session=session,
                from_date=window_from,
                to_date=window_to,
                instruments=_split_csv(instruments),
                max_age_hours=max_age_hours,
            )

    @app.get("/corporate-actions/dividends", tags=["corporate-actions"])
    def tbank_dividends(
        request: Request,
        auth: AuthDep,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        instruments: Annotated[str, Query()] = "",
        source: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        from trade_core.corporate_actions import CorporateActionService

        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        database = _database_service(request)
        with database.session_scope() as session:
            return [
                {
                    "corporate_action_id": str(row.corporate_action_id),
                    "instrument_id": row.instrument_id,
                    "ticker": row.ticker,
                    "action_type": row.action_type,
                    "ex_date": row.ex_date.isoformat() if row.ex_date else None,
                    "amount_per_share": str(row.amount_per_share)
                    if row.amount_per_share is not None
                    else None,
                    "currency": row.currency,
                    "source": row.source,
                    "confidence": row.confidence,
                    "payload": row.action_payload,
                }
                for row in CorporateActionService(session).list_events(
                    from_date=from_date,
                    to_date=to_date,
                    instruments=_split_csv(instruments),
                    source=source,
                    action_type="dividend",
                )
            ]

    @app.post("/market-special-days/classify", tags=["corporate-actions"])
    def market_special_days_classify(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        from decimal import Decimal

        from trade_core.corporate_actions import MarketSpecialDayClassifier

        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        _ensure_historical_api_mode(runtime_mode)
        data = payload or {}
        window_from, window_to = _historical_window(
            from_date=_date_from_payload(data.get("from_date")),
            to_date=_date_from_payload(data.get("to_date")),
            lookback_days=int(data.get("lookback_days", 90)),
        )
        database = _database_service(request)
        with database.session_scope() as session:
            if bool(data.get("require_dividend_sync", False)):
                from trade_core.corporate_actions import CorporateActionService

                effective_to = window_to
                if bool(data.get("include_future", False)):
                    from datetime import timedelta

                    effective_to = effective_to + timedelta(
                        days=int(data.get("lookahead_days", 365))
                    )
                if not CorporateActionService(session).api_imported_dividend_events_exist(
                    from_date=window_from,
                    to_date=effective_to,
                    instruments=_split_csv(str(data.get("instruments", "SBER,GAZP"))),
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error_code": "dividend_sync_missing",
                            "message": "Run T-Bank dividend sync before classification",
                        },
                    )
            return MarketSpecialDayClassifier(session).classify(
                from_date=window_from,
                to_date=window_to,
                instruments=_split_csv(str(data.get("instruments", "SBER,GAZP"))),
                gap_threshold_bps=Decimal(str(data.get("gap_threshold_bps", "150"))),
                dividend_gap_threshold_bps=Decimal(
                    str(data.get("dividend_gap_threshold_bps", "50"))
                ),
                force_rebuild=bool(data.get("force_rebuild", False)),
                include_future=bool(data.get("include_future", False)),
                lookahead_days=int(data.get("lookahead_days", 365)),
            ).as_payload()

    @app.get("/market-special-days", tags=["corporate-actions"])
    def market_special_days(
        request: Request,
        auth: AuthDep,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        instruments: Annotated[str, Query()] = "",
    ) -> list[dict[str, Any]]:
        from trade_core.corporate_actions import CorporateActionService

        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        database = _database_service(request)
        with database.session_scope() as session:
            return [
                {
                    "special_day_id": str(row.special_day_id),
                    "trading_date": row.trading_date.isoformat(),
                    "instrument_id": row.instrument_id,
                    "ticker": row.ticker,
                    "special_day_type": row.special_day_type,
                    "reason_code": row.reason_code,
                    "source": row.source,
                    "open_gap_bps": str(row.open_gap_bps)
                    if row.open_gap_bps is not None
                    else None,
                    "severity": row.severity,
                    "exclude_from_primary_calibration": (
                        row.exclude_from_primary_calibration
                    ),
                    "trade_policy": row.trade_policy,
                    "payload": row.special_day_payload,
                }
                for row in CorporateActionService(session).list_special_days(
                    from_date=from_date,
                    to_date=to_date,
                    instruments=_split_csv(instruments),
                )
            ]

    @app.get("/market-special-days/future", tags=["corporate-actions"])
    def market_special_days_future(
        request: Request,
        auth: AuthDep,
        instruments: Annotated[str, Query()] = "",
        lookahead_days: Annotated[int, Query(ge=1, le=3660)] = 365,
    ) -> list[dict[str, Any]]:
        from datetime import timedelta

        from trade_core.corporate_actions import CorporateActionService

        require_role(auth, (ApiRole.OBSERVER, ApiRole.OPERATOR, ApiRole.ADMIN))
        today = datetime.now(tz=UTC).date()
        database = _database_service(request)
        with database.session_scope() as session:
            return [
                {
                    "special_day_id": str(row.special_day_id),
                    "trading_date": row.trading_date.isoformat(),
                    "instrument_id": row.instrument_id,
                    "ticker": row.ticker,
                    "special_day_type": row.special_day_type,
                    "reason_code": row.reason_code,
                    "source": row.source,
                    "open_gap_bps": str(row.open_gap_bps)
                    if row.open_gap_bps is not None
                    else None,
                    "severity": row.severity,
                    "exclude_from_primary_calibration": (
                        row.exclude_from_primary_calibration
                    ),
                    "trade_policy": row.trade_policy,
                    "payload": row.special_day_payload,
                }
                for row in CorporateActionService(session).list_special_days(
                    from_date=today,
                    to_date=today + timedelta(days=lookahead_days),
                    instruments=_split_csv(instruments),
                )
                if row.special_day_type == "future_dividend_risk_window"
            ]

    @app.post("/historical/replay/run", tags=["historical"])
    def historical_replay_run(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        _ensure_historical_api_mode(runtime_mode)
        from trade_core.market_data.events import parse_timeframe
        from trade_core.replay import (
            HistoricalDbReplayConfig,
            HistoricalDbReplayService,
            default_replay_window,
        )

        data = payload or {}
        window_from, window_to = default_replay_window(
            from_date=_date_from_payload(data.get("from_date")),
            to_date=_date_from_payload(data.get("to_date")),
            lookback_days=int(data.get("lookback_days", 90)),
        )
        config = HistoricalDbReplayConfig(
            from_date=window_from,
            to_date=window_to,
            instruments=_split_csv(str(data.get("instruments", "SBER,GAZP"))),
            timeframes=tuple(
                parse_timeframe(item)
                for item in _split_csv(str(data.get("timeframes", "5m,10m,15m")))
            ),
            strategy_id=str(data.get("strategy_id", "baseline")),
            strategy_version=str(data.get("strategy_version", "latest")),
            dry_run=bool(data.get("dry_run", False)),
            reset_derived_events=bool(data.get("reset_derived_events", False)),
            include_special_days=bool(data.get("include_special_days", False)),
            exclude_dividend_gap_days=bool(data.get("exclude_dividend_gap_days", True)),
            exclude_corporate_action_days=bool(
                data.get("exclude_corporate_action_days", True)
            ),
            exclude_abnormal_gap_days=bool(data.get("exclude_abnormal_gap_days", False)),
            special_day_policy=str(data.get("special_day_policy", "exclude")),
            require_special_day_classification=bool(
                data.get("require_special_day_classification", False)
            ),
            allow_default_strategy_config=bool(
                data.get("allow_default_strategy_config", False)
            ),
            session_template=str(data.get("session_template", "weekday_main")),
            config_version=str(data.get("config_version", "latest")),
        )
        database = _database_service(request)
        with database.session_scope() as session:
            return asyncio.run(HistoricalDbReplayService(session).run(config)).as_payload()

    @app.post("/historical/counterfactual/run", tags=["historical"])
    def historical_counterfactual_run(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        _ensure_historical_api_mode(runtime_mode)
        from report_worker.analytics.historical_counterfactual import (
            HistoricalCounterfactualConfig,
            HistoricalCounterfactualService,
            default_counterfactual_window,
        )

        data = payload or {}
        window_from, window_to = default_counterfactual_window(
            from_date=_date_from_payload(data.get("from_date")),
            to_date=_date_from_payload(data.get("to_date")),
            lookback_days=int(data.get("lookback_days", 90)),
        )
        config = HistoricalCounterfactualConfig(
            from_date=window_from,
            to_date=window_to,
            strategy_id=str(data.get("strategy_id", "baseline")),
            instruments=_split_csv(str(data.get("instruments", "SBER,GAZP"))),
            timeframes=_split_csv(str(data.get("timeframes", "5m,10m,15m"))),
            dry_run=bool(data.get("dry_run", False)),
            force_rebuild=bool(data.get("force_rebuild", False)),
        )
        database = _database_service(request)
        with database.session_scope() as session:
            return HistoricalCounterfactualService(session).rebuild(config).as_payload()

    @app.post("/historical/reports/rebuild", tags=["historical"])
    def historical_reports_rebuild(
        request: Request,
        auth: AuthDep,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        require_role(auth, (ApiRole.OPERATOR, ApiRole.ADMIN))
        _ensure_historical_api_mode(runtime_mode)
        from report_worker.analytics.historical_reports import (
            HistoricalReportRebuildConfig,
            HistoricalReportRebuildService,
            default_report_window,
        )

        data = payload or {}
        window_from, window_to = default_report_window(
            from_date=_date_from_payload(data.get("from_date")),
            to_date=_date_from_payload(data.get("to_date")),
            lookback_days=int(data.get("lookback_days", 90)),
        )
        config = HistoricalReportRebuildConfig(
            from_date=window_from,
            to_date=window_to,
            strategy_id=str(data.get("strategy_id", "baseline")),
            instrument=cast(str | None, data.get("instrument")),
            timeframe=cast(str | None, data.get("timeframe")),
            session_type=cast(str | None, data.get("session_type")),
            include_counterfactual=bool(data.get("include_counterfactual", False)),
            force_rebuild=bool(data.get("force_rebuild", True)),
            dry_run=bool(data.get("dry_run", False)),
        )
        database = _database_service(request)
        with database.session_scope() as session:
            return HistoricalReportRebuildService(session).rebuild(config).as_payload()

    @app.get("/analytics/calibration", tags=["analytics"])
    def analytics_calibration(
        request: Request,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        lookback_days: Annotated[int, Query(ge=1, le=3660)] = 90,
        strategy_id: Annotated[str, Query()] = "baseline",
        instruments: Annotated[str, Query()] = "SBER,GAZP",
        timeframes: Annotated[str, Query()] = "5m,10m,15m",
        group_by: Annotated[str, Query()] = "session_type,instrument_id,timeframe,blocker_code",
        calibration_scope: Annotated[str, Query()] = "primary_normal_days",
        include_dividend_gap_days: Annotated[bool, Query()] = False,
        include_corporate_action_days: Annotated[bool, Query()] = False,
        include_abnormal_gap_days: Annotated[bool, Query()] = False,
        require_special_day_classification: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        from report_worker.analytics.calibration import (
            CalibrationReportConfig,
            CalibrationReportService,
            default_calibration_window,
        )

        window_from, window_to = default_calibration_window(
            from_date=from_date,
            to_date=to_date,
            lookback_days=lookback_days,
        )
        config = CalibrationReportConfig(
            from_date=window_from,
            to_date=window_to,
            strategy_id=strategy_id,
            instruments=_split_csv(instruments),
            timeframes=_split_csv(timeframes),
            group_by=_split_csv(group_by),
            calibration_scope=calibration_scope,
            include_dividend_gap_days=include_dividend_gap_days,
            include_corporate_action_days=include_corporate_action_days,
            include_abnormal_gap_days=include_abnormal_gap_days,
            require_special_day_classification=require_special_day_classification,
        )
        database = _database_service(request)
        with database.session_scope() as session:
            return CalibrationReportService(session).build(config).as_payload()

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


async def _run_session_preflight(
    request: Request,
    *,
    instruments: str | None,
    use_cache: bool = True,
) -> SessionPreflightResponse:
    from trade_core.broker_gateway import BrokerGateway
    from trade_core.session import (
        TradingSessionPreflightConfig,
        TradingSessionPreflightService,
    )

    gateway = cast(BrokerGateway, _preflight_broker_gateway())
    refs = _instrument_refs_for_preflight(_database_service(request), instruments)
    config = TradingSessionPreflightConfig(instruments=tuple(refs))
    cache_key = _session_preflight_cache_key(refs)
    if use_cache:
        cached = _cached_session_preflight(cast(FastAPI, request.app), cache_key)
        if cached is not None:
            return cached.model_copy(
                update={
                    "cache_hit": True,
                    "cache_key": cache_key,
                }
            )
    timeout_seconds = float(os.environ.get("TRADING_SESSION_PREFLIGHT_TIMEOUT_SECONDS", "30"))
    try:
        result = await asyncio.wait_for(
            TradingSessionPreflightService(gateway).run(config),
            timeout=timeout_seconds,
        )
        payload = result.as_payload()
        payload["cache_hit"] = False
        payload["cache_key"] = cache_key
        response = SessionPreflightResponse(**payload)
        if use_cache:
            _store_session_preflight(cast(FastAPI, request.app), cache_key, response)
        return response
    except TimeoutError:
        fallback_result = await TradingSessionPreflightService(
            cast(BrokerGateway, _UnavailableReadonlyBrokerGateway())
        ).run(config)
    payload = fallback_result.as_payload()
    warnings = payload.get("warnings")
    payload["warnings"] = [
        *(warnings if isinstance(warnings, list) else []),
        "broker_preflight_timeout",
    ]
    payload["source"] = "fallback_preflight_timeout"
    payload["cache_hit"] = False
    payload["cache_key"] = cache_key
    response = SessionPreflightResponse(**payload)
    if use_cache:
        _store_session_preflight(cast(FastAPI, request.app), cache_key, response)
    return response


async def _fresh_operator_preflight_or_none(
    request: Request,
    *,
    instruments: str | None = None,
) -> SessionPreflightResponse | None:
    try:
        return await _run_session_preflight(
            request,
            instruments=instruments or _default_preflight_instruments(),
            use_cache=False,
        )
    except Exception:
        return None


async def _dashboard_preflight_or_none(request: Request) -> SessionPreflightResponse | None:
    timeout_seconds = float(os.environ.get("DASHBOARD_SESSION_PREFLIGHT_TIMEOUT_SECONDS", "1.5"))
    try:
        return await asyncio.wait_for(
            _run_dashboard_session_preflight(request),
            timeout=timeout_seconds,
        )
    except Exception:
        return None


async def _run_dashboard_session_preflight(request: Request) -> SessionPreflightResponse:
    from trade_core.broker_gateway import BrokerGateway
    from trade_core.session.preflight import (
        TradingSessionPreflightConfig,
        TradingSessionPreflightService,
    )

    app = cast(FastAPI, request.app)
    refs = _instrument_refs_for_preflight(_database_service(request), None)
    cached = _cached_session_preflight(
        app,
        _session_preflight_cache_key(refs),
    )
    if cached is not None:
        return cached
    result = await TradingSessionPreflightService(
        cast(BrokerGateway, _UnavailableReadonlyBrokerGateway())
    ).run(TradingSessionPreflightConfig(instruments=()))
    payload = result.as_payload()
    payload["source"] = "dashboard_fallback_calendar"
    warnings = payload.get("warnings")
    payload["warnings"] = [
        *(warnings if isinstance(warnings, list) else []),
        "dashboard_calendar_only_no_broker_calls",
    ]
    return SessionPreflightResponse(**payload)


def _session_preflight_cache_key(refs: list[Any]) -> str:
    values = [
        str(getattr(ref, "instrument_id", ref)).strip()
        for ref in refs
        if str(getattr(ref, "instrument_id", ref)).strip()
    ]
    return ",".join(sorted(values)) or "dashboard"


def _session_preflight_cache_ttl_seconds() -> int:
    raw = os.getenv("TRADING_SESSION_PREFLIGHT_CACHE_TTL_SECONDS", "30")
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def _session_preflight_cache(
    app: FastAPI,
) -> dict[str, tuple[SessionPreflightResponse, datetime]]:
    cache = getattr(app.state, "session_preflight_cache", None)
    if cache is None:
        cache = {}
        app.state.session_preflight_cache = cache
    return cast(dict[str, tuple[SessionPreflightResponse, datetime]], cache)


def _cached_session_preflight(app: FastAPI, key: str) -> SessionPreflightResponse | None:
    cache = _session_preflight_cache(app)
    item = cache.get(key)
    if item is None:
        return None
    response, cached_at = item
    if datetime.now(tz=UTC) - cached_at > timedelta(seconds=_session_preflight_cache_ttl_seconds()):
        cache.pop(key, None)
        return None
    return response


def _store_session_preflight(
    app: FastAPI,
    key: str,
    response: SessionPreflightResponse,
) -> None:
    _session_preflight_cache(app)[key] = (response, datetime.now(tz=UTC))


async def _run_broker_balance_refresh(
    request: Request,
    *,
    account_id: str | None,
) -> Any:
    from trade_core.portfolio import BrokerBalanceRefreshResult, BrokerBalanceRefreshService

    try:
        gateway = _readonly_tbank_gateway()
    except Exception as exc:
        return BrokerBalanceRefreshResult(
            balance_refreshed=False,
            account_id_masked=_mask_account_id(account_id),
            total_portfolio_value_rub=None,
            available_cash_rub=None,
            blocked_cash_rub=None,
            expected_yield_rub=None,
            free_collateral_rub=None,
            last_balance_refresh_at=None,
            balance_degraded=True,
            balance_degraded_reason_code=_reason_from_exception(
                exc,
                default="broker_gateway_unavailable",
            ),
        )
    database = _database_service(request)
    with database.session_scope() as session:
        service = BrokerBalanceRefreshService(broker_gateway=gateway, session=session)
        timeout_seconds = float(os.environ.get("BROKER_BALANCE_REFRESH_TIMEOUT_SECONDS", "15"))
        try:
            return await asyncio.wait_for(
                service.refresh(account_id=account_id),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return BrokerBalanceRefreshResult(
                balance_refreshed=False,
                account_id_masked=_mask_account_id(account_id),
                total_portfolio_value_rub=None,
                available_cash_rub=None,
                blocked_cash_rub=None,
                expected_yield_rub=None,
                free_collateral_rub=None,
                last_balance_refresh_at=None,
                balance_degraded=True,
                balance_degraded_reason_code="broker_balance_timeout",
            )


async def _market_overview_with_broker_quotes(
    request: Request,
    *,
    overview: MarketOverviewResponse,
    instruments: str,
    include_details: bool = False,
) -> MarketOverviewResponse:
    from trade_core.broker_gateway import LastPricesRequest

    refs = [
        ref
        for ref in _instrument_refs_for_preflight(_database_service(request), instruments)
        if getattr(ref, "instrument_uid", None) or getattr(ref, "figi", None)
    ]
    if not refs:
        return overview
    try:
        gateway = _readonly_tbank_gateway()
    except Exception:
        return overview
    try:
        timeout_seconds = float(os.environ.get("MARKET_LAST_PRICES_REFRESH_TIMEOUT_SECONDS", "3"))
        last_prices_response = await asyncio.wait_for(
            gateway.get_last_prices(LastPricesRequest(instruments=tuple(refs))),
            timeout=timeout_seconds,
        )
    except Exception:
        last_prices_response = None

    ref_by_broker_id: dict[str, str] = {}
    for ref in refs:
        instrument_id = str(getattr(ref, "instrument_id", "") or "")
        for key in (getattr(ref, "instrument_uid", None), getattr(ref, "figi", None)):
            if key:
                ref_by_broker_id[str(key)] = instrument_id

    prices_by_instrument: dict[str, dict[str, object]] = {}
    if last_prices_response is not None:
        raw_prices = last_prices_response.data.get("prices")
        if isinstance(raw_prices, list):
            for item in raw_prices:
                if not isinstance(item, dict):
                    continue
                broker_key = str(
                    item.get("instrument_uid")
                    or item.get("figi")
                    or item.get("instrument_id")
                    or ""
                )
                target_instrument_id = ref_by_broker_id.get(broker_key)
                if not target_instrument_id:
                    continue
                prices_by_instrument[target_instrument_id] = item

    context_by_instrument = {
        instrument.instrument_id: {
            "official_exchange_open": instrument.official_exchange_open,
            "official_exchange_closed": instrument.official_exchange_closed,
            "venue_type": instrument.venue_type,
        }
        for instrument in overview.instruments
    }
    recent_trades_by_instrument: dict[str, list[dict[str, object]]] = {}
    order_books_by_instrument: dict[str, dict[str, object]] = {}
    if include_details:
        detailed_refs = refs[: int(os.environ.get("MARKET_DETAILED_REFRESH_MAX_INSTRUMENTS", "1"))]
        recent_trades_by_instrument = await _broker_market_trades_by_instrument(
            gateway=gateway,
            refs=detailed_refs,
            context_by_instrument=context_by_instrument,
        )
        order_books_by_instrument = await _broker_order_books_by_instrument(
            gateway=gateway,
            refs=detailed_refs,
            context_by_instrument=context_by_instrument,
            recent_trades_by_instrument=recent_trades_by_instrument,
        )

    refreshed = []
    for instrument in overview.instruments:
        order_book_payload = order_books_by_instrument.get(instrument.instrument_id)
        if order_book_payload is not None:
            refreshed.append(
                instrument.model_copy(
                    update={
                        **order_book_payload,
                    }
                )
            )
            continue
        price_payload = prices_by_instrument.get(instrument.instrument_id)
        recent_trades = cast(
            list[dict[str, object]],
            order_books_by_instrument.get(instrument.instrument_id, {}).get(
                "recent_market_trades",
                [],
            ),
        )
        if price_payload is None:
            if recent_trades:
                refreshed.append(
                    instrument.model_copy(
                        update={
                            "recent_market_trades": recent_trades,
                            "market_trades_source": "tbank_get_last_trades",
                            "market_trades_age_ms": _market_trades_age_ms(recent_trades),
                        }
                    )
                )
            else:
                refreshed.append(instrument)
            continue
        price = _decimal_or_none(price_payload.get("price"))
        if price is None:
            refreshed.append(instrument)
            continue
        exchange_ts = _datetime_or_none(price_payload.get("exchange_ts"))
        official_exchange_closed = instrument.official_exchange_closed
        official_exchange_open = instrument.official_exchange_open
        last_price_source = (
            "live_exchange_last_price"
            if official_exchange_open
            else "broker_quote_exchange_closed"
            if official_exchange_closed
            else "broker_indicative_quote"
        )
        venue_type = (
            "official_exchange"
            if official_exchange_open
            else "broker_otc"
            if official_exchange_closed
            else "broker_indicative"
        )
        refreshed.append(
            instrument.model_copy(
                update={
                    "last_price": price,
                    "last_price_at": exchange_ts or datetime.now(tz=UTC),
                    "last_price_ts": exchange_ts or datetime.now(tz=UTC),
                    "last_price_source": last_price_source,
                    "quote_source": last_price_source,
                    "venue_type": venue_type,
                    "quote_allowed_for_data_collection": official_exchange_open,
                    "quote_allowed_for_display": True,
                    "is_price_stale": False,
                    "price_staleness_seconds": 0,
                    "quote_status": "live" if official_exchange_open else "broker_quote",
                    "warning": (
                        "broker_quote_not_for_calibration"
                        if official_exchange_closed
                        else None
                    ),
                    "recent_market_trades": recent_trades,
                    "market_trades_source": (
                        "tbank_get_last_trades"
                        if recent_trades
                        else "no_market_trades_samples"
                    ),
                    "market_trades_age_ms": _market_trades_age_ms(recent_trades),
                    "quote_payload": {
                        **instrument.quote_payload,
                        "source": last_price_source,
                        "quote_source": last_price_source,
                        "venue_type": venue_type,
                        "official_exchange_open": official_exchange_open,
                        "official_exchange_closed": official_exchange_closed,
                        "quote_allowed_for_data_collection": official_exchange_open,
                        "include_in_calibration": official_exchange_open,
                    },
                }
            )
        )
    return MarketOverviewResponse(generated_at=datetime.now(tz=UTC), instruments=refreshed)


async def _broker_order_books_by_instrument(
    *,
    gateway: Any,
    refs: list[Any],
    context_by_instrument: dict[str, dict[str, object]],
    recent_trades_by_instrument: dict[str, list[dict[str, object]]],
) -> dict[str, dict[str, object]]:
    from trade_core.broker_gateway import OrderBookRequest

    timeout_seconds = float(os.environ.get("MARKET_ORDER_BOOK_REFRESH_TIMEOUT_SECONDS", "1.0"))
    concurrency = max(1, int(os.environ.get("MARKET_ORDER_BOOK_REFRESH_CONCURRENCY", "4")))
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(ref: Any) -> tuple[str, dict[str, object] | None]:
        instrument_id = str(getattr(ref, "instrument_id", "") or "")
        try:
            async with semaphore:
                response = await asyncio.wait_for(
                    gateway.get_order_book(OrderBookRequest(instrument=ref, depth=10)),
                    timeout=timeout_seconds,
                )
        except Exception:
            return instrument_id, None
        return instrument_id, _order_book_overview_payload(
            response.data,
            official_exchange_open=bool(
                context_by_instrument.get(instrument_id, {}).get("official_exchange_open")
            ),
            official_exchange_closed=bool(
                context_by_instrument.get(instrument_id, {}).get("official_exchange_closed")
            ),
            recent_market_trades=recent_trades_by_instrument.get(instrument_id, []),
        )

    results = await asyncio.gather(*(fetch(ref) for ref in refs))
    return {
        instrument_id: payload
        for instrument_id, payload in results
        if instrument_id and payload is not None
    }


async def _broker_market_trades_by_instrument(
    *,
    gateway: Any,
    refs: list[Any],
    context_by_instrument: dict[str, dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    from trade_core.broker_gateway import LastTradesRequest

    timeout_seconds = float(os.environ.get("MARKET_TRADES_REFRESH_TIMEOUT_SECONDS", "1.0"))
    lookback_minutes = int(os.environ.get("MARKET_TRADES_REFRESH_LOOKBACK_MINUTES", "30"))
    limit = int(os.environ.get("MARKET_TRADES_REFRESH_LIMIT", "12"))
    concurrency = max(1, int(os.environ.get("MARKET_TRADES_REFRESH_CONCURRENCY", "4")))
    semaphore = asyncio.Semaphore(concurrency)
    to_ts = datetime.now(tz=UTC)
    from_ts = to_ts - timedelta(minutes=max(1, lookback_minutes))

    async def fetch(ref: Any) -> tuple[str, list[dict[str, object]]]:
        instrument_id = str(getattr(ref, "instrument_id", "") or "")
        context = context_by_instrument.get(instrument_id, {})
        official_exchange_open = bool(context.get("official_exchange_open"))
        official_exchange_closed = bool(context.get("official_exchange_closed"))
        trade_source = "exchange" if official_exchange_open else "all"
        venue_type = "official_exchange" if official_exchange_open else "broker_otc"
        source = (
            "tbank_get_last_trades_exchange"
            if official_exchange_open
            else "tbank_get_last_trades_broker"
        )
        include_in_calibration = official_exchange_open and not official_exchange_closed
        try:
            async with semaphore:
                response = await asyncio.wait_for(
                    gateway.get_last_trades(
                        LastTradesRequest(
                            instrument=ref,
                            from_=from_ts,
                            to=to_ts,
                            trade_source=trade_source,
                        )
                    ),
                    timeout=timeout_seconds,
                )
        except Exception:
            return instrument_id, []
        trades = response.data.get("trades")
        if not isinstance(trades, list):
            return instrument_id, []
        normalized: list[dict[str, object]] = []
        for item in trades[:limit]:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    **item,
                    "instrument_id": instrument_id,
                    "source": source,
                    "venue_type": venue_type,
                    "quote_allowed_for_data_collection": include_in_calibration,
                    "include_in_calibration": include_in_calibration,
                }
            )
        return instrument_id, normalized

    results = await asyncio.gather(*(fetch(ref) for ref in refs))
    return {instrument_id: trades for instrument_id, trades in results if instrument_id}


def _order_book_overview_payload(
    payload: dict[str, object],
    *,
    official_exchange_open: bool = False,
    official_exchange_closed: bool = False,
    recent_market_trades: list[dict[str, object]] | None = None,
) -> dict[str, object] | None:
    bids = _price_levels(payload.get("bids"), reverse=True)
    asks = _price_levels(payload.get("asks"), reverse=False)
    if not bids or not asks:
        return None
    now = datetime.now(tz=UTC)
    exchange_ts = _datetime_or_none(payload.get("exchange_ts"))
    received_ts = now
    best_bid_price, best_bid_qty = bids[0]
    best_ask_price, best_ask_qty = asks[0]
    bid_depth = sum((qty for _, qty in bids[:5]), Decimal("0"))
    ask_depth = sum((qty for _, qty in asks[:5]), Decimal("0"))
    spread_metrics = calculate_spread_metrics(best_bid_price, best_ask_price)
    mid_price = spread_metrics.mid_price
    spread_abs = spread_metrics.spread_abs
    spread_bps = spread_metrics.spread_bps
    if mid_price is None:
        return None
    depth_total = bid_depth + ask_depth
    imbalance = None if depth_total == 0 else (bid_depth - ask_depth) / depth_total
    age_seconds = 0.0
    exchange_age_seconds = (
        max(0.0, (now - exchange_ts.astimezone(UTC)).total_seconds())
        if exchange_ts is not None
        else None
    )
    venue_type = (
        "official_exchange"
        if official_exchange_open
        else "broker_otc"
        if official_exchange_closed
        else "broker_indicative"
    )
    quote_source = (
        "live_exchange_order_book"
        if official_exchange_open
        else "broker_quote_exchange_closed"
        if official_exchange_closed
        else "broker_indicative_quote"
    )
    recent_market_trades = recent_market_trades or []
    quality_components = calculate_market_quality(
        spread_bps=spread_bps,
        bid_depth_lots=bid_depth,
        ask_depth_lots=ask_depth,
        best_bid_qty_lots=best_bid_qty,
        best_ask_qty_lots=best_ask_qty,
        book_imbalance=imbalance,
        order_book_age_ms=int(age_seconds * 1000),
        order_book_stale=False,
        venue_type=venue_type,
        official_exchange_open=official_exchange_open,
        trades_count=len(recent_market_trades),
    )
    return {
        "venue_type": venue_type,
        "trading_mode": (
            "standard_exchange"
            if official_exchange_open
            else "broker_otc_only"
            if official_exchange_closed
            else "indicative_only"
        ),
        "quote_source": quote_source,
        "quote_allowed_for_data_collection": official_exchange_open,
        "quote_allowed_for_display": True,
        "last_price": mid_price,
        "last_price_at": received_ts,
        "last_price_ts": received_ts,
        "last_price_source": quote_source,
        "is_price_stale": False,
        "price_staleness_seconds": int(age_seconds),
        "quote_status": "live" if official_exchange_open else "broker_quote",
        "spread": spread_abs,
        "spread_abs": spread_abs,
        "spread_bps": spread_bps,
        "spread_abs_rub": spread_abs,
        "spread_units_validated": True,
        "mid_price": mid_price,
        "market_quality": quality_components.get("display_market_quality_score"),
        "market_quality_score": quality_components.get("display_market_quality_score"),
        "display_market_quality_score": quality_components.get(
            "display_market_quality_score"
        ),
        "calibration_market_quality_score": quality_components.get(
            "calibration_market_quality_score"
        ),
        "market_quality_label": quality_components.get(
            "market_quality_label", "unknown"
        ),
        "market_quality_components": quality_components,
        "best_bid": best_bid_price,
        "best_ask": best_ask_price,
        "bid_depth_lots": bid_depth,
        "ask_depth_lots": ask_depth,
        "book_imbalance": imbalance,
        "order_book_source": quote_source,
        "order_book_ts": received_ts,
        "order_book_age_ms": int(age_seconds * 1000),
        "order_book_stale": False,
        "recent_market_trades": recent_market_trades,
        "market_trades_source": (
            "tbank_get_last_trades"
            if recent_market_trades
            else "no_market_trades_samples"
        ),
        "market_trades_age_ms": _market_trades_age_ms(recent_market_trades),
        "reason_code": (
            "moex_dsvd_cancelled_platform_update"
            if official_exchange_closed
            else None
        ),
        "warning": (
            "broker_quote_not_for_calibration" if official_exchange_closed else None
        ),
        "order_book_summary": {
            "source": quote_source,
            "venue_type": venue_type,
            "quote_allowed_for_data_collection": official_exchange_open,
            "include_in_calibration": official_exchange_open,
            "depth_levels": len(bids) + len(asks),
            "bids": [
                {"price": str(price), "quantity_lots": str(quantity)}
                for price, quantity in bids[:20]
            ],
            "asks": [
                {"price": str(price), "quantity_lots": str(quantity)}
                for price, quantity in asks[:20]
            ],
            "best_bid_qty_lots": str(best_bid_qty),
            "best_ask_qty_lots": str(best_ask_qty),
            "bid_depth_lots": str(bid_depth),
            "ask_depth_lots": str(ask_depth),
            "book_imbalance": str(imbalance) if imbalance is not None else None,
            "spread_abs_rub": str(spread_abs) if spread_abs is not None else None,
            "spread_bps": str(spread_bps) if spread_bps is not None else None,
            "market_quality_components": quality_components,
            "exchange_ts": exchange_ts.isoformat() if exchange_ts is not None else None,
            "received_ts": received_ts.isoformat(),
            "age_seconds": round(age_seconds, 3),
            "exchange_age_seconds": round(exchange_age_seconds, 3)
            if exchange_age_seconds is not None
            else None,
            "is_stale": False,
        },
        "quote_payload": {
            "source": quote_source,
            "quote_source": quote_source,
            "venue_type": venue_type,
            "official_exchange_open": official_exchange_open,
            "official_exchange_closed": official_exchange_closed,
            "quote_allowed_for_data_collection": official_exchange_open,
            "quote_allowed_for_display": True,
            "include_in_calibration": official_exchange_open,
            "price_staleness_seconds": int(age_seconds),
            "order_book_stale": False,
            "market_quality_components": quality_components,
            "exchange_ts": exchange_ts.isoformat() if exchange_ts is not None else None,
            "exchange_age_seconds": round(exchange_age_seconds, 3)
            if exchange_age_seconds is not None
            else None,
        },
    }


def _price_levels(value: object, *, reverse: bool) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(value, list):
        return []
    levels: list[tuple[Decimal, Decimal]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        price = _decimal_or_none(item.get("price"))
        qty = _decimal_or_none(item.get("quantity_lots") or item.get("quantity"))
        if price is None or qty is None:
            continue
        levels.append((price, qty))
    return sorted(levels, key=lambda item: item[0], reverse=reverse)


def _book_quality_score(
    *,
    spread_bps: Decimal | None,
    imbalance: Decimal | None,
    age_seconds: float,
) -> Decimal | None:
    if spread_bps is None:
        return None
    spread_penalty = min(spread_bps / Decimal("100"), Decimal("0.70"))
    imbalance_penalty = (
        min(abs(imbalance) * Decimal("0.20"), Decimal("0.20"))
        if imbalance is not None
        else Decimal("0")
    )
    freshness_penalty = Decimal("0.25") if age_seconds > 30 else Decimal("0")
    score = Decimal("1") - spread_penalty - imbalance_penalty - freshness_penalty
    return max(Decimal("0"), min(Decimal("1"), score)).quantize(Decimal("0.0001"))


def _readonly_tbank_gateway() -> Any:
    from trade_core.infra.tbank import TBankBrokerConfig, TBankBrokerGateway, TBankEnvironment

    raw_environment = os.environ.get(
        "TBANK_READONLY_ENVIRONMENT",
        os.environ.get("TBANK_ENVIRONMENT", TBankEnvironment.LIVE.value),
    )
    config = TBankBrokerConfig.from_env().with_environment(TBankEnvironment(raw_environment))
    try:
        return TBankBrokerGateway(config=config)
    except TypeError:
        return TBankBrokerGateway()


def _preflight_broker_gateway() -> Any:
    try:
        return _readonly_tbank_gateway()
    except Exception:
        return _UnavailableReadonlyBrokerGateway()


class _UnavailableReadonlyBrokerGateway:
    async def trading_schedules(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("broker_schedule_unavailable")

    async def get_trading_status(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("broker_status_unavailable")


def _instrument_refs_for_preflight(
    database: DatabaseService,
    instruments: str | None,
) -> list[Any]:
    from sqlalchemy import select

    from trade_core.broker_gateway import InstrumentRef
    from trading_common.db.models import InstrumentRegistry

    raw_values = [
        item.strip().upper()
        for item in (instruments or "").split(",")
        if item.strip()
    ]
    refs: list[Any] = []
    with database.session_scope() as session:
        if not raw_values:
            rows = session.execute(
                select(InstrumentRegistry)
                .where(InstrumentRegistry.is_enabled.is_(True))
                .order_by(InstrumentRegistry.ticker)
            ).scalars()
            return [
                InstrumentRef(
                    instrument_id=row.instrument_id,
                    instrument_uid=row.instrument_uid,
                    figi=row.figi,
                    class_code=row.class_code,
                    ticker=row.ticker,
                )
                for row in rows
            ]
        for raw in raw_values:
            row = session.get(InstrumentRegistry, raw)
            if row is None:
                row = session.execute(
                    select(InstrumentRegistry).where(InstrumentRegistry.ticker == raw)
                ).scalars().first()
            if row is None:
                refs.append(InstrumentRef(instrument_id=raw, ticker=raw, class_code="TQBR"))
                continue
            refs.append(
                InstrumentRef(
                    instrument_id=row.instrument_id,
                    instrument_uid=row.instrument_uid,
                    figi=row.figi,
                    class_code=row.class_code,
                    ticker=row.ticker,
                )
            )
    return refs


def _default_preflight_instruments() -> str:
    return os.getenv("TRADING_INSTRUMENTS", "SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR")


def _requested_instruments_csv(value: object) -> str:
    if isinstance(value, (list, tuple)):
        instruments = [str(item).strip() for item in value if str(item).strip()]
        return ",".join(instruments) if instruments else _default_preflight_instruments()
    if isinstance(value, str) and value.strip():
        return value
    return _default_preflight_instruments()


def _mask_account_id(account_id: str | None) -> str | None:
    if not account_id:
        return None
    if len(account_id) <= 6:
        return f"{account_id[:2]}***"
    return f"{account_id[:3]}***{account_id[-3:]}"


def _reason_from_exception(exc: Exception, *, default: str) -> str:
    text = str(exc).strip()
    if text and " " not in text and len(text) <= 96:
        return text
    return default


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _datetime_or_none(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _market_trades_age_ms(trades: list[dict[str, object]]) -> int | None:
    newest: datetime | None = None
    for trade in trades:
        ts = _datetime_or_none(
            trade.get("exchange_ts")
            or trade.get("ts_utc")
            or trade.get("time")
            or trade.get("ts")
        )
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    if newest is None:
        return None
    return max(0, int((datetime.now(tz=UTC) - newest).total_seconds() * 1000))


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


def _dashboard_market_feed(app: FastAPI) -> DashboardMarketFeedService:
    service = getattr(app.state, "dashboard_market_feed", None)
    if not isinstance(service, DashboardMarketFeedService):
        service = DashboardMarketFeedService(DashboardMarketFeedConfig.from_env())
        app.state.dashboard_market_feed = service
    return service


async def _dashboard_market_feed_snapshot(
    request: Request,
    *,
    service: BffReadService,
    instruments: str | None,
    selected_instrument: str,
    include_order_book: bool,
    include_trades: bool,
    force: bool = False,
    preflight: SessionPreflightResponse | None = None,
) -> dict[str, object]:
    base_overview = _market_overview_with_cached_quotes(
        request.app,
        service.market_overview(
            instruments=instruments,
            include_details=False,
            preflight=preflight,
        ),
    )
    refs = _instrument_refs_for_preflight(
        _database_service(request),
        instruments or _default_preflight_instruments(),
    )
    feed = _dashboard_market_feed(request.app)
    timeout_seconds = float(os.environ.get("DASHBOARD_MARKET_FEED_SNAPSHOT_TIMEOUT_SECONDS", "6"))
    try:
        return await asyncio.wait_for(
            feed.snapshot(
                base_overview=base_overview,
                refs=refs,
                selected_instrument=selected_instrument,
                gateway_factory=_readonly_tbank_gateway,
                include_order_book=include_order_book,
                include_trades=include_trades,
                force=force,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return _dashboard_market_feed_timeout_snapshot(
            feed,
            base_overview=base_overview,
            selected_instrument=selected_instrument,
        )


def _dashboard_market_feed_timeout_snapshot(
    feed: DashboardMarketFeedService,
    *,
    base_overview: MarketOverviewResponse,
    selected_instrument: str,
) -> dict[str, object]:
    selected_id = selected_instrument or "MOEX:SBER"
    selected_details = next(
        (row for row in base_overview.instruments if row.instrument_id == selected_id),
        base_overview.instruments[0] if base_overview.instruments else None,
    )
    market_open = bool(selected_details and selected_details.official_exchange_open)
    session_phase = "continuous_trading" if market_open else "closed"
    status = feed.status()
    errors = list(status.get("errors") if isinstance(status.get("errors"), list) else [])
    if "dashboard_market_feed_timeout" not in errors:
        errors.insert(0, "dashboard_market_feed_timeout")
    generated_at = datetime.now(tz=UTC).isoformat()
    return {
        "generated_at": generated_at,
        "source": "dashboard_market_feed",
        "data_only_collection_required": False,
        "session": {
            "market_open": market_open,
            "session_type": (
                selected_details.session_type
                if selected_details is not None and selected_details.session_type
                else "unknown"
            ),
            "session_phase": session_phase,
            "venue_type": selected_details.venue_type if selected_details else "unknown",
            "data_only_collection_allowed": (
                selected_details.quote_allowed_for_data_collection
                if selected_details is not None
                else False
            ),
            "reason_code": "dashboard_market_feed_timeout",
            "next_session_at": None,
        },
        "quote_rows": [row.model_dump(mode="json") for row in base_overview.instruments],
        "market_overview": base_overview.model_dump(mode="json"),
        "selected_instrument": selected_id,
        "selected_details": (
            selected_details.model_dump(mode="json") if selected_details is not None else None
        ),
        "errors": errors[:5],
        "warnings": list(
            status.get("warnings") if isinstance(status.get("warnings"), list) else []
        ),
        "status": {
            **status,
            "running": bool(status.get("running")),
            "last_refresh_at": status.get("last_refresh_at") or generated_at,
            "selected_instrument": selected_id,
            "errors": errors[:5],
        },
    }


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


def _ensure_historical_api_mode(runtime_mode: RuntimeMode) -> None:
    if runtime_mode is RuntimeMode.HISTORICAL_REPLAY:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "Historical analytics are synchronous only in historical_replay/local mode; "
            "use report-worker jobs in sandbox/shadow/production."
        ),
    )


def _observatory_run_response(
    *,
    diagnostic: dict[str, Any],
    cube_rows: list[dict[str, Any]],
    candidate_config_id: UUID | None,
) -> CalibrationObservatoryRunResponse:
    return CalibrationObservatoryRunResponse(
        diagnostic_run_id=UUID(str(diagnostic["diagnostic_run_id"])),
        diagnosis=str(diagnostic["diagnosis"]),
        confidence=str(diagnostic["confidence"]),
        rolling_cube_rows=len(cube_rows),
        regime_summary=cast(dict[str, Any], diagnostic.get("regime_summary", {})),
        top_contours=_top_contours(cube_rows),
        dead_contours=_dead_contours(cube_rows),
        calibration_recommended=bool(diagnostic.get("calibration_recommended", False)),
        candidate_config_id=candidate_config_id,
        warnings=[str(item) for item in diagnostic.get("warnings", [])],
        blocking_issues=[str(item) for item in diagnostic.get("blocking_issues", [])],
        payload={
            "diagnostic": diagnostic,
            "candidate_configs_auto_applied": False,
            "approval_changes_status_only": True,
        },
    )


def _top_contours(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            float(row.get("net_pnl_proxy") or 0),
            int(row.get("candidate_count") or 0),
        ),
        reverse=True,
    )[:10]


def _dead_contours(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("sample_warning") is not None
        or int(row.get("candidate_count") or 0) == 0
        or row.get("contour_status") in {"data_only", "research_only"}
    ][:20]


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _date_from_payload(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _historical_window(
    *,
    from_date: date | None,
    to_date: date | None,
    lookback_days: int,
) -> tuple[date, date]:
    end = to_date or datetime.now(tz=UTC).date()
    start = from_date or date.fromordinal(end.toordinal() - lookback_days + 1)
    if start > end:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")
    return start, end


def _dividend_sync_status_payload(
    *,
    session: Any,
    from_date: date,
    to_date: date,
    instruments: tuple[str, ...],
    max_age_hours: int,
) -> dict[str, Any]:
    from trade_core.corporate_actions import (
        CorporateActionService,
        dividend_sync_status_payload,
    )

    service = CorporateActionService(session)
    api_events = service.list_events(
        from_date=from_date,
        to_date=to_date,
        instruments=instruments,
        source="api_import",
        action_type="dividend",
    )
    dividend_events = service.list_events(
        from_date=from_date,
        to_date=to_date,
        instruments=instruments,
        action_type="dividend",
    )
    manual_events = [row for row in dividend_events if row.source != "api_import"]
    payload = dividend_sync_status_payload(session, max_age_hours=max_age_hours)
    payload.update(
        {
            "requested_from_date": from_date.isoformat(),
            "requested_to_date": to_date.isoformat(),
            "requested_instruments": list(instruments),
            "api_import_dividend_events_count": len(api_events),
            "manual_dividend_events_count": len(manual_events),
        }
    )
    return payload


def _instrument_registry_payload(row: Any, resolved: bool) -> dict[str, Any]:
    return {
        "instrument_id": row.instrument_id,
        "ticker": row.ticker,
        "class_code": row.class_code,
        "source": row.source,
        "resolution_status": row.resolution_status,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "instrument_uid_present": bool(row.instrument_uid),
        "figi_present": bool(row.figi),
        "lot_size": row.lot_size,
        "min_price_increment": str(row.min_price_increment)
        if row.min_price_increment is not None
        else None,
        "currency": row.currency,
        "is_enabled": row.is_enabled,
        "ready_for_broker_calls": resolved,
        "resolution_error_code": row.resolution_error_code,
        "resolution_error_message": row.resolution_error_message,
    }


def _market_quote_cache(
    app: FastAPI,
) -> dict[str, tuple[MarketInstrumentOverview, datetime]]:
    cache = getattr(app.state, "market_quote_refresh_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        app.state.market_quote_refresh_cache = cache
    return cast(dict[str, tuple[MarketInstrumentOverview, datetime]], cache)


def _market_quote_refresh_lock(app: FastAPI) -> asyncio.Lock:
    lock = getattr(app.state, "market_quote_refresh_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.market_quote_refresh_lock = lock
    return cast(asyncio.Lock, lock)


def _market_quote_cache_ttl_seconds() -> float:
    return float(os.environ.get("MARKET_QUOTE_REFRESH_CACHE_TTL_SECONDS", "45"))


def _store_market_quote_cache(app: FastAPI, overview: MarketOverviewResponse) -> None:
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=_market_quote_cache_ttl_seconds())
    cache = _market_quote_cache(app)
    cacheable_sources = {
        "live_order_book_mid",
        "tbank_last_price",
        "live_exchange_order_book",
        "live_exchange_last_price",
        "broker_quote_exchange_closed",
        "broker_otc_order_book",
        "broker_indicative_quote",
    }
    for instrument in overview.instruments:
        if instrument.last_price_source in cacheable_sources:
            cache[instrument.instrument_id] = (instrument, expires_at)


def _market_overview_with_cached_quotes(
    app: FastAPI,
    overview: MarketOverviewResponse,
) -> MarketOverviewResponse:
    cache = _market_quote_cache(app)
    if not cache:
        return overview
    now = datetime.now(tz=UTC)
    instruments: list[MarketInstrumentOverview] = []
    for instrument in overview.instruments:
        cached = cache.get(instrument.instrument_id)
        if cached is None:
            instruments.append(instrument)
            continue
        cached_instrument, expires_at = cached
        if expires_at <= now:
            cache.pop(instrument.instrument_id, None)
            instruments.append(instrument)
            continue
        if not _cached_market_row_safe_for_session(instrument, cached_instrument):
            instruments.append(instrument)
            continue
        instruments.append(cached_instrument)
    return MarketOverviewResponse(generated_at=now, instruments=instruments)


def _cached_market_row_safe_for_session(
    base: MarketInstrumentOverview,
    cached: MarketInstrumentOverview,
) -> bool:
    if base.official_exchange_open:
        return True
    cached_payload = cached.quote_payload if isinstance(cached.quote_payload, dict) else {}
    cached_book = (
        cached.order_book_summary if isinstance(cached.order_book_summary, dict) else {}
    )
    return not (
        cached.official_exchange_open
        or cached.quote_allowed_for_data_collection
        or cached_payload.get("include_in_calibration") is True
        or cached_book.get("include_in_calibration") is True
        or str(cached.quote_source).startswith("live")
        or str(cached.last_price_source).startswith("live")
    )


def _dashboard_payload(app: FastAPI) -> dict[str, object]:
    database = _database_service_from_app(app)
    with database.session_scope() as session:
        service = BffReadService(session)
        status = service.robot_status(
            robot_control_state=_robot_control_from_app(app).current_state()
        )
        return {
            "robot_status": status,
            "market": _market_overview_with_cached_quotes(app, service.market_overview()),
            "open_orders": service.open_orders(),
            "positions": service.positions(),
            "signals": service.current_signals(),
        }


def _orders_payload(app: FastAPI) -> dict[str, object]:
    database = _database_service_from_app(app)
    with database.session_scope() as session:
        return {"orders": BffReadService(session).open_orders()}


def _market_payload(app: FastAPI) -> MarketOverviewResponse:
    database = _database_service_from_app(app)
    with database.session_scope() as session:
        return _market_overview_with_cached_quotes(
            app,
            BffReadService(session).market_overview(),
        )


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

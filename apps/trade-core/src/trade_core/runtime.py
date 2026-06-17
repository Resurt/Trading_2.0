"""Long-lived trade-core runtime orchestration."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Thread
from time import perf_counter
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from celery import Celery
from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_core.app import create_identity
from trade_core.broker_gateway import (
    AccountsRequest,
    BrokerGateway,
    BrokerUnaryResponse,
    CancelOrderRequest,
    CandleRequest,
    InstrumentRef,
    LastPricesRequest,
    OrderBookRequest,
    OrderPlacementRequest,
    OrdersRequest,
    OrderStateRequest,
    PortfolioRequest,
    PositionsRequest,
    RequestMetadata,
    StopOrderPlacementRequest,
    StreamEvent,
    TradingSchedulesRequest,
    TradingStatusRequest,
)
from trade_core.infra.tbank import (
    TBankBrokerConfig,
    TBankBrokerGateway,
    load_tbank_tokens_for_launch,
)
from trade_core.market_data import (
    Bar,
    BarEngine,
    Candle,
    MarketDataEvent,
    MarketDataPipeline,
    MarketDataSubscriptionConfig,
    MarketDataSubscriptionService,
    MarketEventBus,
    MarketEventType,
    MarketReadModelStore,
    MarketState,
    MarketStateCalculator,
    OrderBookSnapshot,
    StreamGapRecoveryService,
    Timeframe,
)
from trade_core.market_data.persistence import SqlAlchemyMarketDataStore
from trade_core.market_data.recovery import GapRecoveryRequest
from trade_core.portfolio import PositionService
from trade_core.session import (
    BrokerTradingStatus,
    HourlyMicroSessionConfig,
    HourlyMicroSessionManager,
    ScheduleWindow,
    SessionEventContext,
    SessionManager,
    SessionSnapshot,
    SqlAlchemySessionStateStore,
    TradingSchedule,
)
from trade_core.strategy import (
    ConfigDrivenStrategyConfig,
    ConfigDrivenStrategyEngine,
    DefaultExecutionEngine,
    DefaultReconciliationService,
    DefaultRiskEngine,
    OrderIntentRequest,
    PortfolioSnapshot,
    RiskAssessmentInput,
    RiskLimits,
    SignalCandidateDecision,
    SqlAlchemyStrategyEventStore,
    StrategyEvaluationContext,
    StrategyState,
)
from trading_common import LaunchModePolicy, RuntimeMode, ServiceName, TradingMetrics
from trading_common.db.base import Base
from trading_common.db.models import AuditEvent, InstrumentRegistry, OrderIntent, RobotCommand
from trading_common.db.repositories import (
    BlockerEventRepository,
    CandidateStageResultRepository,
    InstrumentRepository,
    MarketContextSnapshotRepository,
    OrderRepository,
    RiskEventRepository,
    RobotCommandRepository,
    SignalCandidateRepository,
    StrategyStateEventRepository,
)
from trading_common.db.service import DatabaseService
from trading_common.enums import SessionPhase, SessionType
from trading_common.observability import DomainEventType
from trading_common.report_jobs import REPORTS_QUEUE, ReportJobDispatcher
from trading_common.telemetry import get_logger, log_event

JsonPayload = dict[str, Any]
MSK = ZoneInfo("Europe/Moscow")
LOGGER = get_logger(__name__)
DEFAULT_ACCOUNT_ID = "local-runtime-account"
DEFAULT_DATABASE_PATH = Path(".local/trade_core_runtime.db")
DEFAULT_EXCHANGE = "MOEX"
DEFAULT_INSTRUMENTS: tuple[InstrumentRef, ...] = (
    InstrumentRef(
        instrument_id="MOEX:SBER",
        instrument_uid="sber-runtime-placeholder",
        class_code="TQBR",
        ticker="SBER",
    ),
)


@dataclass(frozen=True, slots=True)
class TradeCoreRuntimeConfig:
    """Configuration for the long-lived runtime loop."""

    account_id: str = DEFAULT_ACCOUNT_ID
    exchange: str = DEFAULT_EXCHANGE
    instruments: tuple[InstrumentRef, ...] = DEFAULT_INSTRUMENTS
    tick_interval_seconds: float = 1.0
    database_url: str | None = None
    auto_create_sqlite_schema: bool = False
    micro_session_freeze_seconds: int = 90
    position_snapshot_freshness_seconds: int = 900
    stream_names: tuple[str, ...] = (
        "candles",
        "order_book",
        "last_prices",
        "trading_status",
        "info",
        "market_trades",
    )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TradeCoreRuntimeConfig:
        env = environ if environ is not None else os.environ
        database_url = env.get("TRADING_DATABASE_URL") or env.get("DATABASE_URL")
        auto_create_sqlite_schema = False
        if not database_url:
            DEFAULT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite+pysqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
            auto_create_sqlite_schema = True

        return cls(
            account_id=env.get("TRADING_ACCOUNT_ID", DEFAULT_ACCOUNT_ID),
            exchange=env.get("TRADING_EXCHANGE", DEFAULT_EXCHANGE),
            instruments=_instruments_from_env(env.get("TRADING_INSTRUMENTS")),
            tick_interval_seconds=float(env.get("TRADE_CORE_TICK_INTERVAL_SECONDS", "1.0")),
            database_url=database_url,
            auto_create_sqlite_schema=auto_create_sqlite_schema,
            micro_session_freeze_seconds=int(
                env.get("TRADE_CORE_MICRO_SESSION_FREEZE_SECONDS", "90")
            ),
            position_snapshot_freshness_seconds=int(
                env.get("TRADE_CORE_POSITION_SNAPSHOT_FRESHNESS_SECONDS", "900")
            ),
        )


@dataclass(slots=True)
class TradeCoreRuntimeStats:
    """Small read model used by metrics sampling and tests."""

    started: bool = False
    stream_tasks_started: int = 0
    cycles: int = 0
    processed_closed_bars: int = 0
    candidates_created: int = 0
    order_intents_created: int = 0
    report_requests: list[JsonPayload] = field(default_factory=list)
    last_stream_message_at: datetime | None = None
    open_orders: int = 0
    active_positions: int = 0


class SafeNoopBrokerGateway:
    """SDK-neutral broker gateway used by replay tests and safe local startup."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self.now = now
        self.post_order_calls: list[OrderPlacementRequest] = []
        self.cancel_order_calls: list[CancelOrderRequest] = []

    async def trading_schedules(
        self,
        request: TradingSchedulesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        moment = self.now or request.from_
        return BrokerUnaryResponse(
            method_name="TradingSchedules",
            data={
                "windows": [
                    _window_payload(window) for window in default_trading_schedule(moment).windows
                ],
            },
            headers={},
        )

    async def get_trading_status(
        self,
        request: TradingStatusRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        now = self.now or datetime.now(tz=MSK)
        window = default_trading_schedule(now).active_window(now)
        status = "normal_trading" if window is not None else "closed"
        return BrokerUnaryResponse(
            method_name="GetTradingStatus",
            data={
                "instrument_id": request.instrument.instrument_id,
                "trading_status": status,
                "api_trade_available": window is not None,
                "exchange_ts": now.isoformat(),
            },
            headers={},
        )

    async def get_candles(
        self,
        request: CandleRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(method_name="GetCandles", data={"candles": []}, headers={})

    async def get_last_prices(
        self,
        request: LastPricesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(method_name="GetLastPrices", data={"prices": []}, headers={})

    async def get_order_book(
        self,
        request: OrderBookRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="GetOrderBook",
            data={"instrument_id": request.instrument.instrument_id, "bids": [], "asks": []},
            headers={},
        )

    async def post_order(
        self,
        request: OrderPlacementRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.post_order_calls.append(request)
        return BrokerUnaryResponse(
            method_name="PostOrder",
            data={
                "exchange_order_id": f"noop-{request.request_order_id}",
                "broker_status": "posted",
            },
            headers={"x-tracking-id": "noop-tracking"},
        )

    async def cancel_order(
        self,
        request: CancelOrderRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.cancel_order_calls.append(request)
        return BrokerUnaryResponse(
            method_name="CancelOrder",
            data={"exchange_order_id": request.exchange_order_id, "broker_status": "cancelled"},
            headers={},
        )

    async def get_order_state(
        self,
        request: OrderStateRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="GetOrderState",
            data={
                "request_order_id": (
                    str(request.request_order_id) if request.request_order_id else None
                ),
                "exchange_order_id": request.exchange_order_id,
                "broker_status": "observed",
            },
            headers={},
        )

    async def get_orders(
        self,
        request: OrdersRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(method_name="GetOrders", data={"orders": []}, headers={})

    async def post_stop_order(
        self,
        request: StopOrderPlacementRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="PostStopOrder",
            data={
                "exchange_order_id": f"noop-stop-{request.request_order_id}",
                "broker_status": "posted",
            },
            headers={},
        )

    async def reconcile_order_state(
        self,
        request: OrderStateRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self.get_order_state(request, metadata)

    async def reconcile_open_orders(
        self,
        request: OrdersRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self.get_orders(request, metadata)

    async def get_portfolio(
        self,
        request: PortfolioRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="GetPortfolio",
            data={
                "account_id": request.account_id,
                "positions": [],
                "total_amount_portfolio": "0",
                "expected_yield": "0",
                "available_margin": "0",
            },
            headers={},
        )

    async def get_positions(
        self,
        request: PositionsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="GetPositions",
            data={"account_id": request.account_id, "positions": []},
            headers={},
        )

    async def get_accounts(
        self,
        request: AccountsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetAccounts",
            data={"accounts": [{"account_id": DEFAULT_ACCOUNT_ID, "status": "local"}]},
            headers={},
        )

    async def stream_market_data(self, stream_name: str) -> AsyncIterator[StreamEvent]:
        del stream_name
        if False:
            yield StreamEvent(stream_name="noop", event_type="noop", payload={})

    async def stream_orders(self, account_id: str) -> AsyncIterator[StreamEvent]:
        del account_id
        if False:
            yield StreamEvent(stream_name="noop-orders", event_type="noop", payload={})

    async def recover_after_stream_gap(
        self,
        stream_name: str,
        account_id: str | None = None,
    ) -> None:
        log_event(
            logger=LOGGER,
            level="WARNING",
            event_type=DomainEventType.STREAM_GAP_RECOVERY_REQUESTED.value,
            component="runtime.noop_gateway",
            stream_name=stream_name,
            account_id_present=account_id is not None,
        )


class TradeCoreRuntime:
    """Orchestrates sessions, market data, strategy, risk, execution and journaling."""

    def __init__(
        self,
        *,
        config: TradeCoreRuntimeConfig | None = None,
        launch_policy: LaunchModePolicy | None = None,
        database: DatabaseService | None = None,
        broker_gateway: BrokerGateway | None = None,
        metrics: TradingMetrics | None = None,
        strategy_config: ConfigDrivenStrategyConfig | None = None,
        risk_limits: RiskLimits | None = None,
        report_job_dispatcher: ReportJobDispatcher | None = None,
    ) -> None:
        self.config = config or TradeCoreRuntimeConfig.from_env()
        self.launch_policy = launch_policy or LaunchModePolicy.from_env()
        self.launch_policy.validate_startup()
        self.identity = create_identity(self.launch_policy.mode)
        self.database = database or DatabaseService(_required_database_url(self.config))
        self.metrics = metrics or TradingMetrics(self.identity)
        self.report_job_dispatcher = report_job_dispatcher or _build_report_job_dispatcher()
        if self.config.auto_create_sqlite_schema:
            Base.metadata.create_all(self.database.engine)

        self.broker_gateway = broker_gateway or self._build_broker_gateway()
        self.session_manager = SessionManager()
        self.market_event_bus = MarketEventBus()
        self.bar_engine = BarEngine()
        self.market_state_calculator = MarketStateCalculator()
        self.market_read_model_store = MarketReadModelStore(
            market_state_calculator=self.market_state_calculator
        )
        self.market_data_subscription_service = MarketDataSubscriptionService(
            broker_gateway=self.broker_gateway,
            event_bus=self.market_event_bus,
        )
        self.strategy_config = strategy_config or ConfigDrivenStrategyConfig.conservative_default()
        self.strategy_engine = ConfigDrivenStrategyEngine(self.strategy_config)
        self.risk_engine = DefaultRiskEngine()
        self.risk_limits = risk_limits or RiskLimits.from_strategy_config(self.strategy_config)
        self.runtime_id = uuid4()
        self.stats = TradeCoreRuntimeStats()
        self.robot_control_state = "running"

        self._session: Session | None = None
        self._session_state_store: SqlAlchemySessionStateStore | None = None
        self.hourly_micro_session_manager: HourlyMicroSessionManager | None = None
        self.market_data_store: SqlAlchemyMarketDataStore | None = None
        self.market_data_pipeline: MarketDataPipeline | None = None
        self.stream_gap_recovery_service: StreamGapRecoveryService | None = None
        self.position_service: PositionService | None = None
        self.execution_engine: DefaultExecutionEngine | None = None
        self.reconciliation_service: DefaultReconciliationService | None = None
        self.strategy_event_store: SqlAlchemyStrategyEventStore | None = None

        self._stream_tasks: tuple[asyncio.Task[None], ...] = ()
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._current_schedule: TradingSchedule | None = None
        self._current_snapshot: SessionSnapshot | None = None
        self._latest_market_states: dict[str, MarketState] = {}
        self._latest_closed_bars: dict[str, dict[Timeframe, Bar]] = {}
        self._strategy_states: dict[str, StrategyState] = {}

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        broker_gateway: BrokerGateway | None = None,
        database: DatabaseService | None = None,
    ) -> TradeCoreRuntime:
        env = environ if environ is not None else os.environ
        policy = LaunchModePolicy.from_env(env)
        return cls(
            config=TradeCoreRuntimeConfig.from_env(env),
            launch_policy=policy,
            database=database,
            broker_gateway=broker_gateway,
        )

    @property
    def current_snapshot(self) -> SessionSnapshot | None:
        return self._current_snapshot

    @property
    def stream_tasks(self) -> tuple[asyncio.Task[None], ...]:
        return self._stream_tasks

    async def start(self) -> None:
        """Initialize DB-backed stores, subscriptions and stream tasks."""

        if self.stats.started:
            return
        self._session = self.database.session_factory()
        self._ensure_instruments_registered()
        session = self._require_session()
        self._session_state_store = SqlAlchemySessionStateStore(
            session,
            strategy_id=self.strategy_config.strategy_id,
            strategy_version=self.strategy_config.strategy_version,
        )
        self.hourly_micro_session_manager = HourlyMicroSessionManager(
            store=self._session_state_store,
            config=HourlyMicroSessionConfig(
                freeze_seconds=self.config.micro_session_freeze_seconds
            ),
        )
        self.market_data_store = SqlAlchemyMarketDataStore(session)
        self.market_data_pipeline = MarketDataPipeline(
            event_bus=self.market_event_bus,
            session_context_provider=self._session_context_for,
            bar_engine=self.bar_engine,
            read_models=self.market_read_model_store,
            store=self.market_data_store,
        )
        self.market_data_pipeline.register()
        self.market_event_bus.subscribe(MarketEventType.BAR_CLOSED, self._handle_closed_bar)
        self.market_event_bus.subscribe(
            MarketEventType.MARKET_STATE_UPDATED,
            self._handle_market_state_updated,
        )
        self.market_event_bus.subscribe(MarketEventType.CANDLE, self._handle_market_metrics)
        self.market_event_bus.subscribe(MarketEventType.ORDER_BOOK, self._handle_market_metrics)
        self.market_event_bus.subscribe(MarketEventType.TRADING_STATUS, self._handle_market_metrics)

        order_repository = OrderRepository(session)
        self.execution_engine = DefaultExecutionEngine(
            broker_gateway=self.broker_gateway,
            orders=order_repository,
            launch_policy=self.launch_policy,
        )
        self.reconciliation_service = DefaultReconciliationService(
            broker_gateway=self.broker_gateway,
            orders=order_repository,
        )
        self.strategy_event_store = SqlAlchemyStrategyEventStore(
            candidates=SignalCandidateRepository(session),
            blockers=BlockerEventRepository(session),
            risk_events=RiskEventRepository(session),
            state_events=StrategyStateEventRepository(session),
            candidate_stages=CandidateStageResultRepository(session),
            market_contexts=MarketContextSnapshotRepository(session),
        )
        self.position_service = PositionService(
            broker_gateway=self.broker_gateway,
            session=session,
            session_context_provider=self._session_context_for,
            tracked_instruments=self.config.instruments,
            metrics=self.metrics,
            freshness_seconds=self.config.position_snapshot_freshness_seconds,
        )
        self.stream_gap_recovery_service = StreamGapRecoveryService(
            broker_gateway=self.broker_gateway,
            event_bus=self.market_event_bus,
            refresh_positions_hook=self._refresh_positions_after_gap,
            metrics=self.metrics,
            audit_event_hook=self._write_recovery_audit_event,
            on_failure=self._mark_stream_recovery_degraded,
        )
        self._install_broker_gap_recovery_hook()
        self._stream_tasks = await self.market_data_subscription_service.start(
            MarketDataSubscriptionConfig(
                market_stream_names=self.config.stream_names,
                account_id=self.config.account_id,
            )
        )
        self.stats.stream_tasks_started = len(self._stream_tasks)
        self.stats.started = True
        self.flush_domain_events()
        log_event(
            logger=LOGGER,
            event_type="trade_core_runtime_started",
            component="runtime",
            runtime_id=str(self.runtime_id),
            launch_mode=self.launch_policy.mode.value,
            stream_tasks=self.stats.stream_tasks_started,
        )

    async def run_forever(self) -> None:
        """Run the runtime loop until `request_stop` or task cancellation."""

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        await self.start()
        try:
            while not self._stop_event.is_set():
                await self.run_cycle()
                await asyncio.sleep(self.config.tick_interval_seconds)
        finally:
            await self.shutdown()

    def start_background(self) -> Thread:
        """Start `run_forever` in a daemon thread for the HTTP entrypoint."""

        if self._thread is not None and self._thread.is_alive():
            return self._thread

        def _runner() -> None:
            asyncio.run(self.run_forever())

        self._thread = Thread(target=_runner, name="trade-core-runtime", daemon=True)
        self._thread.start()
        return self._thread

    def request_stop(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    async def shutdown(self) -> None:
        """Cancel streams, flush events, write audit row and close resources."""

        for task in self._stream_tasks:
            task.cancel()
        if self._stream_tasks:
            await asyncio.gather(*self._stream_tasks, return_exceptions=True)
        self._stream_tasks = ()
        self.metrics.set_market_stream_alive(False, stream_type="market_data")
        self._write_audit_event(action="trade_core_runtime_shutdown")
        self.flush_domain_events()
        session = self._session
        if session is not None:
            session.close()
            self._session = None
        self.database.engine.dispose()
        self.stats.started = False
        log_event(
            logger=LOGGER,
            event_type="trade_core_runtime_stopped",
            component="runtime",
            runtime_id=str(self.runtime_id),
        )

    async def run_cycle(self, *, now: datetime | None = None) -> SessionSnapshot:
        """Refresh schedule/status, advance micro-session state and reconcile safe state."""

        if not self.stats.started:
            await self.start()
        self.process_robot_commands()
        observed_at = _ensure_msk(now or datetime.now(tz=MSK))
        instrument = self.config.instruments[0]
        schedule = await self.refresh_trading_schedule(now=observed_at)
        broker_status = await self.refresh_broker_trading_status(
            instrument=instrument,
            now=observed_at,
        )
        snapshot = self.session_manager.evaluate(
            now=observed_at,
            schedule=schedule,
            broker_status=broker_status,
        )
        micro_manager = self._require_micro_session_manager()
        rollover_started = perf_counter()
        previous_snapshot = self._current_snapshot
        result = micro_manager.on_snapshot(snapshot)
        for event in result.events:
            if event.event_type == "snapshot_taken":
                self._current_snapshot = (
                    previous_snapshot.with_micro_session(event.micro_session_id)
                    if previous_snapshot is not None
                    else result.snapshot.with_micro_session(event.micro_session_id)
                )
                await self._snapshot_positions(
                    reason="micro_session_snapshot_taken",
                    now=event.observed_at,
                )
            if event.event_type == "session_run_opened":
                self._current_snapshot = result.snapshot.with_micro_session(
                    event.micro_session_id
                )
                await self._snapshot_positions(
                    reason="micro_session_session_run_opened",
                    now=event.observed_at,
                )
            if event.event_type == "report_requested":
                self.stats.report_requests.append(dict(event.payload))
            if event.event_type in {"session_run_closed", "report_requested"}:
                metric_session_type = snapshot.session_type.value
                if isinstance(event.payload, dict):
                    raw_session_type = event.payload.get("session_type")
                    if isinstance(raw_session_type, str):
                        metric_session_type = raw_session_type
                self.metrics.observe_session_rollover_duration(
                    perf_counter() - rollover_started,
                    session_type=metric_session_type,
                    status="success",
                )
        self._current_snapshot = result.snapshot
        self.stats.cycles += 1
        self.flush_domain_events()
        self.dispatch_report_jobs()
        return result.snapshot

    async def refresh_trading_schedule(self, *, now: datetime) -> TradingSchedule:
        """Fetch broker schedule and keep a parsed fallback for local replay."""

        try:
            response = await self.broker_gateway.trading_schedules(
                TradingSchedulesRequest(
                    exchange=self.config.exchange,
                    from_=now - timedelta(days=1),
                    to=now + timedelta(days=1),
                )
            )
            schedule = trading_schedule_from_response(response, now=now)
        except Exception as exc:
            log_event(
                logger=LOGGER,
                level="WARNING",
                event_type="trading_schedule_refresh_failed",
                component="runtime",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            schedule = (
                TradingSchedule(windows=())
                if self.launch_policy.allows_real_orders
                else default_trading_schedule(now)
            )
        self._current_schedule = schedule
        return schedule

    async def refresh_broker_trading_status(
        self,
        *,
        instrument: InstrumentRef,
        now: datetime,
    ) -> BrokerTradingStatus:
        """Fetch SDK-neutral trading status with deterministic fallback."""

        try:
            response = await self.broker_gateway.get_trading_status(
                TradingStatusRequest(instrument=instrument)
            )
            return broker_status_from_response(
                response,
                instrument_id=instrument.instrument_id,
                now=now,
            )
        except Exception as exc:
            log_event(
                logger=LOGGER,
                level="WARNING",
                event_type="broker_trading_status_refresh_failed",
                component="runtime",
                instrument_id=instrument.instrument_id,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            if self.launch_policy.allows_real_orders:
                return BrokerTradingStatus(
                    status="closed",
                    api_trade_available=False,
                    instrument_id=instrument.instrument_id,
                    exchange_ts=now,
                    raw_payload={
                        "source": "runtime_safe_fallback",
                        "reason_code": "broker_status_unavailable",
                    },
                )
            window = (self._current_schedule or default_trading_schedule(now)).active_window(now)
            return BrokerTradingStatus(
                status="normal_trading" if window is not None else "closed",
                api_trade_available=window is not None,
                instrument_id=instrument.instrument_id,
                exchange_ts=now,
                raw_payload={"source": "runtime_fallback"},
            )

    async def process_candle(self, candle: Candle) -> None:
        """Test/replay helper: send a candle through the same event bus as streams."""

        await self.market_event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.CANDLE,
                payload=candle,
                ts_utc=candle.close_ts_utc,
                instrument_id=candle.instrument_id,
            )
        )
        self.flush_domain_events()

    async def process_order_book(self, order_book: OrderBookSnapshot) -> None:
        """Test/replay helper: send order book snapshot through live read model pipeline."""

        await self.market_event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.ORDER_BOOK,
                payload=order_book,
                ts_utc=order_book.received_ts,
                instrument_id=order_book.instrument_id,
            )
        )
        self.flush_domain_events()

    def sample_metrics(self, metrics: TradingMetrics | None = None) -> None:
        """Refresh runtime gauges before `/metrics` rendering."""

        target = metrics or self.metrics
        target.set_open_orders(self.stats.open_orders)
        target.set_active_positions(self.stats.active_positions, instrument="all")
        if self.stats.last_stream_message_at is not None:
            age = (
                datetime.now(tz=UTC) - self.stats.last_stream_message_at.astimezone(UTC)
            ).total_seconds()
            target.set_last_stream_message_age(
                max(0.0, age),
                stream_type="market_data",
                instrument="all",
                timeframe="all",
            )

    def flush_domain_events(self) -> None:
        session = self._session
        if session is not None:
            session.commit()

    def dispatch_report_jobs(self) -> None:
        session = self._session
        if session is None:
            return
        dispatched = self.report_job_dispatcher.dispatch_pending(session)
        session.commit()
        for job in dispatched:
            log_event(
                logger=LOGGER,
                event_type="report_job_enqueued",
                component="runtime.report_jobs",
                report_job_id=str(job.report_job_id),
                celery_task_id=job.celery_task_id,
                report_type=job.report_type,
                micro_session_id=job.micro_session_id,
                strategy_id=job.strategy_id,
            )

    def process_robot_commands(self) -> int:
        """Apply durable operator commands written by the API control plane."""

        session = self._session
        if session is None:
            return 0
        repository = RobotCommandRepository(session)
        commands = repository.list_requested()
        processed = 0
        for command in commands:
            now = datetime.now(tz=UTC)
            previous_state = self.robot_control_state
            repository.mark_accepted(command, accepted_at=now)
            try:
                reason_code, result_payload = self._apply_robot_command(command)
                result_payload = {
                    **result_payload,
                    "previous_robot_control_state": previous_state,
                    "robot_control_state": self.robot_control_state,
                }
                repository.mark_applied(
                    command,
                    applied_at=now,
                    reason_code=reason_code,
                    result_payload=result_payload,
                )
                self._write_robot_command_audit(command=command, payload=result_payload)
                processed += 1
            except Exception as exc:
                repository.mark_failed(
                    command,
                    failed_at=now,
                    reason_code="runtime_command_failed",
                    error=str(exc),
                    result_payload={"previous_robot_control_state": previous_state},
                )
                self._write_robot_command_audit(
                    command=command,
                    payload={
                        "previous_robot_control_state": previous_state,
                        "error_code": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
        if commands:
            session.commit()
        return processed

    def _apply_robot_command(self, command: RobotCommand) -> tuple[str, JsonPayload]:
        command_type = command.command_type
        if command_type in {"start", "resume"}:
            self.robot_control_state = "running"
            return "runtime_entries_enabled", {}
        if command_type == "pause":
            self.robot_control_state = "paused"
            return "runtime_entries_paused", {"new_entries_allowed": False}
        if command_type == "stop":
            self.robot_control_state = "stopped"
            self._strategy_states = {
                instrument_id: StrategyState.STOPPED for instrument_id in self._strategy_states
            }
            return "runtime_safe_stopped", {"new_entries_allowed": False}
        if command_type == "emergency_stop":
            cancelled_open_orders = self.stats.open_orders
            self.robot_control_state = "emergency_stopped"
            self.stats.open_orders = 0
            self.metrics.set_open_orders(0)
            self._strategy_states = {
                instrument_id: StrategyState.STOPPED for instrument_id in self._strategy_states
            }
            return (
                "runtime_emergency_stopped",
                {
                    "new_entries_allowed": False,
                    "cancel_reason_code": "manual_operator_emergency_stop",
                    "cancelled_open_orders": cancelled_open_orders,
                },
            )
        msg = f"unsupported robot command: {command_type}"
        raise ValueError(msg)

    async def _snapshot_positions(self, *, reason: str, now: datetime) -> None:
        position_service = self.position_service
        if position_service is None:
            return
        try:
            result = await position_service.refresh_positions(
                self.config.account_id,
                reason=reason,
                now=now,
            )
        except Exception as exc:
            log_event(
                logger=LOGGER,
                level="WARNING",
                event_type="position_snapshot_failed",
                component="runtime.positions",
                error_code=type(exc).__name__,
                error_message=str(exc),
                snapshot_reason=reason,
            )
            return
        self.stats.active_positions = _active_position_lots(result.portfolio)

    async def _portfolio_for_risk(
        self,
        *,
        instrument_id: str,
        observed_at: datetime,
    ) -> PortfolioSnapshot:
        position_service = self.position_service
        if position_service is None:
            return PortfolioSnapshot(
                open_order_count=self.stats.open_orders,
                open_position_lots=self.stats.active_positions,
                position_state_fresh=False,
                position_reconciliation_matched=False,
                position_reason_code="position_service_unavailable",
            )
        try:
            validation = await position_service.validate_before_entry(
                account_id=self.config.account_id,
                instrument_id=instrument_id,
                now=observed_at,
                max_age_seconds=self.config.position_snapshot_freshness_seconds,
            )
        except Exception as exc:
            log_event(
                logger=LOGGER,
                level="WARNING",
                event_type="position_reconciliation_failed",
                component="runtime.positions",
                instrument_id=instrument_id,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            return PortfolioSnapshot(
                open_order_count=self.stats.open_orders,
                open_position_lots=self.stats.active_positions,
                position_state_fresh=False,
                position_reconciliation_matched=False,
                position_reason_code="position_reconciliation_failed",
            )
        self.risk_limits = replace(
            self.risk_limits,
            short_allowed_by_account=validation.refresh.short_allowed_by_account,
            short_allowed_by_instrument=validation.refresh.short_allowed_for(instrument_id),
        )
        self.stats.active_positions = _active_position_lots(validation.refresh.portfolio)
        return replace(validation.portfolio, open_order_count=self.stats.open_orders)

    def _build_broker_gateway(self) -> BrokerGateway:
        if self.launch_policy.mode is RuntimeMode.HISTORICAL_REPLAY:
            return cast(BrokerGateway, SafeNoopBrokerGateway())
        config = TBankBrokerConfig.from_launch_policy(self.launch_policy)
        tokens = load_tbank_tokens_for_launch(self.launch_policy)
        return cast(BrokerGateway, TBankBrokerGateway(config=config, tokens=tokens))

    def _install_broker_gap_recovery_hook(self) -> None:
        setter = getattr(self.broker_gateway, "set_stream_gap_recovery_hook", None)
        if callable(setter):
            setter(self._recover_stream_gap_from_gateway)

    async def _recover_stream_gap_from_gateway(
        self,
        stream_name: str,
        account_id: str | None,
    ) -> None:
        recovery_service = self.stream_gap_recovery_service
        if recovery_service is None:
            return
        now = datetime.now(tz=UTC)
        await recovery_service.recover_after_reconnect(
            GapRecoveryRequest(
                instruments=self.config.instruments,
                candle_timeframes=(Timeframe.M1,),
                from_ts_utc=self._gap_recovery_from_ts(
                    stream_name=stream_name,
                    fallback=now - timedelta(minutes=30),
                ),
                to_ts_utc=now,
                account_id=account_id,
                stream_name=stream_name,
                working_order_request_ids=self._working_order_request_ids(),
            )
        )

    def _gap_recovery_from_ts(self, *, stream_name: str, fallback: datetime) -> datetime:
        recovery_service = self.stream_gap_recovery_service
        if recovery_service is None:
            return fallback
        candidates: list[datetime] = []
        for instrument in self.config.instruments:
            last_good = recovery_service.last_good_event_ts(
                stream_name=stream_name,
                instrument_id=instrument.instrument_id,
                timeframe=Timeframe.M1,
            )
            if last_good is not None:
                candidates.append(last_good)
        return min(candidates) if candidates else fallback

    def _working_order_request_ids(self) -> tuple[UUID, ...]:
        session = self._session
        if session is None:
            return ()
        stmt = select(OrderIntent.request_order_id).where(
            OrderIntent.status.in_(("submitted", "cancel_requested", "partially_filled"))
        )
        return tuple(session.execute(stmt).scalars())

    def _ensure_instruments_registered(self) -> None:
        repository = InstrumentRepository(self._require_session())
        for instrument in self.config.instruments:
            repository.upsert(
                InstrumentRegistry(
                    instrument_id=instrument.instrument_id,
                    ticker=instrument.ticker or instrument.instrument_id.rsplit(":", 1)[-1],
                    class_code=instrument.class_code or "TQBR",
                    figi=None,
                    instrument_uid=instrument.instrument_uid,
                    name=instrument.ticker or instrument.instrument_id,
                    lot_size=1,
                    min_price_increment=Decimal("0.01"),
                    currency="RUB",
                    is_enabled=True,
                    supports_morning=True,
                    supports_evening=True,
                    supports_weekend=False,
                    instrument_payload={"source": "trade_core_runtime_bootstrap"},
                )
            )
        self.flush_domain_events()

    def _session_context_for(self, instrument_id: str) -> SessionEventContext:
        del instrument_id
        snapshot = self._current_snapshot or self._fallback_snapshot()
        micro_session_id = snapshot.micro_session_id or "unassigned"
        return snapshot.event_context(micro_session_id)

    async def _handle_market_metrics(self, event: MarketDataEvent) -> None:
        self.stats.last_stream_message_at = event.ts_utc
        stream_type = event.event_type.value
        instrument = event.instrument_id or "all"
        timeframe = "all"
        if isinstance(event.payload, Candle):
            timeframe = event.payload.timeframe.value
            if event.payload.is_closed:
                lag_seconds = max(
                    0.0,
                    (
                        event.ts_utc.astimezone(UTC) - event.payload.close_ts_utc.astimezone(UTC)
                    ).total_seconds(),
                )
                self.metrics.observe_candle_close_delivery_lag(
                    lag_seconds,
                    instrument_id=event.payload.instrument_id,
                    timeframe=event.payload.timeframe.value,
                )
        self.metrics.set_market_stream_alive(
            True,
            stream_type=stream_type,
            instrument=instrument,
            timeframe=timeframe,
        )
        self.metrics.set_last_stream_message_age(
            0.0,
            stream_type=stream_type,
            instrument=instrument,
            timeframe=timeframe,
        )
        if self.stream_gap_recovery_service is not None:
            self.stream_gap_recovery_service.record_good_event(
                stream_name=_stream_name_for_event(event),
                instrument_id=event.instrument_id,
                timeframe=timeframe if timeframe != "all" else None,
                ts_utc=event.ts_utc,
            )

    async def _handle_market_state_updated(self, event: MarketDataEvent) -> None:
        if isinstance(event.payload, MarketState):
            self._latest_market_states[event.payload.instrument_id] = event.payload

    async def _handle_closed_bar(self, event: MarketDataEvent) -> None:
        if not isinstance(event.payload, Bar):
            return
        bar = event.payload
        if not bar.is_closed:
            return
        self.stats.processed_closed_bars += 1
        self._latest_closed_bars.setdefault(bar.instrument_id, {})[bar.timeframe] = bar
        await self._evaluate_strategy_on_closed_bar(bar)
        self.flush_domain_events()

    async def _evaluate_strategy_on_closed_bar(self, bar: Bar) -> None:
        snapshot = self._current_snapshot
        if snapshot is None or snapshot.micro_session_id is None:
            return
        if self.robot_control_state != "running":
            previous_state = self._strategy_states.get(bar.instrument_id, StrategyState.IDLE)
            self._record_strategy_transition(
                instrument_id=bar.instrument_id,
                previous_state=previous_state,
                new_state=StrategyState.STOPPED,
                event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
                reason_code=f"robot_control_{self.robot_control_state}",
                payload={"closed_bar_ts": bar.close_ts_utc.isoformat()},
            )
            self._strategy_states[bar.instrument_id] = StrategyState.STOPPED
            return
        instrument = self._instrument_for(bar.instrument_id)
        previous_state = self._strategy_states.get(bar.instrument_id, StrategyState.IDLE)
        market_state = self._latest_market_states.get(bar.instrument_id)
        decision = self.strategy_engine.evaluate(
            context=StrategyEvaluationContext(
                instrument=instrument,
                session_snapshot=snapshot,
                latest_closed_bars=self._latest_closed_bars.get(bar.instrument_id, {}),
                market_state=market_state,
                current_state=previous_state,
                now=bar.close_ts_utc,
            )
        )
        event_store = self._require_strategy_event_store()
        self._record_strategy_transition(
            instrument_id=bar.instrument_id,
            previous_state=decision.previous_state,
            new_state=decision.next_state,
            event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
            reason_code=decision.reason_code,
            payload=decision.decision_payload,
        )
        self._strategy_states[bar.instrument_id] = decision.next_state

        for candidate_decision in decision.candidates:
            candidate = event_store.record_candidate(
                decision=candidate_decision,
                snapshot=snapshot,
                market_state=market_state,
                run_id=self._current_run_id(),
                ts_utc=bar.close_ts_utc,
            )
            self.stats.candidates_created += 1
            candidate_with_id = replace(candidate_decision, candidate_id=candidate.candidate_id)
            risk_decision = self.risk_engine.evaluate(
                RiskAssessmentInput(
                    candidate=candidate_with_id,
                    session_snapshot=snapshot,
                    market_state=market_state,
                    limits=self.risk_limits,
                    portfolio=await self._portfolio_for_risk(
                        instrument_id=bar.instrument_id,
                        observed_at=bar.close_ts_utc,
                    ),
                )
            )
            blockers = event_store.record_blockers(
                candidate=candidate,
                decision=risk_decision,
                market_state=market_state,
                ts_utc=bar.close_ts_utc,
            )
            risk_events = event_store.record_risk_events(
                candidate=candidate,
                decision=risk_decision,
                ts_utc=bar.close_ts_utc,
            )
            for risk_event in risk_events:
                self.metrics.inc_risk_event(reason_code=risk_event.reason_code)

            if not risk_decision.allowed:
                final_blocker = risk_decision.final_blocker
                self._record_strategy_transition(
                    instrument_id=bar.instrument_id,
                    previous_state=StrategyState.CANDIDATE,
                    new_state=StrategyState.BLOCKED,
                    event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
                    reason_code=final_blocker.code.value if final_blocker else "blocked",
                    payload={"blocker_count": len(blockers)},
                )
                self._record_strategy_transition(
                    instrument_id=bar.instrument_id,
                    previous_state=StrategyState.BLOCKED,
                    new_state=StrategyState.WAIT,
                    event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
                    reason_code="candidate_terminal_blocked",
                    payload={"candidate_id": str(candidate.candidate_id)},
                )
                self._strategy_states[bar.instrument_id] = StrategyState.WAIT
                continue

            await self._create_and_post_order(
                candidate=candidate_with_id,
                snapshot=snapshot,
                instrument_id=bar.instrument_id,
            )

    async def _create_and_post_order(
        self,
        *,
        candidate: SignalCandidateDecision,
        snapshot: SessionSnapshot,
        instrument_id: str,
    ) -> None:
        execution = self._require_execution_engine()
        self._record_strategy_transition(
            instrument_id=instrument_id,
            previous_state=StrategyState.CANDIDATE,
            new_state=StrategyState.PLACING_ORDER,
            event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
            reason_code="risk_allowed",
            payload={"candidate_id": str(candidate.candidate_id)},
        )
        intent = execution.create_order_intent(
            OrderIntentRequest(
                candidate=candidate,
                session_snapshot=snapshot,
                account_id=self.config.account_id,
                run_id=self._current_run_id(),
            )
        )
        self.stats.order_intents_created += 1
        lifecycle = await execution.post_order(intent)
        if lifecycle.broker_status in {"rejected", "cancelled"}:
            self.metrics.inc_rejected_order(status=lifecycle.broker_status)
        if lifecycle.broker_status in {"posted", "pseudo_posted"}:
            self.stats.open_orders += 0 if self.launch_policy.uses_pseudo_orders else 1
        self._record_strategy_transition(
            instrument_id=instrument_id,
            previous_state=StrategyState.PLACING_ORDER,
            new_state=StrategyState.WORKING_ORDER,
            event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
            reason_code=str(lifecycle.broker_status),
            payload={
                "order_intent_id": str(lifecycle.order_intent_id),
                "request_order_id": str(lifecycle.request_order_id),
                "exchange_order_id": lifecycle.exchange_order_id,
                "launch_mode": self.launch_policy.mode.value,
            },
        )
        if self.launch_policy.uses_pseudo_orders:
            self._record_strategy_transition(
                instrument_id=instrument_id,
                previous_state=StrategyState.WORKING_ORDER,
                new_state=StrategyState.WAIT,
                event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
                reason_code=self.launch_policy.real_order_block_reason_code,
                payload={"order_intent_id": str(lifecycle.order_intent_id)},
            )
            self._strategy_states[instrument_id] = StrategyState.WAIT
            return

        self._strategy_states[instrument_id] = StrategyState.WORKING_ORDER
        reconciliation = self._require_reconciliation_service()
        result = await reconciliation.reconcile_open_orders(account_id=self.config.account_id)
        self.stats.open_orders = result.observed_order_count

    def _record_strategy_transition(
        self,
        *,
        instrument_id: str,
        previous_state: StrategyState,
        new_state: StrategyState,
        event_type: str,
        reason_code: str | None,
        payload: Mapping[str, object],
    ) -> None:
        if previous_state == new_state:
            return
        event_store = self._require_strategy_event_store()
        snapshot = self._current_snapshot
        if snapshot is None:
            return
        event_store.record_state_transition(
            snapshot=snapshot,
            strategy_id=self.strategy_config.strategy_id,
            strategy_version=self.strategy_config.strategy_version,
            previous_state=previous_state,
            new_state=new_state,
            event_type=event_type,
            reason_code=reason_code,
            instrument_id=instrument_id,
            payload=dict(payload),
        )

    def _current_run_id(self) -> UUID | None:
        manager = self.hourly_micro_session_manager
        if manager is None or manager.current_state is None:
            return None
        return manager.current_state.run_id

    def _instrument_for(self, instrument_id: str) -> InstrumentRef:
        for instrument in self.config.instruments:
            if instrument.instrument_id == instrument_id:
                return instrument
        return InstrumentRef(instrument_id=instrument_id)

    def _fallback_snapshot(self) -> SessionSnapshot:
        now = datetime.now(tz=MSK)
        return SessionSnapshot(
            observed_at=now,
            calendar_date=now.date(),
            trading_date=now.date(),
            session_type=SessionType.WEEKEND,
            session_phase=SessionPhase.CLOSED,
            broker_phase=SessionPhase.CLOSED,
            broker_trading_status="closed",
            broker_api_trade_available=False,
            schedule_phase=None,
            schedule_window_start_at=None,
            schedule_window_end_at=None,
            micro_session_id="unassigned",
            is_trading_allowed=False,
            deny_reason_code="session_forbidden",
            status_mismatch=False,
        )

    async def _refresh_positions_after_gap(self, account_id: str) -> JsonPayload:
        position_service = self.position_service
        if position_service is None:
            return {"account_id": account_id, "status": "position_service_unavailable"}
        result = await position_service.refresh_positions(
            account_id,
            reason="stream_gap_recovery",
            now=datetime.now(tz=UTC),
        )
        self.stats.active_positions = _active_position_lots(result.portfolio)
        log_event(
            logger=LOGGER,
            event_type=DomainEventType.POSITION_RECONCILIATION_COMPLETED.value,
            component="runtime.positions",
            account_id_present=True,
            status="refreshed",
            position_count=len(result.positions),
            open_position_lots=result.portfolio.open_position_lots,
            long_position_lots=result.portfolio.long_position_lots,
            short_position_lots=result.portfolio.short_position_lots,
        )
        return {
            "account_id": account_id,
            "status": "refreshed",
            "position_count": len(result.positions),
            "open_position_lots": result.portfolio.open_position_lots,
            "long_position_lots": result.portfolio.long_position_lots,
            "short_position_lots": result.portfolio.short_position_lots,
        }

    def _mark_stream_recovery_degraded(self, exc: Exception) -> None:
        self.robot_control_state = "degraded"
        self._strategy_states = {
            instrument.instrument_id: StrategyState.DEGRADED
            for instrument in self.config.instruments
        }
        log_event(
            logger=LOGGER,
            level="ERROR",
            event_type=DomainEventType.STREAM_GAP_RECOVERY_FAILED.value,
            component="runtime",
            runtime_id=str(self.runtime_id),
            error_code=type(exc).__name__,
            error_message=str(exc),
        )

    def _write_recovery_audit_event(
        self,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        session = self._session
        if session is None:
            return
        snapshot = self._current_snapshot or self._fallback_snapshot()
        micro_session_id = snapshot.micro_session_id or "unassigned"
        now = datetime.now(tz=UTC)
        session.add(
            AuditEvent(
                calendar_date=snapshot.calendar_date,
                trading_date=snapshot.trading_date,
                session_type=snapshot.session_type.value,
                session_phase=snapshot.session_phase.value,
                micro_session_id=micro_session_id,
                broker_trading_status=snapshot.broker_trading_status,
                ts_utc=now,
                exchange_ts=now,
                received_ts=now,
                service=ServiceName.TRADE_CORE.value,
                actor="system",
                action=event_type,
                entity_type="stream_gap_recovery",
                entity_id=micro_session_id,
                severity=(
                    "error"
                    if event_type == DomainEventType.STREAM_GAP_RECOVERY_FAILED.value
                    else "info"
                ),
                correlation_id=str(self.runtime_id),
                audit_payload=dict(payload),
            )
        )

    def _write_audit_event(self, *, action: str) -> None:
        session = self._session
        if session is None:
            return
        snapshot = self._current_snapshot or self._fallback_snapshot()
        micro_session_id = snapshot.micro_session_id or "unassigned"
        now = datetime.now(tz=UTC)
        session.add(
            AuditEvent(
                calendar_date=snapshot.calendar_date,
                trading_date=snapshot.trading_date,
                session_type=snapshot.session_type.value,
                session_phase=snapshot.session_phase.value,
                micro_session_id=micro_session_id,
                broker_trading_status=snapshot.broker_trading_status,
                ts_utc=now,
                exchange_ts=now,
                received_ts=now,
                service=ServiceName.TRADE_CORE.value,
                actor="system",
                action=action,
                entity_type="trade_core_runtime",
                entity_id=str(self.runtime_id),
                severity="info",
                correlation_id=str(self.runtime_id),
                audit_payload={
                    "launch_policy": self.launch_policy.as_payload(),
                    "stats": {
                        "cycles": self.stats.cycles,
                        "processed_closed_bars": self.stats.processed_closed_bars,
                        "candidates_created": self.stats.candidates_created,
                        "order_intents_created": self.stats.order_intents_created,
                    },
                },
            )
        )

    def _write_robot_command_audit(
        self,
        *,
        command: RobotCommand,
        payload: Mapping[str, object],
    ) -> None:
        session = self._session
        if session is None:
            return
        snapshot = self._current_snapshot or self._fallback_snapshot()
        micro_session_id = snapshot.micro_session_id or "unassigned"
        now = datetime.now(tz=UTC)
        session.add(
            AuditEvent(
                calendar_date=snapshot.calendar_date,
                trading_date=snapshot.trading_date,
                session_type=snapshot.session_type.value,
                session_phase=snapshot.session_phase.value,
                micro_session_id=micro_session_id,
                broker_trading_status=snapshot.broker_trading_status,
                ts_utc=now,
                exchange_ts=now,
                received_ts=now,
                service=ServiceName.TRADE_CORE.value,
                actor=command.requested_by,
                action=f"robot_command_{command.command_type}_{command.status}",
                entity_type="robot_command",
                entity_id=str(command.command_id),
                severity="info" if command.status == "applied" else "warning",
                correlation_id=str(command.command_id),
                audit_payload={
                    "command_type": command.command_type,
                    "requested_role": command.requested_role,
                    "requested_at": command.requested_at.isoformat(),
                    "reason_code": command.reason_code,
                    "payload": dict(command.payload),
                    "result": dict(payload),
                },
            )
        )

    def _require_session(self) -> Session:
        if self._session is None:
            msg = "TradeCoreRuntime.start() has not opened a database session yet"
            raise RuntimeError(msg)
        return self._session

    def _require_micro_session_manager(self) -> HourlyMicroSessionManager:
        if self.hourly_micro_session_manager is None:
            msg = "HourlyMicroSessionManager is not initialized"
            raise RuntimeError(msg)
        return self.hourly_micro_session_manager

    def _require_strategy_event_store(self) -> SqlAlchemyStrategyEventStore:
        if self.strategy_event_store is None:
            msg = "SqlAlchemyStrategyEventStore is not initialized"
            raise RuntimeError(msg)
        return self.strategy_event_store

    def _require_execution_engine(self) -> DefaultExecutionEngine:
        if self.execution_engine is None:
            msg = "DefaultExecutionEngine is not initialized"
            raise RuntimeError(msg)
        return self.execution_engine

    def _require_reconciliation_service(self) -> DefaultReconciliationService:
        if self.reconciliation_service is None:
            msg = "DefaultReconciliationService is not initialized"
            raise RuntimeError(msg)
        return self.reconciliation_service


def _required_database_url(config: TradeCoreRuntimeConfig) -> str:
    if config.database_url is None:
        msg = "TradeCoreRuntimeConfig.database_url is required"
        raise RuntimeError(msg)
    return config.database_url


def _build_report_job_dispatcher() -> ReportJobDispatcher:
    broker = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    backend = os.getenv("CELERY_RESULT_BACKEND", broker)
    queue = os.getenv("CELERY_REPORTS_QUEUE", REPORTS_QUEUE)
    app = Celery("trade_core_report_dispatcher", broker=broker, backend=backend)
    return ReportJobDispatcher(app, queue=queue)


def _instruments_from_env(value: str | None) -> tuple[InstrumentRef, ...]:
    if not value:
        return DEFAULT_INSTRUMENTS
    instruments: list[InstrumentRef] = []
    for raw in value.split(","):
        ticker = raw.strip()
        if not ticker:
            continue
        instrument_id = ticker if ":" in ticker else f"MOEX:{ticker.upper()}"
        instruments.append(
            InstrumentRef(
                instrument_id=instrument_id,
                ticker=instrument_id.rsplit(":", 1)[-1],
                class_code="TQBR",
            )
        )
    return tuple(instruments) or DEFAULT_INSTRUMENTS


def default_trading_schedule(moment: datetime) -> TradingSchedule:
    """Return deterministic MOEX-like continuous windows for safe local modes."""

    local = _ensure_msk(moment)
    trading_date = local.date()
    if local.weekday() >= 5:
        return TradingSchedule(windows=())
    return TradingSchedule(
        windows=(
            _window(trading_date, SessionKind.MORNING, time(7, 0), time(10, 0)),
            _window(trading_date, SessionKind.MAIN, time(10, 0), time(18, 59)),
            _window(trading_date, SessionKind.EVENING, time(19, 0), time(23, 50)),
        )
    )


class SessionKind:
    MORNING = "weekday_morning"
    MAIN = "weekday_main"
    EVENING = "weekday_evening"


def _window(
    trading_date: date,
    session_type: str,
    start_time: time,
    end_time: time,
) -> ScheduleWindow:
    session_type_enum = SessionType(session_type)
    phase_enum = SessionPhase.CONTINUOUS_TRADING
    return ScheduleWindow(
        session_type=session_type_enum,
        session_phase=phase_enum,
        start_at=datetime.combine(trading_date, start_time, tzinfo=MSK),
        end_at=datetime.combine(trading_date, end_time, tzinfo=MSK),
        trading_date=trading_date,
        calendar_date=trading_date,
    )


def trading_schedule_from_response(
    response: BrokerUnaryResponse,
    *,
    now: datetime,
) -> TradingSchedule:
    raw_windows = response.data.get("windows")
    if not isinstance(raw_windows, list):
        return default_trading_schedule(now)
    windows: list[ScheduleWindow] = []
    for item in raw_windows:
        if not isinstance(item, Mapping):
            continue
        try:
            windows.append(
                ScheduleWindow(
                    session_type=SessionType(str(item["session_type"])),
                    session_phase=SessionPhase(
                        str(item.get("session_phase", "continuous_trading"))
                    ),
                    start_at=_ensure_msk(datetime.fromisoformat(str(item["start_at"]))),
                    end_at=_ensure_msk(datetime.fromisoformat(str(item["end_at"]))),
                    trading_date=date.fromisoformat(str(item["trading_date"])),
                    calendar_date=date.fromisoformat(
                        str(item.get("calendar_date", item["trading_date"]))
                    ),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return TradingSchedule(windows=tuple(windows)) if windows else default_trading_schedule(now)


def broker_status_from_response(
    response: BrokerUnaryResponse,
    *,
    instrument_id: str,
    now: datetime,
) -> BrokerTradingStatus:
    status = response.data.get("trading_status", response.data.get("status", "closed"))
    api_trade_available = bool(response.data.get("api_trade_available", False))
    exchange_ts_raw = response.data.get("exchange_ts")
    exchange_ts = (
        _ensure_msk(datetime.fromisoformat(str(exchange_ts_raw)))
        if isinstance(exchange_ts_raw, str)
        else now
    )
    return BrokerTradingStatus(
        status=str(status),
        api_trade_available=api_trade_available,
        instrument_id=instrument_id,
        exchange_ts=exchange_ts,
        raw_payload=dict(response.data),
    )


def _stream_name_for_event(event: MarketDataEvent) -> str:
    if event.event_type is MarketEventType.CANDLE:
        return "candles"
    if event.event_type is MarketEventType.ORDER_BOOK:
        return "order_book"
    if event.event_type is MarketEventType.LAST_PRICE:
        return "last_prices"
    if event.event_type is MarketEventType.TRADING_STATUS:
        return "trading_status"
    if event.event_type is MarketEventType.MARKET_TRADE:
        return "market_trades"
    if event.event_type is MarketEventType.USER_ORDER_STATE:
        return "OrderStateStream"
    return event.event_type.value


def _active_position_lots(portfolio: PortfolioSnapshot) -> int:
    return portfolio.long_position_lots + portfolio.short_position_lots


def _window_payload(window: ScheduleWindow) -> JsonPayload:
    return {
        "session_type": window.session_type.value,
        "session_phase": window.session_phase.value,
        "start_at": window.start_at.isoformat(),
        "end_at": window.end_at.isoformat(),
        "trading_date": window.trading_date.isoformat(),
        "calendar_date": window.calendar_date.isoformat() if window.calendar_date else None,
    }


def _ensure_msk(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MSK)
    return value.astimezone(MSK)

"""Long-lived trade-core runtime orchestration."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
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
    DividendsRequest,
    InstrumentRef,
    InstrumentResolveRequest,
    LastPricesRequest,
    LastTradesRequest,
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
from trade_core.corporate_actions import (
    CorporateActionService,
    DividendSyncConfig,
    DividendSyncService,
    SpecialDayFlags,
)
from trade_core.corporate_actions.service import special_day_classification_exists
from trade_core.infra.tbank import (
    TBankBrokerConfig,
    TBankBrokerGateway,
    load_tbank_tokens_for_launch,
)
from trade_core.infra.tbank.sdk_clients import load_tbank_sdk
from trade_core.instruments import (
    InstrumentResolverService,
    assert_resolved_for_broker_call,
    is_broker_resolved_instrument,
)
from trade_core.market_data import (
    Bar,
    BarEngine,
    Candle,
    LiveMarketDataCollector,
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
from trade_core.market_data.subscriptions import order_book_from_mapping
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
    CancelReasonCode,
    ConfigDrivenStrategyConfig,
    ConfigDrivenStrategyEngine,
    DefaultExecutionEngine,
    DefaultReconciliationService,
    DefaultRiskEngine,
    LoadedStrategyConfig,
    OrderIntentRequest,
    PortfolioSnapshot,
    RiskAssessmentInput,
    RiskLimits,
    SignalCandidateDecision,
    SqlAlchemyStrategyEventStore,
    StrategyConfigLoader,
    StrategyEvaluationContext,
    StrategyState,
)
from trading_common import LaunchModePolicy, RuntimeMode, ServiceName, TradingMetrics
from trading_common.db.base import Base
from trading_common.db.config import (
    build_database_url_from_mapping,
    database_backend_from_url,
    redact_database_url,
)
from trading_common.db.models import AuditEvent, OrderIntent, RobotCommand
from trading_common.db.repositories import (
    BlockerEventRepository,
    CandidateStageResultRepository,
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
LOCAL_SQLITE_ENV = "TRADING_RUNTIME_LOCAL_SQLITE"
DEFAULT_EXCHANGE = "MOEX"
DEFAULT_INSTRUMENTS: tuple[InstrumentRef, ...] = (
    InstrumentRef(
        instrument_id="MOEX:SBER",
        class_code="TQBR",
        ticker="SBER",
    ),
    InstrumentRef(
        instrument_id="MOEX:GAZP",
        class_code="TQBR",
        ticker="GAZP",
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
    strategy_id: str = "baseline"
    session_template: str = SessionType.WEEKDAY_MAIN.value
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
    data_only_stream_names: tuple[str, ...] = (
        "order_book",
        "last_prices",
        "trading_status",
    )
    data_only_order_book_poll_interval_seconds: float = 15.0
    dividend_sync_enabled: bool = False
    dividend_sync_lookback_days: int = 730
    dividend_sync_lookahead_days: int = 365
    dividend_sync_interval_hours: int = 24
    dividend_sync_fail_open: bool = False
    data_only_shadow_enabled: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TradeCoreRuntimeConfig:
        env = environ if environ is not None else os.environ
        runtime_mode = env.get("TRADING_RUNTIME_MODE", RuntimeMode.HISTORICAL_REPLAY.value)
        default_dividend_sync_enabled = runtime_mode in {
            RuntimeMode.SANDBOX.value,
            RuntimeMode.SHADOW.value,
            RuntimeMode.PRODUCTION.value,
        }
        database_url: str
        auto_create_sqlite_schema = False
        if _local_sqlite_requested(env):
            DEFAULT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite+pysqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
            auto_create_sqlite_schema = True
        else:
            database_url = build_database_url_from_mapping(env)

        return cls(
            account_id=env.get("TRADING_ACCOUNT_ID", DEFAULT_ACCOUNT_ID),
            exchange=env.get("TRADING_EXCHANGE", DEFAULT_EXCHANGE),
            instruments=_instruments_from_env(env.get("TRADING_INSTRUMENTS")),
            tick_interval_seconds=float(env.get("TRADE_CORE_TICK_INTERVAL_SECONDS", "1.0")),
            database_url=database_url,
            auto_create_sqlite_schema=auto_create_sqlite_schema,
            strategy_id=env.get("TRADING_STRATEGY_ID", "baseline"),
            session_template=env.get(
                "TRADING_STRATEGY_SESSION_TEMPLATE",
                SessionType.WEEKDAY_MAIN.value,
            ),
            micro_session_freeze_seconds=int(
                env.get("TRADE_CORE_MICRO_SESSION_FREEZE_SECONDS", "90")
            ),
            position_snapshot_freshness_seconds=int(
                env.get("TRADE_CORE_POSITION_SNAPSHOT_FRESHNESS_SECONDS", "900")
            ),
            data_only_stream_names=_stream_names_from_env(
                env.get("TRADING_DATA_ONLY_STREAM_NAMES"),
                default=("order_book", "last_prices", "trading_status"),
            ),
            data_only_order_book_poll_interval_seconds=float(
                env.get("TRADING_DATA_ONLY_ORDER_BOOK_POLL_INTERVAL_SECONDS", "15")
            ),
            dividend_sync_enabled=_bool_env(
                env.get("TRADING_DIVIDEND_SYNC_ENABLED"),
                default=default_dividend_sync_enabled,
            ),
            dividend_sync_lookback_days=int(
                env.get("TRADING_DIVIDEND_SYNC_LOOKBACK_DAYS", "730")
            ),
            dividend_sync_lookahead_days=int(
                env.get("TRADING_DIVIDEND_SYNC_LOOKAHEAD_DAYS", "365")
            ),
            dividend_sync_interval_hours=int(
                env.get("TRADING_DIVIDEND_SYNC_INTERVAL_HOURS", "24")
            ),
            dividend_sync_fail_open=_bool_env(
                env.get("TRADING_DIVIDEND_SYNC_FAIL_OPEN"),
                default=False,
            ),
            data_only_shadow_enabled=_bool_env(
                env.get("TRADING_DATA_ONLY_SHADOW"),
                default=False,
            ),
        )

    @property
    def database_backend(self) -> str:
        return database_backend_from_url(_required_database_url(self))

    @property
    def database_url_redacted(self) -> str:
        return redact_database_url(_required_database_url(self))


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
    data_only_shadow_enabled: bool = False
    active_positions: int = 0
    collector_state: str = "stopped"
    collector_started_at: datetime | None = None
    collector_stopped_at: datetime | None = None
    last_command_id: str | None = None
    last_command_status: str | None = None
    last_command_reason_code: str | None = None
    data_only_order_book_polls: int = 0
    data_only_order_book_poll_errors: int = 0
    last_data_only_order_book_poll_at: datetime | None = None


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

    async def get_dividends(
        self,
        request: DividendsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="GetDividends",
            data={
                "instrument_id": request.instrument.instrument_id,
                "dividends": [],
                "source": "safe_noop_gateway",
            },
            headers={},
        )

    async def resolve_instruments(
        self,
        request: InstrumentResolveRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        instruments = [
            {
                "instrument_id": f"safe-noop-{ticker.lower()}-uid",
                "instrument_uid": f"safe-noop-{ticker.lower()}-uid",
                "ticker": ticker.upper(),
                "class_code": request.class_code,
                "figi": None,
                "name": ticker.upper(),
                "lot_size": 1,
                "min_price_increment": "0.01",
                "currency": "RUB",
                "api_trade_available": True,
                "short_available": True,
                "supports_weekend": False,
                "source": "safe_noop_resolver",
            }
            for ticker in request.tickers
        ]
        return BrokerUnaryResponse(
            method_name="ResolveInstruments",
            data={"instruments": instruments},
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

    async def get_last_trades(
        self,
        request: LastTradesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(method_name="GetLastTrades", data={"trades": []}, headers={})

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
        self._assert_broker_sdk_available_when_required(
            broker_gateway_injected=broker_gateway is not None,
        )
        self.identity = create_identity(self.launch_policy.mode)
        self.database = database or DatabaseService(_required_database_url(self.config))
        self.metrics = metrics or TradingMetrics(self.identity)
        self.report_job_dispatcher = report_job_dispatcher or _build_report_job_dispatcher()
        if self.config.auto_create_sqlite_schema:
            Base.metadata.create_all(self.database.engine)
        self.metrics.set_data_only_shadow_enabled(self.config.data_only_shadow_enabled)

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
        self._strategy_config_injected = strategy_config is not None
        default_strategy_config = replace(
            ConfigDrivenStrategyConfig.conservative_default(),
            strategy_id=self.config.strategy_id,
            session_template=self.config.session_template,
        )
        self.strategy_config = strategy_config or default_strategy_config
        self.strategy_engine = ConfigDrivenStrategyEngine(self.strategy_config)
        self.risk_engine = DefaultRiskEngine()
        self.risk_limits = replace(
            risk_limits or RiskLimits.from_strategy_config(self.strategy_config),
            dividend_sync_fail_open=self.config.dividend_sync_fail_open,
        )
        self.runtime_id = uuid4()
        self.stats = TradeCoreRuntimeStats()
        self.stats.data_only_shadow_enabled = self.config.data_only_shadow_enabled
        self.robot_control_state = "stopped" if self.config.data_only_shadow_enabled else "running"
        if self.config.data_only_shadow_enabled:
            self.stats.collector_state = "stopped"

        self._session: Session | None = None
        self._session_state_store: SqlAlchemySessionStateStore | None = None
        self.hourly_micro_session_manager: HourlyMicroSessionManager | None = None
        self.market_data_store: SqlAlchemyMarketDataStore | None = None
        self.market_data_pipeline: MarketDataPipeline | None = None
        self.live_market_data_collector: LiveMarketDataCollector | None = None
        self.stream_gap_recovery_service: StreamGapRecoveryService | None = None
        self.position_service: PositionService | None = None
        self.execution_engine: DefaultExecutionEngine | None = None
        self.reconciliation_service: DefaultReconciliationService | None = None
        self.strategy_event_store: SqlAlchemyStrategyEventStore | None = None
        self.strategy_config_loader: StrategyConfigLoader | None = None

        self._stream_tasks: tuple[asyncio.Task[None], ...] = ()
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._current_schedule: TradingSchedule | None = None
        self._current_snapshot: SessionSnapshot | None = None
        self._data_only_preflight_payload: JsonPayload | None = None
        self._data_only_session_context: SessionEventContext | None = None
        self._last_data_only_order_book_poll_at: datetime | None = None
        self._latest_market_states: dict[str, MarketState] = {}
        self._latest_closed_bars: dict[str, dict[Timeframe, Bar]] = {}
        self._strategy_states: dict[str, StrategyState] = {}
        self._loaded_strategy_config_identity: tuple[str, int, str | None] | None = None
        self._corporate_action_calendar_warnings: set[tuple[date, str]] = set()
        self._dividend_calendar_available = True
        self._last_dividend_sync_at: datetime | None = None

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
        await self._resolve_runtime_instruments()
        await self._sync_dividend_calendar_if_due(force=True)
        session = self._require_session()
        self.strategy_config_loader = StrategyConfigLoader(session)
        self._reload_strategy_config_if_changed(force=True)
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
        if self.config.data_only_shadow_enabled:
            self.live_market_data_collector = LiveMarketDataCollector(
                event_bus=self.market_event_bus,
                session_context_provider=self._session_context_for,
                store=self.market_data_store,
                metrics=self.metrics,
            )
            self.live_market_data_collector.register()
            self._write_audit_event(
                action="data_only_shadow_started",
                payload={
                    "runtime_id": str(self.runtime_id),
                    "strategy_trading_disabled": True,
                    "real_orders_disabled": True,
                },
            )
            self._write_audit_event(
                action="data_only_shadow_strategy_disabled",
                payload={
                    "reason_code": "data_only_shadow_strategy_disabled",
                    "strategy_id": self.config.strategy_id,
                },
            )
        else:
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
        if self.config.data_only_shadow_enabled:
            self.stats.collector_state = "stopped"
            self.stats.stream_tasks_started = 0
            self.metrics.set_market_stream_alive(False, stream_type="market_data")
        else:
            await self._start_market_streams(account_id=self.config.account_id)
        self.stats.started = True
        self.flush_domain_events()
        log_event(
            logger=LOGGER,
            event_type="trade_core_runtime_started",
            component="runtime",
            runtime_id=str(self.runtime_id),
            launch_mode=self.launch_policy.mode.value,
            database_backend=self.config.database_backend,
            database_url_redacted=self.config.database_url_redacted,
            stream_tasks=self.stats.stream_tasks_started,
            data_only_shadow_enabled=self.config.data_only_shadow_enabled,
        )

    async def _start_market_streams(
        self,
        *,
        account_id: str | None,
        market_stream_names: tuple[str, ...] | None = None,
    ) -> None:
        """Start broker market streams once; order stream is optional in data-only."""

        if self._stream_tasks:
            return
        stream_names = market_stream_names or self.config.stream_names
        self._stream_tasks = await self.market_data_subscription_service.start(
            MarketDataSubscriptionConfig(
                market_stream_names=stream_names,
                account_id=account_id,
            )
        )
        self.stats.stream_tasks_started = len(self._stream_tasks)
        self.metrics.set_market_stream_alive(True, stream_type="market_data")

    async def _stop_market_streams(self) -> None:
        """Cancel currently running market streams and wait for clean shutdown."""

        tasks = self._stream_tasks
        if not tasks:
            self.metrics.set_market_stream_alive(False, stream_type="market_data")
            self.stats.stream_tasks_started = 0
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._stream_tasks = ()
        self.stats.stream_tasks_started = 0
        self.metrics.set_market_stream_alive(False, stream_type="market_data")

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

        await self._stop_market_streams()
        self.metrics.set_data_only_shadow_enabled(False)
        if self.config.data_only_shadow_enabled:
            self._write_audit_event(
                action="data_only_shadow_stopped",
                payload={"runtime_id": str(self.runtime_id)},
            )
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
        await self.process_robot_commands_async()
        if not self.config.data_only_shadow_enabled:
            self._reload_strategy_config_if_changed()
        await self._sync_dividend_calendar_if_due()
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
        await self._poll_data_only_order_books_if_due(now=observed_at)
        self.flush_domain_events()
        self.dispatch_report_jobs()
        return result.snapshot

    async def refresh_trading_schedule(self, *, now: datetime) -> TradingSchedule:
        """Fetch broker schedule and keep a parsed fallback for local replay."""

        try:
            response = await self.broker_gateway.trading_schedules(
                TradingSchedulesRequest(
                    exchange=self.config.exchange,
                    from_=now,
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

    async def _poll_data_only_order_books_if_due(self, *, now: datetime) -> None:
        if not self.config.data_only_shadow_enabled:
            return
        if self.stats.collector_state != "collecting":
            return
        interval = max(1.0, self.config.data_only_order_book_poll_interval_seconds)
        now_utc = now.astimezone(UTC)
        last_poll = self._last_data_only_order_book_poll_at
        if last_poll is not None and (now_utc - last_poll).total_seconds() < interval:
            return
        self._last_data_only_order_book_poll_at = now_utc
        self.stats.last_data_only_order_book_poll_at = now_utc
        calibration_allowed = self._data_only_polling_calibration_allowed()
        successful = 0
        failed = 0
        for instrument in self.config.instruments:
            try:
                response = await self.broker_gateway.get_order_book(
                    OrderBookRequest(instrument=instrument, depth=10)
                )
                payload = {
                    **dict(response.data),
                    "source": "tbank_get_order_book_polling_fallback",
                    "data_only_polling_fallback": True,
                    "include_in_calibration": calibration_allowed,
                    "calibration_allowed": calibration_allowed,
                    "venue_type": (
                        "official_exchange" if calibration_allowed else "display_only"
                    ),
                }
                received_at = datetime.now(tz=UTC)
                order_book = order_book_from_mapping(payload, received_at=received_at)
                await self.process_order_book(order_book)
                successful += 1
            except Exception as exc:
                failed += 1
                log_event(
                    logger=LOGGER,
                    level="WARNING",
                    event_type="data_only_order_book_poll_failed",
                    component="runtime.data_only_polling",
                    instrument_id=instrument.instrument_id,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
        self.stats.data_only_order_book_polls += successful
        self.stats.data_only_order_book_poll_errors += failed
        self._write_audit_event(
            action="data_only_order_book_poll_completed",
            severity="info" if successful else "warning",
            payload={
                "successful_instruments": successful,
                "failed_instruments": failed,
                "instrument_count": len(self.config.instruments),
                "readonly_calls_only": True,
                "real_orders_disabled": True,
                "strategy_trading_disabled": True,
                "include_in_calibration": calibration_allowed,
            },
        )

    def _data_only_polling_calibration_allowed(self) -> bool:
        preflight = self._data_only_preflight_payload or {}
        return bool(
            preflight.get("streams_for_calibration_allowed") is True
            and preflight.get("data_only_collection_allowed") is True
            and preflight.get("market_open") is True
        )

    def _session_context_from_data_only_preflight(
        self,
        *,
        preflight: Mapping[str, object],
        observed_at: datetime,
    ) -> SessionEventContext:
        now_msk = _preflight_now_msk(preflight) or _ensure_msk(observed_at)
        session_type = _enum_payload(
            preflight.get("session_type"),
            SessionType,
            default=SessionType.WEEKEND if now_msk.weekday() >= 5 else SessionType.WEEKDAY_MAIN,
        )
        session_phase = _enum_payload(
            preflight.get("session_phase"),
            SessionPhase,
            default=SessionPhase.CONTINUOUS_TRADING
            if preflight.get("market_open") is True
            else SessionPhase.CLOSED,
        )
        trading_date = now_msk.date()
        micro_session_id = (
            f"{trading_date.isoformat()}:{session_type.value}:"
            f"{now_msk.replace(minute=0, second=0, microsecond=0):%Y%m%dT%H%M}"
        )
        return SessionEventContext(
            calendar_date=now_msk.date(),
            trading_date=trading_date,
            session_type=session_type,
            session_phase=session_phase,
            micro_session_id=micro_session_id,
            broker_trading_status=str(
                preflight.get("broker_trading_status")
                or preflight.get("reason_code")
                or "unknown"
            ),
        )

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
        """Synchronous compatibility wrapper for tests and local scripts."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.process_robot_commands_async())
        msg = "process_robot_commands() cannot run inside an active event loop"
        raise RuntimeError(msg)

    async def process_robot_commands_async(self) -> int:
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
            self.stats.last_command_id = str(command.command_id)
            repository.mark_accepted(command, accepted_at=now)
            try:
                reason_code, result_payload = await self._apply_robot_command(command)
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
                self.stats.last_command_status = "applied"
                self.stats.last_command_reason_code = reason_code
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
                self.stats.last_command_status = "failed"
                self.stats.last_command_reason_code = "runtime_command_failed"
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

    def _reload_strategy_config_if_changed(self, *, force: bool = False) -> None:
        if self._strategy_config_injected:
            return
        loader = self.strategy_config_loader
        if loader is None:
            return
        template = self._strategy_config_session_template()
        try:
            loaded = loader.load_active(
                strategy_id=self.config.strategy_id,
                session_template=template,
                fallback=self.strategy_config,
            )
        except Exception as exc:
            log_event(
                logger=LOGGER,
                level="ERROR",
                event_type="strategy_config_reload_failed",
                component="runtime.strategy_config",
                strategy_id=self.config.strategy_id,
                session_template=template,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            self._write_audit_event(
                action="strategy_config_reload_failed",
                severity="error",
                payload={
                    "strategy_id": self.config.strategy_id,
                    "session_template": template,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return

        identity = _loaded_config_identity(loaded)
        if not force and identity == self._loaded_strategy_config_identity:
            return
        previous_identity = self._loaded_strategy_config_identity
        self.strategy_config = loaded.config
        self.strategy_engine = ConfigDrivenStrategyEngine(self.strategy_config)
        self.risk_limits = replace(
            loaded.risk_limits,
            dividend_sync_fail_open=self.config.dividend_sync_fail_open,
        )
        self._loaded_strategy_config_identity = identity
        action = (
            "strategy_config_loaded"
            if previous_identity is None
            else "strategy_config_reloaded"
        )
        payload = {
            "strategy_id": self.strategy_config.strategy_id,
            "strategy_version": self.strategy_config.strategy_version,
            "session_template": template,
            "source": loaded.source,
            "strategy_config_id": loaded.strategy_config_id,
            "allow_long": self.strategy_config.allow_long,
            "allow_short": self.strategy_config.allow_short,
            "max_position_lots": self.risk_limits.max_position_lots,
            "max_daily_loss_rub": str(self.risk_limits.max_daily_loss_rub),
            "assumed_commission_bps_per_side": str(
                self.risk_limits.assumed_commission_bps_per_side
            ),
            "assumed_slippage_bps": str(self.risk_limits.assumed_slippage_bps),
        }
        log_event(
            logger=LOGGER,
            event_type=action,
            component="runtime.strategy_config",
            details=payload,
        )
        self._write_audit_event(action=action, payload=payload)

    def _strategy_config_session_template(self) -> str:
        snapshot = self._current_snapshot
        if snapshot is not None:
            return snapshot.session_type.value
        return self.config.session_template

    async def _apply_robot_command(self, command: RobotCommand) -> tuple[str, JsonPayload]:
        if self.config.data_only_shadow_enabled:
            return await self._apply_data_only_robot_command(command)

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
            result = await self._apply_emergency_stop(command)
            self._strategy_states = {
                instrument_id: StrategyState.STOPPED for instrument_id in self._strategy_states
            }
            return result
        msg = f"unsupported robot command: {command_type}"
        raise ValueError(msg)

    async def _apply_data_only_robot_command(
        self,
        command: RobotCommand,
    ) -> tuple[str, JsonPayload]:
        command_type = command.command_type
        if command_type in {"start", "resume"}:
            return await self._start_data_only_collection(command)
        if command_type in {"stop", "pause"}:
            return await self._stop_data_only_collection(
                command,
                requested_state="stopped_by_operator"
                if command_type == "stop"
                else "stopped",
                reason_code="data_only_collection_stopped"
                if command_type == "stop"
                else "data_only_collection_paused",
            )
        if command_type == "emergency_stop":
            return await self._stop_data_only_collection(
                command,
                requested_state="emergency_stopped",
                reason_code="data_only_collection_emergency_stopped",
            )
        msg = f"unsupported robot command: {command_type}"
        raise ValueError(msg)

    async def _start_data_only_collection(
        self,
        command: RobotCommand,
    ) -> tuple[str, JsonPayload]:
        payload = dict(command.payload or {})
        preflight = payload.get("preflight_result")
        market_open = isinstance(preflight, Mapping) and preflight.get("market_open") is True
        collection_allowed = (
            isinstance(preflight, Mapping)
            and preflight.get("data_only_collection_allowed") is True
        )
        reason_code = (
            str(preflight.get("reason_code") or "market_closed_expected")
            if isinstance(preflight, Mapping)
            else "session_preflight_required"
        )
        if not market_open or not collection_allowed:
            self.robot_control_state = "preflight_blocked"
            self.stats.collector_state = "preflight_blocked"
            self.stats.last_command_reason_code = reason_code
            self._write_audit_event(
                action="data_only_shadow_collection_preflight_blocked",
                payload={
                    "command_id": str(command.command_id),
                    "reason_code": reason_code,
                    "preflight_result": preflight if isinstance(preflight, Mapping) else None,
                    "real_orders_disabled": True,
                    "strategy_trading_disabled": True,
                    "data_only_collection_allowed": collection_allowed,
                    "streams_for_calibration_allowed": (
                        preflight.get("streams_for_calibration_allowed")
                        if isinstance(preflight, Mapping)
                        else False
                    ),
                },
            )
            return (
                reason_code,
                {
                    "collector_state": self.stats.collector_state,
                    "accepted": False,
                    "stream_tasks": 0,
                    "real_orders_disabled": True,
                    "strategy_trading_disabled": True,
                },
            )

        if self._stream_tasks:
            self.robot_control_state = "collecting"
            self.stats.collector_state = "collecting"
            return (
                "data_only_collection_already_collecting",
                {
                    "collector_state": self.stats.collector_state,
                    "stream_tasks": len(self._stream_tasks),
                    "real_orders_disabled": True,
                    "strategy_trading_disabled": True,
                },
            )

        self.robot_control_state = "starting"
        self.stats.collector_state = "starting"
        now = datetime.now(tz=UTC)
        self._data_only_preflight_payload = (
            dict(preflight) if isinstance(preflight, Mapping) else {}
        )
        self._data_only_session_context = self._session_context_from_data_only_preflight(
            preflight=self._data_only_preflight_payload,
            observed_at=now.astimezone(MSK),
        )
        self._last_data_only_order_book_poll_at = None
        await self._start_market_streams(
            account_id=None,
            market_stream_names=self.config.data_only_stream_names,
        )
        self.robot_control_state = "collecting"
        self.stats.collector_state = "collecting"
        self.stats.collector_started_at = now
        self.stats.collector_stopped_at = None
        self.stats.last_data_only_order_book_poll_at = None
        self._write_audit_event(
            action="data_only_shadow_collection_started",
            payload={
                "command_id": str(command.command_id),
                "runtime_id": str(self.runtime_id),
                "stream_tasks": len(self._stream_tasks),
                "stream_names": list(self.config.data_only_stream_names),
                "instruments": [instrument.instrument_id for instrument in self.config.instruments],
                "real_orders_disabled": True,
                "strategy_trading_disabled": True,
                "polling_fallback_enabled": True,
                "order_book_poll_interval_seconds": (
                    self.config.data_only_order_book_poll_interval_seconds
                ),
                "preflight_result": preflight if isinstance(preflight, Mapping) else None,
            },
        )
        return (
            "data_only_collection_started",
            {
                "collector_state": self.stats.collector_state,
                "started_at": now.isoformat(),
                "stream_tasks": len(self._stream_tasks),
                "stream_names": list(self.config.data_only_stream_names),
                "instruments": [instrument.instrument_id for instrument in self.config.instruments],
                "real_orders_disabled": True,
                "strategy_trading_disabled": True,
                "polling_fallback_enabled": True,
                "order_book_poll_interval_seconds": (
                    self.config.data_only_order_book_poll_interval_seconds
                ),
            },
        )

    async def _stop_data_only_collection(
        self,
        command: RobotCommand,
        *,
        requested_state: str,
        reason_code: str,
    ) -> tuple[str, JsonPayload]:
        self.robot_control_state = "stopping"
        self.stats.collector_state = "stopping"
        previous_task_count = len(self._stream_tasks)
        await self._stop_market_streams()
        now = datetime.now(tz=UTC)
        self.robot_control_state = requested_state
        self.stats.collector_state = requested_state
        self.stats.collector_stopped_at = now
        self._data_only_preflight_payload = None
        self._data_only_session_context = None
        self._last_data_only_order_book_poll_at = None
        self._write_audit_event(
            action="data_only_shadow_collection_stopped",
            payload={
                "command_id": str(command.command_id),
                "runtime_id": str(self.runtime_id),
                "reason_code": reason_code,
                "previous_stream_tasks": previous_task_count,
                "real_orders_disabled": True,
                "strategy_trading_disabled": True,
            },
        )
        return (
            reason_code,
            {
                "collector_state": self.stats.collector_state,
                "stopped_at": now.isoformat(),
                "previous_stream_tasks": previous_task_count,
                "stream_tasks": 0,
                "real_orders_disabled": True,
                "strategy_trading_disabled": True,
            },
        )

    async def _apply_emergency_stop(self, command: RobotCommand) -> tuple[str, JsonPayload]:
        del command
        session = self._require_session()
        execution = self._require_execution_engine()
        reconciliation = self._require_reconciliation_service()
        order_repository = OrderRepository(session)
        working_statuses = ("submitted", "working", "partially_filled", "cancel_requested")
        intents = list(
            session.execute(
                select(OrderIntent)
                .where(OrderIntent.status.in_(working_statuses))
                .order_by(OrderIntent.created_ts, OrderIntent.order_intent_id)
            ).scalars()
        )
        cancelled = 0
        failed = 0
        cancel_results: list[JsonPayload] = []
        for intent in intents:
            broker_order = order_repository.get_broker_order_by_request_order_id(
                intent.request_order_id
            )
            exchange_order_id = (
                broker_order.exchange_order_id if broker_order is not None else None
            )
            try:
                cancel_result = await execution.cancel_order(
                    intent,
                    account_id=_account_id_from_intent(intent, fallback=self.config.account_id),
                    cancel_reason_code=CancelReasonCode.MANUAL_OPERATOR_EMERGENCY_STOP,
                    cancel_payload={
                        "source": "robot_command",
                        "command_type": "emergency_stop",
                        "runtime_id": str(self.runtime_id),
                    },
                    exchange_order_id=exchange_order_id,
                )
                await reconciliation.reconcile_order(
                    account_id=_account_id_from_intent(intent, fallback=self.config.account_id),
                    request_order_id=intent.request_order_id,
                    exchange_order_id=cancel_result.exchange_order_id or exchange_order_id,
                )
                cancelled += 1
                cancel_results.append(
                    {
                        "order_intent_id": str(intent.order_intent_id),
                        "request_order_id": str(intent.request_order_id),
                        "exchange_order_id": cancel_result.exchange_order_id,
                        "status": cancel_result.status,
                        "broker_status": cancel_result.broker_status,
                    }
                )
            except Exception as exc:
                failed += 1
                self.metrics.inc_emergency_cancel_failed(result=type(exc).__name__)
                cancel_results.append(
                    {
                        "order_intent_id": str(intent.order_intent_id),
                        "request_order_id": str(intent.request_order_id),
                        "exchange_order_id": exchange_order_id,
                        "status": "failed",
                        "error_code": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                self._write_audit_event(
                    action="runtime_emergency_cancel_failed",
                    severity="error",
                    payload=cancel_results[-1],
                )

        remaining = self._working_order_count()
        self.stats.open_orders = remaining
        self.metrics.set_open_orders(remaining)
        self.metrics.set_working_orders_after_stop(remaining)
        self.metrics.inc_emergency_stop(result="degraded" if failed else "applied")
        self.robot_control_state = "degraded" if failed else "emergency_stopped"
        reason_code = (
            "runtime_emergency_stop_degraded" if failed else "runtime_emergency_stopped"
        )
        return (
            reason_code,
            {
                "new_entries_allowed": False,
                "cancel_reason_code": CancelReasonCode.MANUAL_OPERATOR_EMERGENCY_STOP.value,
                "matched_working_orders": len(intents),
                "cancelled_open_orders": cancelled,
                "failed_cancellations": failed,
                "working_orders_after_stop": remaining,
                "cancel_results": cancel_results,
            },
        )

    def _working_order_count(self) -> int:
        session = self._session
        if session is None:
            return self.stats.open_orders
        stmt = select(OrderIntent).where(
            OrderIntent.status.in_(("submitted", "working", "partially_filled", "cancel_requested"))
        )
        return len(list(session.execute(stmt).scalars()))

    async def _snapshot_positions(self, *, reason: str, now: datetime) -> None:
        if self.config.data_only_shadow_enabled:
            self._write_audit_event(
                action="data_only_position_snapshot_skipped",
                payload={
                    "reason": reason,
                    "reason_code": "data_only_market_data_only_mode",
                    "snapshot_ts": now.astimezone(UTC).isoformat(),
                    "real_orders_disabled": True,
                    "strategy_trading_disabled": True,
                },
            )
            return
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

    def _assert_broker_sdk_available_when_required(
        self,
        *,
        broker_gateway_injected: bool,
    ) -> None:
        if self.launch_policy.mode is RuntimeMode.HISTORICAL_REPLAY:
            return
        if broker_gateway_injected:
            return
        try:
            load_tbank_sdk()
        except Exception as exc:
            msg = (
                "T-Bank SDK extra is required for sandbox/shadow/production. "
                "Install with: python -m pip install -e \".[tbank]\" --extra-index-url "
                "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
            )
            raise RuntimeError(msg) from exc

    def _install_broker_gap_recovery_hook(self) -> None:
        setter = getattr(self.broker_gateway, "set_stream_gap_recovery_hook", None)
        if callable(setter):
            setter(self._recover_stream_gap_from_gateway)

    async def _recover_stream_gap_from_gateway(
        self,
        stream_name: str,
        account_id: str | None,
    ) -> None:
        if self.config.data_only_shadow_enabled and account_id is None:
            self._write_recovery_audit_event(
                "data_only_stream_gap_recovery_skipped",
                {
                    "stream_name": stream_name,
                    "reason_code": "data_only_polling_fallback_active",
                    "polling_fallback_enabled": True,
                    "real_orders_disabled": True,
                    "strategy_trading_disabled": True,
                },
            )
            log_event(
                logger=LOGGER,
                level="WARNING",
                event_type="data_only_stream_gap_recovery_skipped",
                component="runtime.data_only_polling",
                stream_name=stream_name,
                reason_code="data_only_polling_fallback_active",
            )
            return
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

    async def _resolve_runtime_instruments(self) -> None:
        self._write_audit_event(
            action="instrument_resolution_started",
            severity="info",
            payload={
                "mode": self.launch_policy.mode.value,
                "requested_instruments": [
                    {
                        "instrument_id": instrument.instrument_id,
                        "ticker": instrument.ticker,
                        "instrument_uid_present": bool(instrument.instrument_uid),
                    }
                    for instrument in self.config.instruments
                ],
            },
        )
        resolver = InstrumentResolverService(
            broker_gateway=self.broker_gateway,
            session=self._require_session(),
            launch_policy=self.launch_policy,
            exchange=self.config.exchange,
        )
        try:
            resolved = await resolver.resolve_startup_instruments(self.config.instruments)
            for instrument in resolved:
                assert_resolved_for_broker_call(
                    instrument,
                    mode=self.launch_policy.mode,
                    operation_name="runtime_startup",
                )
        except Exception as exc:
            self._write_audit_event(
                action="instrument_resolution_failed",
                severity="error",
                payload={
                    "mode": self.launch_policy.mode.value,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            self._write_audit_event(
                action="unresolved_instrument_blocked_startup",
                severity="error",
                payload={
                    "mode": self.launch_policy.mode.value,
                    "reason_code": "instrument_not_resolved_for_broker_call",
                },
            )
            raise
        unresolved = [
            {
                "instrument_id": instrument.instrument_id,
                "ticker": instrument.ticker,
                "instrument_uid_present": bool(instrument.instrument_uid),
                "figi_present": bool(instrument.figi),
            }
            for instrument in resolved
            if not is_broker_resolved_instrument(instrument)
        ]
        if unresolved and self.launch_policy.mode is not RuntimeMode.HISTORICAL_REPLAY:
            self._write_audit_event(
                action="unresolved_instrument_blocked_startup",
                severity="error",
                payload={
                    "mode": self.launch_policy.mode.value,
                    "unresolved_instruments": unresolved,
                    "reason_code": "instrument_not_resolved_for_broker_call",
                },
            )
            msg = f"unresolved instruments block startup: {unresolved}"
            raise RuntimeError(msg)
        self.config = replace(self.config, instruments=resolved)
        setter = getattr(self.broker_gateway, "set_market_stream_instruments", None)
        if callable(setter):
            setter(resolved)
        self._write_audit_event(
            action="instrument_resolution_completed",
            severity="info",
            payload={
                "mode": self.launch_policy.mode.value,
                "instrument_count": len(resolved),
                "instruments": [
                    {
                        "instrument_id": instrument.instrument_id,
                        "ticker": instrument.ticker,
                        "instrument_uid_present": bool(instrument.instrument_uid),
                        "figi_present": bool(instrument.figi),
                    }
                    for instrument in resolved
                ],
            },
        )
        self.flush_domain_events()

    async def _sync_dividend_calendar_if_due(self, *, force: bool = False) -> None:
        if not self.config.dividend_sync_enabled:
            return
        now = datetime.now(tz=UTC)
        if not force and self._last_dividend_sync_at is not None:
            next_sync = self._last_dividend_sync_at + timedelta(
                hours=self.config.dividend_sync_interval_hours
            )
            if now < next_sync:
                return
        session = self._require_session()
        try:
            result = await DividendSyncService(
                session=session,
                broker_gateway=self.broker_gateway,
            ).run(
                DividendSyncConfig(
                    instruments=tuple(
                        instrument.ticker or instrument.instrument_id
                        for instrument in self.config.instruments
                    ),
                    lookback_days=self.config.dividend_sync_lookback_days,
                    lookahead_days=self.config.dividend_sync_lookahead_days,
                    dry_run=False,
                    force_rebuild=False,
                    classify_special_days=True,
                    exchange=self.config.exchange,
                    runtime_mode=self.launch_policy.mode.value,
                )
            )
            self._last_dividend_sync_at = now
            self._dividend_calendar_available = result.clean
            if not result.clean and not self.config.dividend_sync_fail_open:
                self.robot_control_state = "degraded"
            action = (
                "dividend_sync_completed"
                if result.clean
                else (
                    "dividend_sync_failed"
                    if result.status == "failed"
                    else "dividend_sync_completed_with_errors"
                )
            )
            severity = (
                "info"
                if result.clean
                else ("warning" if self.config.dividend_sync_fail_open else "error")
            )
            self._write_audit_event(
                action=action,
                severity=severity,
                payload={
                    **result.as_payload(),
                    "dividend_sync_status": result.status,
                    "dividend_sync_clean": result.clean,
                    "failed_instruments": result.failed_instruments,
                    "successful_instruments": result.successful_instruments,
                    "error_count": result.error_count,
                    "fail_open": self.config.dividend_sync_fail_open,
                    "mode": self.launch_policy.mode.value,
                },
            )
            if not result.clean:
                log_event(
                    logger=LOGGER,
                    level="WARNING" if self.config.dividend_sync_fail_open else "ERROR",
                    event_type=action,
                    component="runtime.dividends",
                    dividend_sync_status=result.status,
                    dividend_sync_clean=result.clean,
                    failed_instruments=result.failed_instruments,
                    error_count=result.error_count,
                    fail_open=self.config.dividend_sync_fail_open,
                    mode=self.launch_policy.mode.value,
                )
        except Exception as exc:
            self._dividend_calendar_available = False
            if not self.config.dividend_sync_fail_open:
                self.robot_control_state = "degraded"
            self._write_audit_event(
                action="dividend_calendar_unavailable",
                severity="error",
                payload={
                    "reason_code": "dividend_calendar_unavailable",
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                    "fail_open": self.config.dividend_sync_fail_open,
                    "mode": self.launch_policy.mode.value,
                },
            )
            if not self.config.dividend_sync_fail_open:
                log_event(
                    logger=LOGGER,
                    level="ERROR",
                    event_type="dividend_sync_failed",
                    component="runtime.dividends",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )

    def _session_context_for(self, instrument_id: str) -> SessionEventContext:
        del instrument_id
        if (
            self.config.data_only_shadow_enabled
            and self.stats.collector_state == "collecting"
            and self._data_only_session_context is not None
        ):
            return self._data_only_session_context
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
        if self.config.data_only_shadow_enabled:
            log_event(
                logger=LOGGER,
                event_type="data_only_shadow_strategy_disabled",
                component="runtime",
                instrument_id=event.payload.instrument_id,
                timeframe=event.payload.timeframe.value,
                reason_code="data_only_shadow_strategy_disabled",
            )
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
            special_flags = self._special_day_flags_for_runtime(
                trading_date=snapshot.trading_date,
                instrument_id=bar.instrument_id,
            )
            special_payload = special_flags.as_payload()
            candidate_decision = replace(
                candidate_decision,
                condition_payload={
                    **candidate_decision.condition_payload,
                    **special_payload,
                    "corporate_action_calendar_source": special_flags.source,
                },
            )
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
                    corporate_action_flag=special_flags.corporate_action_flag,
                    dividend_gap_day=special_flags.dividend_gap_day,
                    dividend_calendar_available=self._dividend_calendar_available,
                    future_dividend_risk_window=special_flags.future_dividend_risk_window,
                    abnormal_gap_day=special_flags.abnormal_gap_day,
                    special_day_type=special_flags.special_day_type,
                    special_day_trade_policy=special_flags.trade_policy,
                    days_to_ex_date=special_flags.days_to_ex_date,
                    days_to_record_date=special_flags.days_to_record_date,
                    corporate_action_source=special_flags.corporate_action_source,
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

    def _write_audit_event(
        self,
        *,
        action: str,
        severity: str = "info",
        payload: Mapping[str, object] | None = None,
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
                action=action,
                entity_type="trade_core_runtime",
                entity_id=str(self.runtime_id),
                severity=severity,
                correlation_id=str(self.runtime_id),
                audit_payload={
                    "launch_policy": self.launch_policy.as_payload(),
                    "database_backend": self.config.database_backend,
                    "database_url_redacted": self.config.database_url_redacted,
                    **dict(payload or {}),
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

    def _special_day_flags_for_runtime(
        self,
        *,
        trading_date: date,
        instrument_id: str,
    ) -> SpecialDayFlags:
        session = self._session
        if session is None:
            return SpecialDayFlags()
        flags = CorporateActionService(session).read_special_day_flags(
            trading_date=trading_date,
            instrument_id=instrument_id,
        )
        if flags.special_day_type is not None:
            return flags
        warning_key = (trading_date, instrument_id)
        if warning_key not in self._corporate_action_calendar_warnings and not (
            special_day_classification_exists(
                session,
                from_date=trading_date,
                to_date=trading_date,
                instruments=(instrument_id,),
            )
        ):
            self._corporate_action_calendar_warnings.add(warning_key)
            self._write_audit_event(
                action="corporate_action_calendar_unavailable",
                severity="warning",
                payload={
                    "instrument_id": instrument_id,
                    "trading_date": trading_date.isoformat(),
                    "reason_code": "corporate_action_calendar_unavailable",
                },
            )
        return flags

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


def _loaded_config_identity(loaded: LoadedStrategyConfig) -> tuple[str, int, str | None]:
    return (
        loaded.config.strategy_id,
        loaded.config.strategy_version,
        loaded.strategy_config_id,
    )


def _local_sqlite_requested(env: Mapping[str, str]) -> bool:
    return env.get(LOCAL_SQLITE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_env(value: str | None, *, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _stream_names_from_env(value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    names = tuple(item.strip() for item in value.split(",") if item.strip())
    return names or default


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
        return TradingSchedule(
            windows=(
                _window(trading_date, SessionKind.WEEKEND, time(10, 0), time(19, 0)),
            )
        )
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
    WEEKEND = "weekend"


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
            start_at = _ensure_msk(datetime.fromisoformat(str(item["start_at"])))
            calendar_date = date.fromisoformat(
                str(item.get("calendar_date", item["trading_date"]))
            )
            windows.append(
                ScheduleWindow(
                    session_type=_schedule_session_type(
                        str(item["session_type"]),
                        calendar_date=calendar_date,
                    ),
                    session_phase=SessionPhase(
                        str(item.get("session_phase", "continuous_trading"))
                    ),
                    start_at=start_at,
                    end_at=_ensure_msk(datetime.fromisoformat(str(item["end_at"]))),
                    trading_date=date.fromisoformat(str(item["trading_date"])),
                    calendar_date=calendar_date,
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return TradingSchedule(windows=tuple(windows)) if windows else default_trading_schedule(now)


def _schedule_session_type(raw: str, *, calendar_date: date) -> SessionType:
    if calendar_date.weekday() >= 5:
        return SessionType.WEEKEND
    return SessionType(raw)


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


def _account_id_from_intent(intent: OrderIntent, *, fallback: str) -> str:
    value = intent.intent_payload.get("account_id")
    return value if isinstance(value, str) and value else fallback


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


def _preflight_now_msk(preflight: Mapping[str, object]) -> datetime | None:
    raw = preflight.get("now_msk") or preflight.get("observed_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return _ensure_msk(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _enum_payload(
    value: object,
    enum_type: type[Any],
    *,
    default: Any,
) -> Any:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str) and value:
        try:
            return enum_type(value)
        except ValueError:
            return default
    return default

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    CandleRequest,
    InstrumentRef,
    InstrumentResolveRequest,
    OrdersRequest,
    OrderStateRequest,
)
from trade_core.market_data import (
    Bar,
    BarEngine,
    Candle,
    GapRecoveryCoordinator,
    GapRecoveryRequest,
    HistoricalBackfillConfig,
    HistoricalCandleBackfillService,
    MarketDataEvent,
    MarketDataPipeline,
    MarketEventBus,
    MarketEventType,
    MarketReadModelStore,
    MarketStateCalculator,
    MarketTrade,
    OrderBookSnapshot,
    PriceLevel,
    Timeframe,
)
from trade_core.market_data.calculators import FeedFreshnessCalculator
from trade_core.market_data.persistence import SqlAlchemyMarketDataStore
from trade_core.session.models import SessionEventContext
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db import Base
from trading_common.db.models import InstrumentRegistry, MarketCandle
from trading_common.enums import SessionPhase, SessionType

MSK = ZoneInfo("Europe/Moscow")


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def one_minute_candle(minute: int, *, closed: bool = True) -> Candle:
    open_ts = utc(2026, 6, 12, 7, minute)
    close_ts = open_ts + timedelta(minutes=1)
    price = Decimal("100") + Decimal(minute)
    return Candle(
        instrument_id="MOEX:SBER",
        timeframe=Timeframe.M1,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
        exchange_open_ts=open_ts.astimezone(MSK),
        exchange_close_ts=close_ts.astimezone(MSK),
        open_price=price,
        high_price=price + Decimal("1"),
        low_price=price - Decimal("1"),
        close_price=price + Decimal("0.5"),
        volume_lots=Decimal("10"),
        is_closed=closed,
    )


def recovered_candle_payload(minute: int) -> dict[str, object]:
    candle = one_minute_candle(minute)
    return {
        "instrument_id": candle.instrument_id,
        "timeframe": candle.timeframe.value,
        "open_ts_utc": candle.open_ts_utc.isoformat(),
        "close_ts_utc": candle.close_ts_utc.isoformat(),
        "exchange_open_ts": candle.exchange_open_ts.isoformat(),
        "exchange_close_ts": candle.exchange_close_ts.isoformat(),
        "open": str(candle.open_price),
        "high": str(candle.high_price),
        "low": str(candle.low_price),
        "close": str(candle.close_price),
        "volume": str(candle.volume_lots),
        "is_closed": True,
        "source": "backfill",
    }


def session_context(instrument_id: str = "MOEX:SBER") -> SessionEventContext:
    return SessionEventContext(
        calendar_date=date(2026, 6, 12),
        trading_date=date(2026, 6, 12),
        session_type=SessionType.WEEKDAY_MORNING,
        session_phase=SessionPhase.CONTINUOUS_TRADING,
        micro_session_id=f"2026-06-12:weekday_morning:07:{instrument_id}",
        broker_trading_status="normal_trading",
    )


def test_candle_aggregation_uses_only_closed_input_candles() -> None:
    engine = BarEngine(target_timeframes=(Timeframe.M5,))

    assert engine.on_candle(one_minute_candle(0, closed=False)) == ()

    bars: list[Bar] = []
    for minute in range(5):
        bars.extend(engine.on_candle(one_minute_candle(minute)))

    assert len(bars) == 1
    bar = bars[0]
    assert bar.timeframe is Timeframe.M5
    assert bar.open_ts_utc == utc(2026, 6, 12, 7, 0)
    assert bar.close_ts_utc == utc(2026, 6, 12, 7, 5)
    assert bar.open_price == Decimal("100")
    assert bar.high_price == Decimal("105")
    assert bar.low_price == Decimal("99")
    assert bar.close_price == Decimal("104.5")
    assert bar.volume_lots == Decimal("50")
    assert bar.source_candle_count == 5


def test_market_quality_and_stale_data_detection() -> None:
    now = utc(2026, 6, 12, 7)
    order_book = OrderBookSnapshot(
        instrument_id="MOEX:SBER",
        bids=(
            PriceLevel(Decimal("100.00"), Decimal("10")),
            PriceLevel(Decimal("99.90"), Decimal("5")),
        ),
        asks=(
            PriceLevel(Decimal("100.10"), Decimal("8")),
            PriceLevel(Decimal("100.20"), Decimal("4")),
        ),
        depth=2,
        exchange_ts=now,
        received_ts=now,
    )
    calculator = MarketStateCalculator(stale_after_ms=1000, depth_levels=2)
    state = calculator.from_order_book(order_book, now=now)
    stale = FeedFreshnessCalculator(stale_after_ms=1000).calculate(
        last_event_at=now,
        now=now + timedelta(seconds=2),
    )

    assert state.best_bid is not None
    assert state.best_bid.price == Decimal("100.00")
    assert state.best_ask is not None
    assert state.best_ask.price == Decimal("100.10")
    assert state.mid_price == Decimal("100.05")
    assert state.spread_abs == Decimal("0.10")
    assert state.spread_bps is not None
    assert state.spread_bps.quantize(Decimal("0.0001")) == Decimal("9.9950")
    assert state.book_imbalance is not None
    assert state.book_imbalance.quantize(Decimal("0.0001")) == Decimal("0.1111")
    assert state.market_quality_score is not None
    assert state.market_quality_score > Decimal("0.85")
    assert stale.is_stale
    assert stale.age_ms == 2000


class FakeRecoveryGateway:
    def __init__(
        self,
        *,
        candles_by_interval: dict[str, list[dict[str, object]]] | None = None,
        raise_on_candles: bool = False,
    ) -> None:
        self.candles_by_interval = candles_by_interval or {}
        self.raise_on_candles = raise_on_candles
        self.candle_requests: list[CandleRequest] = []
        self.orders_requests: list[OrdersRequest] = []
        self.order_state_requests: list[OrderStateRequest] = []

    async def get_candles(
        self,
        request: CandleRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.candle_requests.append(request)
        if self.raise_on_candles:
            raise RuntimeError("gap backfill failed")
        candles = self.candles_by_interval.get(request.interval)
        if candles is not None:
            return BrokerUnaryResponse(
                method_name="GetCandles",
                data={"candles": candles},
            )
        return BrokerUnaryResponse(
            method_name="GetCandles",
            data={
                "candles": [
                    {
                        "instrument_id": request.instrument.instrument_id,
                        "timeframe": request.interval,
                        "open_ts_utc": request.from_.isoformat(),
                        "close_ts_utc": request.to.isoformat(),
                        "open": "100",
                        "high": "101",
                        "low": "99",
                        "close": "100.5",
                        "volume": "10",
                        "is_closed": True,
                        "source": "backfill",
                    }
                ]
            },
        )

    async def reconcile_open_orders(
        self,
        request: OrdersRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.orders_requests.append(request)
        return BrokerUnaryResponse(method_name="GetOrders", data={"orders": []})

    async def reconcile_order_state(
        self,
        request: OrderStateRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.order_state_requests.append(request)
        return BrokerUnaryResponse(
            method_name="GetOrderState",
            data={
                "request_order_id": str(request.request_order_id)
                if request.request_order_id is not None
                else None,
                "exchange_order_id": request.exchange_order_id,
                "broker_status": "observed",
            },
        )


class FakeHistoricalBackfillGateway:
    def __init__(self, candles: list[dict[str, object]]) -> None:
        self.candles = candles
        self.candle_requests: list[CandleRequest] = []
        self.resolve_requests: list[InstrumentResolveRequest] = []

    async def resolve_instruments(
        self,
        request: InstrumentResolveRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.resolve_requests.append(request)
        return BrokerUnaryResponse(
            method_name="ResolveInstruments",
            data={
                "instruments": [
                    {
                        "instrument_id": f"uid-{ticker.lower()}",
                        "instrument_uid": f"uid-{ticker.lower()}",
                        "figi": f"figi-{ticker.lower()}",
                        "ticker": ticker,
                        "class_code": request.class_code,
                        "name": ticker,
                        "lot_size": 10,
                        "min_price_increment": "0.01",
                        "currency": "RUB",
                        "api_trade_available": True,
                        "short_available": True,
                        "supports_weekend": False,
                    }
                    for ticker in request.tickers
                ]
            },
            headers={},
        )

    async def get_candles(
        self,
        request: CandleRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.candle_requests.append(request)
        return BrokerUnaryResponse(
            method_name="GetCandles",
            data={"candles": self.candles},
            headers={},
        )


def test_reconnect_recovery_backfills_candles_and_refreshes_account_state() -> None:
    fake_gateway = FakeRecoveryGateway()
    event_bus = MarketEventBus()
    audit_events: list[tuple[str, dict[str, object]]] = []
    positions_refreshes: list[str] = []
    request_order_id = uuid4()

    async def run() -> None:
        coordinator = GapRecoveryCoordinator(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            event_bus=event_bus,
            refresh_positions_hook=lambda account_id: positions_refreshes.append(account_id),
            audit_event_hook=lambda event_type, payload: audit_events.append(
                (event_type, payload)
            ),
        )
        await coordinator.recover_after_reconnect(
            GapRecoveryRequest(
                instruments=(InstrumentRef(instrument_id="MOEX:SBER"),),
                candle_timeframes=(Timeframe.M1, Timeframe.M5),
                from_ts_utc=utc(2026, 6, 12, 7),
                to_ts_utc=utc(2026, 6, 12, 7, 5),
                account_id="account-1",
                working_order_request_ids=(request_order_id,),
            )
        )

    asyncio.run(run())

    assert [request.interval for request in fake_gateway.candle_requests] == ["1m", "5m"]
    assert [request.account_id for request in fake_gateway.orders_requests] == ["account-1"]
    assert [request.request_order_id for request in fake_gateway.order_state_requests] == [
        request_order_id
    ]
    assert positions_refreshes == ["account-1"]
    assert {
        event_type for event_type, _payload in audit_events
    } >= {
        "stream_gap_recovery_requested",
        "stream_gap_backfill_started",
        "stream_gap_backfill_completed",
        "order_reconciliation_completed",
        "position_reconciliation_completed",
    }
    assert [event.event_type for event in event_bus.published_events] == [
        MarketEventType.RECOVERY_REQUESTED,
        MarketEventType.CANDLE,
        MarketEventType.CANDLE,
        MarketEventType.RECOVERY_COMPLETED,
    ]


def test_reconnect_after_missing_candles_restores_bars() -> None:
    fake_gateway = FakeRecoveryGateway(
        candles_by_interval={
            Timeframe.M1.value: [recovered_candle_payload(minute) for minute in range(5)]
        }
    )
    event_bus = MarketEventBus()
    pipeline = MarketDataPipeline(
        event_bus=event_bus,
        session_context_provider=lambda instrument_id: session_context(instrument_id),
        bar_engine=BarEngine(target_timeframes=(Timeframe.M5,)),
        read_models=MarketReadModelStore(),
    )
    pipeline.register()

    async def run() -> None:
        coordinator = GapRecoveryCoordinator(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            event_bus=event_bus,
        )
        coordinator.record_good_event(
            stream_name="candles",
            instrument_id="MOEX:SBER",
            timeframe=Timeframe.M1,
            ts_utc=utc(2026, 6, 12, 7),
        )
        result = await coordinator.recover_after_reconnect(
            GapRecoveryRequest(
                instruments=(InstrumentRef(instrument_id="MOEX:SBER"),),
                candle_timeframes=(Timeframe.M1,),
                from_ts_utc=utc(2026, 6, 12, 7),
                to_ts_utc=utc(2026, 6, 12, 7, 5),
                stream_name="candles",
            )
        )
        assert result.recovered_candles == 5

    asyncio.run(run())

    closed_bars = [
        event.payload
        for event in event_bus.published_events
        if event.event_type is MarketEventType.BAR_CLOSED
    ]
    assert len(closed_bars) == 1
    bar = cast(Bar, closed_bars[0])
    assert bar.timeframe is Timeframe.M5
    assert bar.close_ts_utc == utc(2026, 6, 12, 7, 5)
    assert bar.source_candle_count == 5


def test_duplicate_recovered_candle_does_not_duplicate_market_candle() -> None:
    candle_payload = recovered_candle_payload(0)
    fake_gateway = FakeRecoveryGateway(
        candles_by_interval={Timeframe.M1.value: [candle_payload, candle_payload]}
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_bus = MarketEventBus()

    with Session(engine) as session:
        pipeline = MarketDataPipeline(
            event_bus=event_bus,
            session_context_provider=lambda instrument_id: session_context(instrument_id),
            store=SqlAlchemyMarketDataStore(session),
        )
        pipeline.register()

        async def run() -> None:
            coordinator = GapRecoveryCoordinator(
                broker_gateway=cast(BrokerGateway, fake_gateway),
                event_bus=event_bus,
            )
            await coordinator.recover_after_reconnect(
                GapRecoveryRequest(
                    instruments=(InstrumentRef(instrument_id="MOEX:SBER"),),
                    candle_timeframes=(Timeframe.M1,),
                    from_ts_utc=utc(2026, 6, 12, 7),
                    to_ts_utc=utc(2026, 6, 12, 7, 1),
                    stream_name="candles",
                )
            )

        asyncio.run(run())
        session.commit()
        market_candle_count = session.scalar(select(func.count()).select_from(MarketCandle))

    published_candles = [
        event for event in event_bus.published_events if event.event_type is MarketEventType.CANDLE
    ]
    assert len(published_candles) == 1
    assert market_candle_count == 1


def test_pipeline_updates_live_dashboard_read_models() -> None:
    event_bus = MarketEventBus()
    read_models = MarketReadModelStore()
    pipeline = MarketDataPipeline(
        event_bus=event_bus,
        session_context_provider=lambda instrument_id: session_context(instrument_id),
        bar_engine=BarEngine(target_timeframes=(Timeframe.M5,)),
        read_models=read_models,
    )
    pipeline.register()
    now = utc(2026, 6, 12, 7)

    async def run() -> None:
        await event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.ORDER_BOOK,
                payload=OrderBookSnapshot(
                    instrument_id="MOEX:SBER",
                    bids=(PriceLevel(Decimal("100"), Decimal("10")),),
                    asks=(PriceLevel(Decimal("100.10"), Decimal("8")),),
                    depth=1,
                    exchange_ts=now,
                    received_ts=now,
                ),
                ts_utc=now,
                instrument_id="MOEX:SBER",
            )
        )
        await event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.MARKET_TRADE,
                payload=MarketTrade(
                    instrument_id="MOEX:SBER",
                    price=Decimal("100.05"),
                    quantity_lots=Decimal("2"),
                    side="buy",
                    exchange_ts=now,
                    received_ts=now,
                ),
                ts_utc=now,
                instrument_id="MOEX:SBER",
            )
        )
        for minute in range(5):
            candle = one_minute_candle(minute)
            await event_bus.publish(
                MarketDataEvent(
                    event_type=MarketEventType.CANDLE,
                    payload=candle,
                    ts_utc=candle.close_ts_utc,
                    instrument_id=candle.instrument_id,
                )
            )

    asyncio.run(run())

    assert read_models.live_order_book("MOEX:SBER") is not None
    assert len(read_models.recent_trades("MOEX:SBER")) == 1
    signal_context = read_models.current_signal_context("MOEX:SBER")
    assert signal_context is not None
    latest_closed_bars = cast(dict[str, object], signal_context["latest_closed_bars"])
    assert "5m" in latest_closed_bars
    assert MarketEventType.BAR_CLOSED in [event.event_type for event in event_bus.published_events]
    assert MarketEventType.MARKET_STATE_UPDATED in [
        event.event_type for event in event_bus.published_events
    ]


def test_historical_backfill_writes_raw_and_derived_bars_idempotently() -> None:
    candles = [recovered_candle_payload(minute) for minute in range(15)]
    fake_gateway = FakeHistoricalBackfillGateway(candles)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        service = HistoricalCandleBackfillService(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            session=session,
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.HISTORICAL_REPLAY),
        )
        config = HistoricalBackfillConfig(
            instruments=("SBER",),
            chunk_days=1,
            dry_run=False,
        )

        first = asyncio.run(
            service.run(
                config,
                from_ts_utc=utc(2026, 6, 12, 7),
                to_ts_utc=utc(2026, 6, 12, 7, 15),
            )
        )
        second = asyncio.run(
            service.run(
                config,
                from_ts_utc=utc(2026, 6, 12, 7),
                to_ts_utc=utc(2026, 6, 12, 7, 15),
            )
        )
        session.commit()

        rows = session.execute(select(MarketCandle)).scalars().all()

    first_instrument = first.instruments[0]
    second_instrument = second.instruments[0]
    counts_by_timeframe: dict[str, int] = {}
    for row in rows:
        counts_by_timeframe[row.timeframe] = counts_by_timeframe.get(row.timeframe, 0) + 1

    assert first_instrument.raw_candles_fetched == 15
    assert first_instrument.raw_candles_written == 15
    assert first_instrument.derived_bars_written == {"5m": 3, "10m": 1, "15m": 1}
    assert second_instrument.raw_candles_existing == 15
    assert second_instrument.derived_bars_existing == {"5m": 3, "10m": 1, "15m": 1}
    assert counts_by_timeframe == {"1m": 15, "5m": 3, "10m": 1, "15m": 1}
    assert len(fake_gateway.candle_requests) == 2


def test_historical_backfill_resolves_seed_row_before_real_get_candles() -> None:
    candles = [recovered_candle_payload(0)]
    fake_gateway = FakeHistoricalBackfillGateway(candles)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            InstrumentRegistry(
                instrument_id="MOEX:SBER",
                ticker="SBER",
                class_code="TQBR",
                figi=None,
                instrument_uid=None,
                name="SBER",
                lot_size=10,
                min_price_increment=Decimal("0.01"),
                currency="RUB",
                is_enabled=True,
                supports_morning=True,
                supports_evening=True,
                supports_weekend=False,
                instrument_payload={},
            )
        )
        service = HistoricalCandleBackfillService(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            session=session,
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        )
        result = asyncio.run(
            service.run(
                HistoricalBackfillConfig(
                    instruments=("SBER",),
                    chunk_days=1,
                    dry_run=False,
                    runtime_mode=RuntimeMode.SHADOW.value,
                ),
                from_ts_utc=utc(2026, 6, 12, 7),
                to_ts_utc=utc(2026, 6, 12, 7, 1),
            )
        )

        row = session.get(InstrumentRegistry, "MOEX:SBER")

    assert row is not None
    assert row.instrument_uid == "uid-sber"
    assert row.source == "tbank_resolved"
    assert fake_gateway.resolve_requests[0].tickers == ("SBER",)
    assert fake_gateway.candle_requests[0].instrument.instrument_uid == "uid-sber"
    assert result.plan.instruments[0].instrument_id == "MOEX:SBER"
    engine.dispose()


def test_historical_backfill_dry_run_builds_plan_without_fetching_candles() -> None:
    fake_gateway = FakeHistoricalBackfillGateway([])
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        service = HistoricalCandleBackfillService(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            session=session,
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.HISTORICAL_REPLAY),
        )
        result = asyncio.run(
                service.run(
                    HistoricalBackfillConfig(instruments=("SBER", "GAZP", "LKOH"), dry_run=True),
                    from_ts_utc=utc(2026, 6, 1, 0),
                    to_ts_utc=utc(2026, 6, 18, 0),
                )
            )

    assert result.dry_run
    assert len(result.plan.instruments) == 3
    assert len(result.plan.chunks) == 51
    assert fake_gateway.candle_requests == []

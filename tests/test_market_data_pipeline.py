from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    CandleRequest,
    InstrumentRef,
    OrdersRequest,
)
from trade_core.market_data import (
    Bar,
    BarEngine,
    Candle,
    GapRecoveryCoordinator,
    GapRecoveryRequest,
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
from trade_core.session.models import SessionEventContext
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
    def __init__(self) -> None:
        self.candle_requests: list[CandleRequest] = []
        self.orders_requests: list[OrdersRequest] = []

    async def get_candles(
        self,
        request: CandleRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.candle_requests.append(request)
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


def test_reconnect_recovery_backfills_candles_and_refreshes_account_state() -> None:
    fake_gateway = FakeRecoveryGateway()
    event_bus = MarketEventBus()
    positions_refreshes: list[str] = []

    async def run() -> None:
        coordinator = GapRecoveryCoordinator(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            event_bus=event_bus,
            refresh_positions_hook=lambda account_id: positions_refreshes.append(account_id),
        )
        await coordinator.recover_after_reconnect(
            GapRecoveryRequest(
                instruments=(InstrumentRef(instrument_id="MOEX:SBER"),),
                candle_timeframes=(Timeframe.M1, Timeframe.M5),
                from_ts_utc=utc(2026, 6, 12, 7),
                to_ts_utc=utc(2026, 6, 12, 7, 5),
                account_id="account-1",
            )
        )

    asyncio.run(run())

    assert [request.interval for request in fake_gateway.candle_requests] == ["1m", "5m"]
    assert [request.account_id for request in fake_gateway.orders_requests] == ["account-1"]
    assert positions_refreshes == ["account-1"]
    assert [event.event_type for event in event_bus.published_events] == [
        MarketEventType.RECOVERY_REQUESTED,
        MarketEventType.CANDLE,
        MarketEventType.CANDLE,
        MarketEventType.RECOVERY_COMPLETED,
    ]


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

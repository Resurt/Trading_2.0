"""Market data pipeline wiring event bus, bar engine, read models, and storage."""

from __future__ import annotations

from collections.abc import Callable

from trade_core.market_data.bars import BarEngine
from trade_core.market_data.event_bus import MarketEventBus
from trade_core.market_data.events import (
    Bar,
    Candle,
    LastPriceTick,
    MarketDataEvent,
    MarketEventType,
    MarketTrade,
    OrderBookSnapshot,
    TradingStatusTick,
)
from trade_core.market_data.persistence import SqlAlchemyMarketDataStore
from trade_core.market_data.read_models import MarketReadModelStore
from trade_core.session.models import SessionEventContext

SessionContextProvider = Callable[[str], SessionEventContext]


class MarketDataPipeline:
    """Consume market data events and maintain bars, stores, and read models."""

    def __init__(
        self,
        *,
        event_bus: MarketEventBus,
        session_context_provider: SessionContextProvider,
        bar_engine: BarEngine | None = None,
        read_models: MarketReadModelStore | None = None,
        store: SqlAlchemyMarketDataStore | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._session_context_provider = session_context_provider
        self._bar_engine = bar_engine or BarEngine()
        self._read_models = read_models or MarketReadModelStore()
        self._store = store

    @property
    def read_models(self) -> MarketReadModelStore:
        return self._read_models

    def register(self) -> None:
        self._event_bus.subscribe(MarketEventType.CANDLE, self.handle_event)
        self._event_bus.subscribe(MarketEventType.ORDER_BOOK, self.handle_event)
        self._event_bus.subscribe(MarketEventType.LAST_PRICE, self.handle_event)
        self._event_bus.subscribe(MarketEventType.TRADING_STATUS, self.handle_event)
        self._event_bus.subscribe(MarketEventType.MARKET_TRADE, self.handle_event)

    async def handle_event(self, event: MarketDataEvent) -> None:
        if event.event_type is MarketEventType.CANDLE and isinstance(event.payload, Candle):
            await self._handle_candle(event.payload)
        elif event.event_type is MarketEventType.ORDER_BOOK and isinstance(
            event.payload,
            OrderBookSnapshot,
        ):
            await self._handle_order_book(event.payload)
        elif event.event_type is MarketEventType.LAST_PRICE and isinstance(
            event.payload,
            LastPriceTick,
        ):
            self._read_models.apply_last_price(event.payload)
        elif event.event_type is MarketEventType.TRADING_STATUS and isinstance(
            event.payload,
            TradingStatusTick,
        ):
            self._read_models.apply_trading_status(event.payload)
            if self._store is not None:
                context = self._session_context_provider(event.payload.instrument_id)
                self._store.save_status(tick=event.payload, context=context)
        elif event.event_type is MarketEventType.MARKET_TRADE and isinstance(
            event.payload,
            MarketTrade,
        ):
            self._read_models.apply_market_trade(event.payload)

    async def _handle_candle(self, candle: Candle) -> None:
        if self._store is not None and candle.is_closed:
            context = self._session_context_provider(candle.instrument_id)
            self._store.save_candle(candle=candle, context=context)

        for bar in self._bar_engine.on_candle(candle):
            await self._publish_closed_bar(bar)

    async def _handle_order_book(self, order_book: OrderBookSnapshot) -> None:
        market_state = self._read_models.apply_order_book(
            order_book,
            now=order_book.received_ts,
        )
        if self._store is not None:
            context = self._session_context_provider(order_book.instrument_id)
            self._store.save_order_book_summary(
                order_book=order_book,
                market_state=market_state,
                context=context,
            )
        await self._event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.MARKET_STATE_UPDATED,
                payload=market_state,
                ts_utc=order_book.received_ts,
                instrument_id=order_book.instrument_id,
            )
        )

    async def _publish_closed_bar(self, bar: Bar) -> None:
        self._read_models.apply_bar(bar)
        if self._store is not None:
            context = self._session_context_provider(bar.instrument_id)
            self._store.save_bar(bar=bar, context=context)
        await self._event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.BAR_CLOSED,
                payload=bar,
                ts_utc=bar.close_ts_utc,
                instrument_id=bar.instrument_id,
            )
        )

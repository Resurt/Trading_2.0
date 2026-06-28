"""Market data pipeline wiring event bus, bar engine, read models, and storage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

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
from trading_common.observability import DomainEventType
from trading_common.telemetry import bind_context, get_logger, log_event

SessionContextProvider = Callable[[str], SessionEventContext]
LOGGER = get_logger(__name__)


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
            context = self._session_context_provider(event.payload.instrument_id)
            _log_market_event(
                event_type=DomainEventType.MARKET_STATUS_CHANGED.value,
                component="market_data.pipeline",
                context=context,
                instrument_id=event.payload.instrument_id,
                payload={
                    "trading_status": event.payload.trading_status,
                    "api_trade_available": event.payload.api_trade_available,
                    "source": "broker_trading_status",
                },
            )
            if self._store is not None:
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
        market_state = replace(
            market_state,
            payload=_market_state_payload_from_order_book(order_book.payload),
        )
        if self._store is not None:
            context = self._session_context_provider(order_book.instrument_id)
            payload = dict(order_book.payload)
            payload["recent_market_trades"] = self._read_models.recent_trades(
                order_book.instrument_id
            )[:20]
            enriched_order_book = replace(order_book, payload=payload)
            self._store.save_order_book_summary(
                order_book=enriched_order_book,
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
        context = self._session_context_provider(bar.instrument_id)
        _log_market_event(
            event_type=DomainEventType.BAR_CLOSED.value,
            component="bar_engine",
            context=context,
            instrument_id=bar.instrument_id,
            timeframe=bar.timeframe.value,
            payload={
                "open_ts_utc": bar.open_ts_utc.isoformat(),
                "close_ts_utc": bar.close_ts_utc.isoformat(),
                "source_candle_count": bar.source_candle_count,
            },
        )
        if self._store is not None:
            self._store.save_bar(bar=bar, context=context)
        await self._event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.BAR_CLOSED,
                payload=bar,
                ts_utc=bar.close_ts_utc,
                instrument_id=bar.instrument_id,
            )
        )


def _log_market_event(
    *,
    event_type: str,
    component: str,
    context: SessionEventContext,
    instrument_id: str,
    payload: dict[str, object],
    timeframe: str | None = None,
) -> None:
    with bind_context(
        session_type=context.session_type.value,
        exchange_phase=context.session_phase.value,
        micro_session_id=context.micro_session_id,
        instrument=instrument_id,
        timeframe=timeframe,
    ):
        log_event(
            logger=LOGGER,
            event_type=event_type,
            component=component,
            details=payload,
        )


def _market_state_payload_from_order_book(payload: dict[str, object]) -> dict[str, object]:
    carried_keys = {
        "source",
        "quote_source",
        "data_only_polling_fallback",
        "include_in_calibration",
        "calibration_allowed",
        "venue_type",
        "reason_code",
    }
    return {key: payload[key] for key in carried_keys if key in payload}

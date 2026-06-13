"""Broker stream subscriptions mapped to internal market data events."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trade_core.broker_gateway import BrokerGateway, StreamEvent
from trade_core.market_data.event_bus import MarketEventBus
from trade_core.market_data.events import (
    Candle,
    LastPriceTick,
    MarketDataEvent,
    MarketEventType,
    MarketTrade,
    OrderBookSnapshot,
    PriceLevel,
    TradingStatusTick,
    UserOrderStateTick,
    datetime_from,
    decimal_from,
    ensure_exchange_tz,
    ensure_utc,
    parse_timeframe,
)

JsonMapping = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MarketDataSubscriptionConfig:
    market_stream_names: tuple[str, ...] = (
        "candles",
        "order_book",
        "last_prices",
        "trading_status",
        "info",
        "market_trades",
    )
    account_id: str | None = None


class MarketDataSubscriptionService:
    """Run broker streams and publish normalized events to the internal bus."""

    def __init__(
        self,
        *,
        broker_gateway: BrokerGateway,
        event_bus: MarketEventBus,
    ) -> None:
        self._broker_gateway = broker_gateway
        self._event_bus = event_bus

    async def start(self, config: MarketDataSubscriptionConfig) -> tuple[asyncio.Task[None], ...]:
        tasks = [
            asyncio.create_task(self._run_market_stream(stream_name))
            for stream_name in config.market_stream_names
        ]
        if config.account_id is not None:
            tasks.append(asyncio.create_task(self._run_order_stream(config.account_id)))
        return tuple(tasks)

    async def handle_stream_event(self, event: StreamEvent) -> None:
        payload = event.payload
        normalized = _normalize_event_type(event.stream_name, event.event_type)
        received_at = ensure_utc(event.received_at or datetime.now().astimezone())

        if normalized is MarketEventType.CANDLE:
            candle = candle_from_mapping(payload, received_at=received_at)
            await self._event_bus.publish(
                MarketDataEvent(
                    event_type=MarketEventType.CANDLE,
                    payload=candle,
                    ts_utc=received_at,
                    instrument_id=candle.instrument_id,
                )
            )
        elif normalized is MarketEventType.ORDER_BOOK:
            order_book = order_book_from_mapping(payload, received_at=received_at)
            await self._event_bus.publish(
                MarketDataEvent(
                    event_type=MarketEventType.ORDER_BOOK,
                    payload=order_book,
                    ts_utc=received_at,
                    instrument_id=order_book.instrument_id,
                )
            )
        elif normalized is MarketEventType.LAST_PRICE:
            last_price_tick = last_price_from_mapping(payload, received_at=received_at)
            await self._event_bus.publish(
                MarketDataEvent(
                    event_type=MarketEventType.LAST_PRICE,
                    payload=last_price_tick,
                    ts_utc=received_at,
                    instrument_id=last_price_tick.instrument_id,
                )
            )
        elif normalized is MarketEventType.TRADING_STATUS:
            status_tick = trading_status_from_mapping(payload, received_at=received_at)
            await self._event_bus.publish(
                MarketDataEvent(
                    event_type=MarketEventType.TRADING_STATUS,
                    payload=status_tick,
                    ts_utc=received_at,
                    instrument_id=status_tick.instrument_id,
                )
            )
        elif normalized is MarketEventType.MARKET_TRADE:
            trade = market_trade_from_mapping(payload, received_at=received_at)
            await self._event_bus.publish(
                MarketDataEvent(
                    event_type=MarketEventType.MARKET_TRADE,
                    payload=trade,
                    ts_utc=received_at,
                    instrument_id=trade.instrument_id,
                )
            )
        elif normalized is MarketEventType.USER_ORDER_STATE:
            order_state_tick = user_order_state_from_mapping(payload, received_at=received_at)
            await self._event_bus.publish(
                MarketDataEvent(
                    event_type=MarketEventType.USER_ORDER_STATE,
                    payload=order_state_tick,
                    ts_utc=received_at,
                    instrument_id=None,
                )
            )

    async def _run_market_stream(self, stream_name: str) -> None:
        async for event in self._broker_gateway.stream_market_data(stream_name):
            await self.handle_stream_event(event)

    async def _run_order_stream(self, account_id: str) -> None:
        async for event in self._broker_gateway.stream_orders(account_id):
            await self.handle_stream_event(event)


def candle_from_mapping(payload: JsonMapping, *, received_at: datetime) -> Candle:
    open_ts = datetime_from(
        _first(payload, "open_ts_utc", "time", "open_time"),
        default=received_at,
    )
    close_ts = datetime_from(_first(payload, "close_ts_utc", "close_time"), default=received_at)
    exchange_open_ts = ensure_exchange_tz(
        datetime_from(
            payload.get("exchange_open_ts", payload.get("time", payload.get("open_time"))),
            default=open_ts,
        )
    )
    exchange_close_ts = ensure_exchange_tz(
        datetime_from(payload.get("exchange_close_ts", payload.get("close_time")), default=close_ts)
    )
    return Candle(
        instrument_id=str(_first(payload, "instrument_id", "figi", "instrument_uid")),
        timeframe=parse_timeframe(str(payload.get("timeframe", payload.get("interval", "1m")))),
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
        exchange_open_ts=exchange_open_ts,
        exchange_close_ts=exchange_close_ts,
        open_price=decimal_from(_first(payload, "open_price", "open")),
        high_price=decimal_from(_first(payload, "high_price", "high")),
        low_price=decimal_from(_first(payload, "low_price", "low")),
        close_price=decimal_from(_first(payload, "close_price", "close")),
        volume_lots=decimal_from(payload.get("volume_lots", payload.get("volume", "0"))),
        is_closed=bool(payload.get("is_closed", payload.get("complete", True))),
        source=str(payload.get("source", "stream")),
        payload=dict(payload),
    )


def order_book_from_mapping(payload: JsonMapping, *, received_at: datetime) -> OrderBookSnapshot:
    bids = tuple(_level_from_mapping(level) for level in payload.get("bids", ()))
    asks = tuple(_level_from_mapping(level) for level in payload.get("asks", ()))
    exchange_ts = datetime_from(payload.get("exchange_ts"), default=received_at)
    return OrderBookSnapshot(
        instrument_id=str(_first(payload, "instrument_id", "figi", "instrument_uid")),
        bids=bids,
        asks=asks,
        depth=int(payload.get("depth", max(len(bids), len(asks)))),
        exchange_ts=exchange_ts,
        received_ts=received_at,
        payload=dict(payload),
    )


def last_price_from_mapping(payload: JsonMapping, *, received_at: datetime) -> LastPriceTick:
    return LastPriceTick(
        instrument_id=str(_first(payload, "instrument_id", "figi", "instrument_uid")),
        price=decimal_from(_first(payload, "price", "last_price")),
        exchange_ts=datetime_from(payload.get("exchange_ts"), default=received_at),
        received_ts=received_at,
        payload=dict(payload),
    )


def trading_status_from_mapping(
    payload: JsonMapping,
    *,
    received_at: datetime,
) -> TradingStatusTick:
    return TradingStatusTick(
        instrument_id=str(_first(payload, "instrument_id", "figi", "instrument_uid")),
        trading_status=str(_first(payload, "trading_status", "status")),
        api_trade_available=bool(payload.get("api_trade_available", True)),
        exchange_ts=datetime_from(payload.get("exchange_ts"), default=received_at),
        received_ts=received_at,
        payload=dict(payload),
    )


def market_trade_from_mapping(payload: JsonMapping, *, received_at: datetime) -> MarketTrade:
    return MarketTrade(
        instrument_id=str(_first(payload, "instrument_id", "figi", "instrument_uid")),
        price=decimal_from(_first(payload, "price", "trade_price")),
        quantity_lots=decimal_from(payload.get("quantity_lots", payload.get("quantity", "0"))),
        side=str(payload["side"]) if "side" in payload else None,
        exchange_ts=datetime_from(payload.get("exchange_ts"), default=received_at),
        received_ts=received_at,
        trade_id=str(payload["trade_id"]) if "trade_id" in payload else None,
        payload=dict(payload),
    )


def user_order_state_from_mapping(
    payload: JsonMapping,
    *,
    received_at: datetime,
) -> UserOrderStateTick:
    return UserOrderStateTick(
        account_id=str(payload.get("account_id", "")),
        request_order_id=(
            str(payload["request_order_id"]) if "request_order_id" in payload else None
        ),
        exchange_order_id=(
            str(payload["exchange_order_id"]) if "exchange_order_id" in payload else None
        ),
        broker_status=str(_first(payload, "broker_status", "status")),
        received_ts=received_at,
        payload=dict(payload),
    )


def _level_from_mapping(value: object) -> PriceLevel:
    if isinstance(value, Mapping):
        return PriceLevel(
            price=decimal_from(_first(value, "price")),
            quantity_lots=decimal_from(value.get("quantity_lots", value.get("quantity", "0"))),
        )
    if isinstance(value, tuple | list) and len(value) >= 2:
        return PriceLevel(price=decimal_from(value[0]), quantity_lots=decimal_from(value[1]))
    msg = f"Unsupported order book level: {value!r}"
    raise TypeError(msg)


def _normalize_event_type(stream_name: str, event_type: str) -> MarketEventType:
    raw = f"{stream_name}:{event_type}".lower()
    if "order_state" in raw or "user_order" in raw:
        return MarketEventType.USER_ORDER_STATE
    if "order_book" in raw or "book" in raw:
        return MarketEventType.ORDER_BOOK
    if "last_price" in raw or "lastprices" in raw:
        return MarketEventType.LAST_PRICE
    if "trading_status" in raw or raw.startswith("info:"):
        return MarketEventType.TRADING_STATUS
    if "market_trade" in raw or "anonymous_trade" in raw or "trade" in raw:
        return MarketEventType.MARKET_TRADE
    return MarketEventType.CANDLE


def _first(payload: JsonMapping, *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    msg = f"Missing required payload key, expected one of: {keys}"
    raise KeyError(msg)

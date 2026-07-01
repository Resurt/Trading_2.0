"""Typed market data events used inside trade-core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

JsonPayload = dict[str, Any]
EXCHANGE_TZ = ZoneInfo("Europe/Moscow")


class MarketEventType(StrEnum):
    CANDLE = "candle"
    ORDER_BOOK = "order_book"
    LAST_PRICE = "last_price"
    TRADING_STATUS = "trading_status"
    MARKET_TRADE = "market_trade"
    USER_ORDER_STATE = "user_order_state"
    BAR_CLOSED = "bar_closed"
    MARKET_STATE_UPDATED = "market_state_updated"
    RECOVERY_REQUESTED = "recovery_requested"
    RECOVERY_COMPLETED = "recovery_completed"


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"

    @property
    def minutes(self) -> int:
        return int(self.value.removesuffix("m"))


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    quantity_lots: Decimal

    def as_read_model(self) -> JsonPayload:
        return {"price": str(self.price), "quantity_lots": str(self.quantity_lots)}


@dataclass(frozen=True, slots=True)
class Candle:
    instrument_id: str
    timeframe: Timeframe
    open_ts_utc: datetime
    close_ts_utc: datetime
    exchange_open_ts: datetime
    exchange_close_ts: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume_lots: Decimal
    is_closed: bool
    source: str = "stream"
    payload: JsonPayload = field(default_factory=dict)

    def as_read_model(self) -> JsonPayload:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe.value,
            "open_ts_utc": self.open_ts_utc.isoformat(),
            "close_ts_utc": self.close_ts_utc.isoformat(),
            "exchange_open_ts": self.exchange_open_ts.isoformat(),
            "exchange_close_ts": self.exchange_close_ts.isoformat(),
            "open_price": str(self.open_price),
            "high_price": str(self.high_price),
            "low_price": str(self.low_price),
            "close_price": str(self.close_price),
            "volume_lots": str(self.volume_lots),
            "is_closed": self.is_closed,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class Bar:
    instrument_id: str
    timeframe: Timeframe
    open_ts_utc: datetime
    close_ts_utc: datetime
    exchange_open_ts: datetime
    exchange_close_ts: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume_lots: Decimal
    source_candle_count: int
    is_closed: bool = True

    def as_candle(self, *, source: str = "bar_engine") -> Candle:
        return Candle(
            instrument_id=self.instrument_id,
            timeframe=self.timeframe,
            open_ts_utc=self.open_ts_utc,
            close_ts_utc=self.close_ts_utc,
            exchange_open_ts=self.exchange_open_ts,
            exchange_close_ts=self.exchange_close_ts,
            open_price=self.open_price,
            high_price=self.high_price,
            low_price=self.low_price,
            close_price=self.close_price,
            volume_lots=self.volume_lots,
            is_closed=self.is_closed,
            source=source,
            payload={"source_candle_count": self.source_candle_count},
        )

    def as_read_model(self) -> JsonPayload:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe.value,
            "open_ts_utc": self.open_ts_utc.isoformat(),
            "close_ts_utc": self.close_ts_utc.isoformat(),
            "exchange_open_ts": self.exchange_open_ts.isoformat(),
            "exchange_close_ts": self.exchange_close_ts.isoformat(),
            "open_price": str(self.open_price),
            "high_price": str(self.high_price),
            "low_price": str(self.low_price),
            "close_price": str(self.close_price),
            "volume_lots": str(self.volume_lots),
            "source_candle_count": self.source_candle_count,
            "is_closed": self.is_closed,
        }


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    instrument_id: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    depth: int
    exchange_ts: datetime | None
    received_ts: datetime
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LastPriceTick:
    instrument_id: str
    price: Decimal
    exchange_ts: datetime
    received_ts: datetime
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketTrade:
    instrument_id: str
    price: Decimal
    quantity_lots: Decimal
    side: str | None
    exchange_ts: datetime
    received_ts: datetime
    trade_id: str | None = None
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TradingStatusTick:
    instrument_id: str
    trading_status: str
    api_trade_available: bool
    exchange_ts: datetime
    received_ts: datetime
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserOrderStateTick:
    account_id: str
    request_order_id: str | None
    exchange_order_id: str | None
    broker_status: str
    received_ts: datetime
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketDataEvent:
    event_type: MarketEventType
    payload: object
    ts_utc: datetime
    instrument_id: str | None = None


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ensure_exchange_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=EXCHANGE_TZ)
    return value.astimezone(EXCHANGE_TZ)


def parse_timeframe(value: str | Timeframe) -> Timeframe:
    if isinstance(value, Timeframe):
        return value
    return Timeframe(value.lower())


def decimal_from(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(str(value))
    if isinstance(value, float):
        return Decimal(str(value))
    msg = f"Cannot convert value to Decimal: {value!r}"
    raise TypeError(msg)


def datetime_from(value: object, *, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        return ensure_utc(datetime.fromisoformat(value))
    if default is not None:
        return ensure_utc(default)
    msg = f"Cannot convert value to datetime: {value!r}"
    raise TypeError(msg)

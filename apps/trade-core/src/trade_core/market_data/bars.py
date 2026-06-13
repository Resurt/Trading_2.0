"""Closed bar aggregation from lower timeframe candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trade_core.market_data.events import Bar, Candle, Timeframe, ensure_utc


@dataclass(slots=True)
class _BarAccumulator:
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
    source_candle_count: int = 0

    def apply(self, candle: Candle) -> None:
        self.high_price = max(self.high_price, candle.high_price)
        self.low_price = min(self.low_price, candle.low_price)
        self.close_price = candle.close_price
        self.volume_lots += candle.volume_lots
        self.source_candle_count += 1

    def as_bar(self) -> Bar:
        return Bar(
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
            source_candle_count=self.source_candle_count,
            is_closed=True,
        )


class BarEngine:
    """Aggregate closed input candles into 5m/10m/15m closed bars."""

    def __init__(
        self,
        *,
        target_timeframes: tuple[Timeframe, ...] = (Timeframe.M5, Timeframe.M10, Timeframe.M15),
        include_forming: bool = False,
    ) -> None:
        self._target_timeframes = target_timeframes
        self._include_forming = include_forming
        self._buckets: dict[tuple[str, Timeframe, datetime], _BarAccumulator] = {}

    def on_candle(self, candle: Candle) -> tuple[Bar, ...]:
        if not candle.is_closed and not self._include_forming:
            return ()

        closed_bars: list[Bar] = []
        for timeframe in self._target_timeframes:
            bucket_open_exchange = _bucket_open(candle.exchange_open_ts, timeframe.minutes)
            bucket_close_exchange = bucket_open_exchange + timedelta(minutes=timeframe.minutes)
            bucket_open_utc = ensure_utc(bucket_open_exchange)
            bucket_close_utc = ensure_utc(bucket_close_exchange)
            key = (candle.instrument_id, timeframe, bucket_open_utc)
            accumulator = self._buckets.get(key)

            if accumulator is None:
                accumulator = _BarAccumulator(
                    instrument_id=candle.instrument_id,
                    timeframe=timeframe,
                    open_ts_utc=bucket_open_utc,
                    close_ts_utc=bucket_close_utc,
                    exchange_open_ts=bucket_open_exchange,
                    exchange_close_ts=bucket_close_exchange,
                    open_price=candle.open_price,
                    high_price=candle.high_price,
                    low_price=candle.low_price,
                    close_price=candle.close_price,
                    volume_lots=Decimal("0"),
                )
                self._buckets[key] = accumulator

            accumulator.apply(candle)
            if ensure_utc(candle.close_ts_utc) >= bucket_close_utc:
                closed_bars.append(accumulator.as_bar())
                del self._buckets[key]

        return tuple(closed_bars)


def _bucket_open(exchange_ts: datetime, minutes: int) -> datetime:
    if exchange_ts.tzinfo is None:
        exchange_ts = exchange_ts.replace(tzinfo=UTC)
    floored_minute = (exchange_ts.minute // minutes) * minutes
    return exchange_ts.replace(minute=floored_minute, second=0, microsecond=0)

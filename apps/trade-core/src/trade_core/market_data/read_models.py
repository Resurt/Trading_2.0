"""In-memory read models prepared for API/UI live dashboard."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from trade_core.market_data.calculators import MarketState, MarketStateCalculator
from trade_core.market_data.events import (
    Bar,
    LastPriceTick,
    MarketTrade,
    OrderBookSnapshot,
    TradingStatusTick,
)


@dataclass(frozen=True, slots=True)
class LiveOrderBookReadModel:
    instrument_id: str
    order_book: OrderBookSnapshot
    market_state: MarketState

    def as_read_model(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "exchange_ts": self.order_book.exchange_ts.isoformat(),
            "received_ts": self.order_book.received_ts.isoformat(),
            "depth": self.order_book.depth,
            "bids": [level.as_read_model() for level in self.order_book.bids],
            "asks": [level.as_read_model() for level in self.order_book.asks],
            "market_state": self.market_state.as_read_model(),
        }


@dataclass(slots=True)
class CurrentSignalContextReadModel:
    instrument_id: str
    latest_closed_bars: dict[str, Bar] = field(default_factory=dict)
    market_state: MarketState | None = None
    last_price: LastPriceTick | None = None
    trading_status: TradingStatusTick | None = None
    updated_at: datetime | None = None

    def as_read_model(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "latest_closed_bars": {
                timeframe: bar.as_read_model()
                for timeframe, bar in sorted(self.latest_closed_bars.items())
            },
            "market_state": self.market_state.as_read_model() if self.market_state else None,
            "last_price": str(self.last_price.price) if self.last_price else None,
            "trading_status": self.trading_status.trading_status if self.trading_status else None,
            "api_trade_available": (
                self.trading_status.api_trade_available if self.trading_status else None
            ),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MarketReadModelStore:
    """Mutable live read models consumed later by FastAPI/WebSocket BFF."""

    def __init__(
        self,
        *,
        market_state_calculator: MarketStateCalculator | None = None,
        trades_limit: int = 100,
    ) -> None:
        self._market_state_calculator = market_state_calculator or MarketStateCalculator()
        self._order_books: dict[str, LiveOrderBookReadModel] = {}
        self._trades: defaultdict[str, deque[MarketTrade]] = defaultdict(
            lambda: deque(maxlen=trades_limit)
        )
        self._signal_contexts: dict[str, CurrentSignalContextReadModel] = {}

    def apply_order_book(self, order_book: OrderBookSnapshot, *, now: datetime) -> MarketState:
        market_state = self._market_state_calculator.from_order_book(order_book, now=now)
        self._order_books[order_book.instrument_id] = LiveOrderBookReadModel(
            instrument_id=order_book.instrument_id,
            order_book=order_book,
            market_state=market_state,
        )
        context = self._context_for(order_book.instrument_id)
        context.market_state = market_state
        context.updated_at = now
        return market_state

    def apply_market_trade(self, trade: MarketTrade) -> None:
        self._trades[trade.instrument_id].appendleft(trade)
        context = self._context_for(trade.instrument_id)
        context.updated_at = trade.received_ts

    def apply_bar(self, bar: Bar) -> None:
        context = self._context_for(bar.instrument_id)
        context.latest_closed_bars[bar.timeframe.value] = bar
        context.updated_at = bar.close_ts_utc

    def apply_last_price(self, tick: LastPriceTick) -> None:
        context = self._context_for(tick.instrument_id)
        context.last_price = tick
        context.updated_at = tick.received_ts

    def apply_trading_status(self, tick: TradingStatusTick) -> None:
        context = self._context_for(tick.instrument_id)
        context.trading_status = tick
        context.updated_at = tick.received_ts

    def live_order_book(self, instrument_id: str) -> dict[str, object] | None:
        model = self._order_books.get(instrument_id)
        return model.as_read_model() if model else None

    def recent_trades(self, instrument_id: str) -> list[dict[str, object]]:
        return [
            {
                "instrument_id": trade.instrument_id,
                "price": str(trade.price),
                "quantity_lots": str(trade.quantity_lots),
                "side": trade.side,
                "exchange_ts": trade.exchange_ts.isoformat(),
                "received_ts": trade.received_ts.isoformat(),
                "trade_id": trade.trade_id,
                "source": "market_trades_stream",
                "venue_type": trade.payload.get("venue_type", "official_exchange"),
                "include_in_calibration": True,
            }
            for trade in self._trades[instrument_id]
        ]

    def current_signal_context(self, instrument_id: str) -> dict[str, object] | None:
        context = self._signal_contexts.get(instrument_id)
        return context.as_read_model() if context else None

    def _context_for(self, instrument_id: str) -> CurrentSignalContextReadModel:
        context = self._signal_contexts.get(instrument_id)
        if context is None:
            context = CurrentSignalContextReadModel(instrument_id=instrument_id)
            self._signal_contexts[instrument_id] = context
        return context

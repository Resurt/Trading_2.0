"""Market state calculators for order book, spread, and freshness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trade_core.market_data.events import OrderBookSnapshot, PriceLevel, ensure_utc

ZERO = Decimal("0")
ONE = Decimal("1")
TEN_THOUSAND = Decimal("10000")


@dataclass(frozen=True, slots=True)
class FeedFreshness:
    age_ms: int
    is_stale: bool


@dataclass(frozen=True, slots=True)
class MarketState:
    instrument_id: str
    best_bid: PriceLevel | None
    best_ask: PriceLevel | None
    mid_price: Decimal | None
    spread_abs: Decimal | None
    spread_bps: Decimal | None
    bid_depth_lots: Decimal
    ask_depth_lots: Decimal
    book_imbalance: Decimal | None
    market_quality_score: Decimal | None
    feed_freshness: FeedFreshness

    def as_read_model(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "best_bid": self.best_bid.as_read_model() if self.best_bid else None,
            "best_ask": self.best_ask.as_read_model() if self.best_ask else None,
            "mid_price": str(self.mid_price) if self.mid_price is not None else None,
            "spread_abs": str(self.spread_abs) if self.spread_abs is not None else None,
            "spread_bps": str(self.spread_bps) if self.spread_bps is not None else None,
            "bid_depth_lots": str(self.bid_depth_lots),
            "ask_depth_lots": str(self.ask_depth_lots),
            "book_imbalance": (
                str(self.book_imbalance) if self.book_imbalance is not None else None
            ),
            "market_quality_score": (
                str(self.market_quality_score)
                if self.market_quality_score is not None
                else None
            ),
            "feed_freshness": {
                "age_ms": self.feed_freshness.age_ms,
                "is_stale": self.feed_freshness.is_stale,
            },
        }


class FeedFreshnessCalculator:
    def __init__(self, *, stale_after_ms: int = 5000) -> None:
        self._stale_after_ms = stale_after_ms

    def calculate(self, *, last_event_at: datetime, now: datetime) -> FeedFreshness:
        age_ms = max(
            0,
            int((ensure_utc(now) - ensure_utc(last_event_at)).total_seconds() * 1000),
        )
        return FeedFreshness(age_ms=age_ms, is_stale=age_ms > self._stale_after_ms)


class MarketStateCalculator:
    """Derive spread, depth, imbalance, and quality from a lightweight book."""

    def __init__(self, *, stale_after_ms: int = 5000, depth_levels: int = 5) -> None:
        self._freshness = FeedFreshnessCalculator(stale_after_ms=stale_after_ms)
        self._depth_levels = depth_levels

    def from_order_book(self, order_book: OrderBookSnapshot, *, now: datetime) -> MarketState:
        bids = tuple(sorted(order_book.bids, key=lambda level: level.price, reverse=True))
        asks = tuple(sorted(order_book.asks, key=lambda level: level.price))
        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        bid_depth = sum((level.quantity_lots for level in bids[: self._depth_levels]), ZERO)
        ask_depth = sum((level.quantity_lots for level in asks[: self._depth_levels]), ZERO)

        mid_price: Decimal | None = None
        spread_abs: Decimal | None = None
        spread_bps: Decimal | None = None
        if best_bid is not None and best_ask is not None:
            mid_price = (best_bid.price + best_ask.price) / Decimal("2")
            spread_abs = best_ask.price - best_bid.price
            if mid_price > ZERO:
                spread_bps = (spread_abs / mid_price) * TEN_THOUSAND

        depth_total = bid_depth + ask_depth
        imbalance = None if depth_total == ZERO else (bid_depth - ask_depth) / depth_total
        freshness = self._freshness.calculate(last_event_at=order_book.received_ts, now=now)
        quality = _quality_score(spread_bps=spread_bps, imbalance=imbalance, freshness=freshness)

        return MarketState(
            instrument_id=order_book.instrument_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread_abs=spread_abs,
            spread_bps=spread_bps,
            bid_depth_lots=bid_depth,
            ask_depth_lots=ask_depth,
            book_imbalance=imbalance,
            market_quality_score=quality,
            feed_freshness=freshness,
        )


def _quality_score(
    *,
    spread_bps: Decimal | None,
    imbalance: Decimal | None,
    freshness: FeedFreshness,
) -> Decimal | None:
    if spread_bps is None:
        return None

    spread_penalty = min(spread_bps / Decimal("100"), Decimal("0.70"))
    imbalance_penalty = (
        min(abs(imbalance) * Decimal("0.20"), Decimal("0.20")) if imbalance is not None else ZERO
    )
    freshness_penalty = Decimal("0.25") if freshness.is_stale else ZERO
    score = ONE - spread_penalty - imbalance_penalty - freshness_penalty
    return max(ZERO, min(ONE, score)).quantize(Decimal("0.0001"))

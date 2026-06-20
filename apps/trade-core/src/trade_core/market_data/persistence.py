"""Persistence adapter for normalized market data aggregates."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from trade_core.market_data.calculators import MarketState
from trade_core.market_data.events import Bar, Candle, OrderBookSnapshot, TradingStatusTick
from trade_core.session.models import SessionEventContext
from trading_common.db.models import (
    MarketCandle,
    MarketMicrostructureSnapshot,
    MarketStatusSnapshot,
    OrderBookSummary,
)
from trading_common.db.repositories import MarketDataRepository


class SqlAlchemyMarketDataStore:
    """Store market data using the shared SQLAlchemy repository layer."""

    def __init__(self, session: Session) -> None:
        self._repository = MarketDataRepository(session)

    def save_candle(
        self,
        *,
        candle: Candle,
        context: SessionEventContext,
    ) -> MarketCandle:
        return self._repository.upsert_candle(
            MarketCandle(
                **context.as_db_values(),
                instrument_id=candle.instrument_id,
                timeframe=candle.timeframe.value,
                open_ts_utc=candle.open_ts_utc.astimezone(UTC),
                close_ts_utc=candle.close_ts_utc.astimezone(UTC),
                exchange_open_ts=candle.exchange_open_ts,
                exchange_close_ts=candle.exchange_close_ts,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
                volume_lots=candle.volume_lots,
                is_closed=candle.is_closed,
                source=candle.source,
                candle_payload=candle.payload,
            )
        )

    def save_bar(
        self,
        *,
        bar: Bar,
        context: SessionEventContext,
    ) -> MarketCandle:
        return self.save_candle(candle=bar.as_candle(), context=context)

    def save_status(
        self,
        *,
        tick: TradingStatusTick,
        context: SessionEventContext,
    ) -> MarketStatusSnapshot:
        return self._repository.save_status_snapshot(
            MarketStatusSnapshot(
                **context.as_db_values(),
                ts_utc=tick.received_ts.astimezone(UTC),
                exchange_ts=tick.exchange_ts,
                received_ts=tick.received_ts,
                instrument_id=tick.instrument_id,
                trading_status=tick.trading_status,
                api_trade_available=tick.api_trade_available,
                status_payload=tick.payload,
            )
        )

    def save_order_book_summary(
        self,
        *,
        order_book: OrderBookSnapshot,
        market_state: MarketState,
        context: SessionEventContext,
    ) -> OrderBookSummary:
        return self._repository.save_order_book_summary(
            OrderBookSummary(
                **context.as_db_values(),
                ts_utc=order_book.received_ts.astimezone(UTC),
                exchange_ts=order_book.exchange_ts,
                received_ts=order_book.received_ts,
                instrument_id=order_book.instrument_id,
                depth_levels=order_book.depth,
                best_bid_price=market_state.best_bid.price if market_state.best_bid else None,
                best_bid_qty_lots=(
                    market_state.best_bid.quantity_lots if market_state.best_bid else None
                ),
                best_ask_price=market_state.best_ask.price if market_state.best_ask else None,
                best_ask_qty_lots=(
                    market_state.best_ask.quantity_lots if market_state.best_ask else None
                ),
                mid_price=market_state.mid_price,
                spread_abs=market_state.spread_abs,
                spread_bps=market_state.spread_bps,
                bid_depth_lots=market_state.bid_depth_lots,
                ask_depth_lots=market_state.ask_depth_lots,
                book_imbalance=market_state.book_imbalance,
                market_quality_score=market_state.market_quality_score,
                summary_payload=order_book.payload,
            )
        )

    def save_microstructure_snapshot(
        self,
        *,
        market_state: MarketState,
        context: SessionEventContext,
        ts_utc: datetime,
        exchange_ts: datetime | None = None,
        received_ts: datetime | None = None,
        source: str = "data_only_shadow",
        payload: dict[str, object] | None = None,
    ) -> MarketMicrostructureSnapshot:
        event_ts = received_ts or ts_utc
        return self._repository.save_microstructure_snapshot(
            MarketMicrostructureSnapshot(
                **context.as_db_values(),
                ts_utc=ts_utc,
                exchange_ts=exchange_ts,
                received_ts=event_ts,
                instrument_id=market_state.instrument_id,
                best_bid=market_state.best_bid.price if market_state.best_bid else None,
                best_ask=market_state.best_ask.price if market_state.best_ask else None,
                mid_price=market_state.mid_price,
                spread_abs=market_state.spread_abs,
                spread_bps=market_state.spread_bps,
                bid_depth_lots=market_state.bid_depth_lots,
                ask_depth_lots=market_state.ask_depth_lots,
                book_imbalance=market_state.book_imbalance,
                market_quality_score=market_state.market_quality_score,
                feed_freshness_age_ms=market_state.feed_freshness.age_ms,
                is_stale=market_state.feed_freshness.is_stale,
                source=source,
                snapshot_payload=payload or market_state.as_read_model(),
            )
        )

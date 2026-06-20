"""Data-only live market collection for spread, depth, and quality calibration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal

from trade_core.market_data.calculators import MarketState
from trade_core.market_data.event_bus import MarketEventBus
from trade_core.market_data.events import (
    Candle,
    MarketDataEvent,
    MarketEventType,
    OrderBookSnapshot,
)
from trade_core.market_data.persistence import SqlAlchemyMarketDataStore
from trade_core.session.models import SessionEventContext
from trading_common import TradingMetrics
from trading_common.telemetry import get_logger, log_event

SessionContextProvider = Callable[[str], SessionEventContext]
LOGGER = get_logger(__name__)


@dataclass(slots=True)
class LiveMarketDataCollectorStats:
    """Small runtime summary for data-only shadow smoke and status checks."""

    candles_received: int = 0
    order_books_received: int = 0
    market_state_snapshots_written: int = 0
    spread_samples: list[Decimal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class LiveMarketDataCollector:
    """Persist live microstructure without producing trading decisions."""

    def __init__(
        self,
        *,
        event_bus: MarketEventBus,
        session_context_provider: SessionContextProvider,
        store: SqlAlchemyMarketDataStore,
        metrics: TradingMetrics | None = None,
        source: str = "data_only_shadow",
    ) -> None:
        self._event_bus = event_bus
        self._session_context_provider = session_context_provider
        self._store = store
        self._metrics = metrics
        self._source = source
        self.stats = LiveMarketDataCollectorStats()

    def register(self) -> None:
        self._event_bus.subscribe(MarketEventType.CANDLE, self._handle_candle)
        self._event_bus.subscribe(MarketEventType.ORDER_BOOK, self._handle_order_book)
        self._event_bus.subscribe(MarketEventType.MARKET_STATE_UPDATED, self._handle_market_state)
        log_event(
            logger=LOGGER,
            event_type="live_data_collector_started",
            component="market_data.live_collector",
            source=self._source,
        )

    async def _handle_candle(self, event: MarketDataEvent) -> None:
        if not isinstance(event.payload, Candle):
            return
        self.stats.candles_received += 1
        log_event(
            logger=LOGGER,
            event_type="live_candle_received",
            component="market_data.live_collector",
            instrument_id=event.payload.instrument_id,
            timeframe=event.payload.timeframe.value,
            is_closed=event.payload.is_closed,
            source=self._source,
        )

    async def _handle_order_book(self, event: MarketDataEvent) -> None:
        if not isinstance(event.payload, OrderBookSnapshot):
            return
        self.stats.order_books_received += 1
        if self._metrics is not None:
            self._metrics.inc_order_book_snapshot(instrument_id=event.payload.instrument_id)
        log_event(
            logger=LOGGER,
            event_type="live_order_book_snapshot_written",
            component="market_data.live_collector",
            instrument_id=event.payload.instrument_id,
            depth=event.payload.depth,
            source=self._source,
        )

    async def _handle_market_state(self, event: MarketDataEvent) -> None:
        if not isinstance(event.payload, MarketState):
            return
        market_state = event.payload
        try:
            context = self._session_context_provider(market_state.instrument_id)
            snapshot = self._store.save_microstructure_snapshot(
                market_state=market_state,
                context=context,
                ts_utc=event.ts_utc.astimezone(UTC),
                received_ts=event.ts_utc.astimezone(UTC),
                source=self._source,
                payload={
                    **market_state.as_read_model(),
                    "source": self._source,
                    "event_type": event.event_type.value,
                },
            )
            self.stats.market_state_snapshots_written += 1
            if market_state.spread_bps is not None:
                self.stats.spread_samples.append(market_state.spread_bps)
            if self._metrics is not None:
                self._metrics.inc_market_microstructure_snapshot(
                    instrument_id=market_state.instrument_id,
                )
            log_event(
                logger=LOGGER,
                event_type="live_market_snapshot_written",
                component="market_data.live_collector",
                instrument_id=market_state.instrument_id,
                snapshot_id=str(snapshot.snapshot_id),
                spread_bps=str(market_state.spread_bps)
                if market_state.spread_bps is not None
                else None,
                source=self._source,
            )
        except Exception as exc:
            self.stats.errors.append(type(exc).__name__)
            if self._metrics is not None:
                self._metrics.inc_market_microstructure_snapshot(
                    instrument_id=market_state.instrument_id,
                    status="error",
                )
            log_event(
                logger=LOGGER,
                level="ERROR",
                event_type="live_market_snapshot_write_failed",
                component="market_data.live_collector",
                instrument_id=market_state.instrument_id,
                error_code=type(exc).__name__,
                source=self._source,
            )
            raise

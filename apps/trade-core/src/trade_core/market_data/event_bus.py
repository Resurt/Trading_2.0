"""Small in-process event bus for trade-core market data."""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable

from trade_core.market_data.events import MarketDataEvent, MarketEventType

MarketEventHandler = Callable[[MarketDataEvent], Awaitable[None] | None]


class MarketEventBus:
    """Async publish/subscribe bus scoped to the trade-core process."""

    def __init__(self) -> None:
        self._subscribers: dict[MarketEventType, list[MarketEventHandler]] = defaultdict(list)
        self.published_events: list[MarketDataEvent] = []

    def subscribe(
        self,
        event_type: MarketEventType,
        handler: MarketEventHandler,
    ) -> None:
        self._subscribers[event_type].append(handler)

    async def publish(self, event: MarketDataEvent) -> None:
        self.published_events.append(event)
        for handler in self._subscribers.get(event.event_type, []):
            result = handler(event)
            if inspect.isawaitable(result):
                await result

    def subscribers_for(self, event_type: MarketEventType) -> int:
        return len(self._subscribers.get(event_type, []))

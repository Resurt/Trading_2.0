"""Stream ping monitoring, reconnect, and gap recovery hooks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trade_core.broker_gateway import StreamEvent
from trade_core.infra.tbank.retry import ExponentialBackoff


@dataclass(slots=True)
class PingMonitor:
    """Tracks stream activity and marks streams stale when pings stop."""

    timeout_seconds: float
    last_message_at: datetime | None = None

    def observe(self, event: StreamEvent) -> None:
        self.last_message_at = event.received_at or datetime.now(UTC)

    def is_stale(self, now: datetime | None = None) -> bool:
        if self.last_message_at is None:
            return False
        current = now or datetime.now(UTC)
        return current - self.last_message_at > timedelta(seconds=self.timeout_seconds)


class StreamSupervisor:
    """Reconnects streams with exponential backoff and runs gap recovery hooks."""

    def __init__(
        self,
        *,
        backoff: ExponentialBackoff,
        ping_timeout_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._backoff = backoff
        self._ping_timeout_seconds = ping_timeout_seconds
        self._sleep = sleep

    async def run(
        self,
        *,
        stream_name: str,
        stream_factory: Callable[[], AsyncIterator[StreamEvent]],
        gap_recovery_hook: Callable[[str], Awaitable[None]],
    ) -> AsyncIterator[StreamEvent]:
        attempt = 1
        while True:
            await self._sleep(self._backoff.delay_for_attempt(attempt))
            monitor = PingMonitor(timeout_seconds=self._ping_timeout_seconds)
            try:
                async for event in stream_factory():
                    monitor.observe(event)
                    if monitor.is_stale():
                        raise TimeoutError(f"{stream_name} stream ping timeout")
                    attempt = 1
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                await gap_recovery_hook(stream_name)
                attempt += 1

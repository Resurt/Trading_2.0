"""Retry and exponential backoff helpers for broker calls and streams."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from trade_core.infra.tbank.errors import BrokerGatewayError


@dataclass(frozen=True, slots=True)
class ExponentialBackoff:
    initial_seconds: float = 1.0
    multiplier: float = 2.0
    max_seconds: float = 60.0

    def delay_for_attempt(self, attempt: int) -> float:
        if attempt <= 1:
            return 0.0
        return min(self.initial_seconds * (self.multiplier ** (attempt - 2)), self.max_seconds)


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    backoff: ExponentialBackoff,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run an async operation with retry for mapped retryable broker errors."""

    if max_attempts < 1:
        msg = "max_attempts must be >= 1"
        raise ValueError(msg)

    last_error: BrokerGatewayError | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            await sleep(backoff.delay_for_attempt(attempt))
        try:
            return await operation()
        except BrokerGatewayError as exc:
            last_error = exc
            if not exc.retryable or attempt == max_attempts:
                raise
    if last_error is not None:
        raise last_error
    msg = "retry loop exited without executing operation"
    raise RuntimeError(msg)

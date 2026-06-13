"""Protocols implemented by concrete T-Bank SDK wrappers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from trade_core.broker_gateway import StreamEvent

JsonPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class UnaryCallResult:
    """Raw result returned by a concrete SDK wrapper."""

    data: JsonPayload
    headers: Mapping[str, object] = field(default_factory=dict)


class TBankUnaryClient(Protocol):
    async def call_unary(
        self,
        method_name: str,
        payload: JsonPayload,
        *,
        metadata: tuple[tuple[str, str], ...],
        timeout_seconds: float,
    ) -> UnaryCallResult: ...


class TBankStreamClient(Protocol):
    def open_market_data_stream(
        self,
        stream_name: str,
        *,
        metadata: tuple[tuple[str, str], ...],
        ping_interval_seconds: float,
    ) -> AsyncIterator[StreamEvent]: ...

    def open_order_state_stream(
        self,
        account_id: str,
        *,
        metadata: tuple[tuple[str, str], ...],
        ping_interval_seconds: float,
    ) -> AsyncIterator[StreamEvent]: ...

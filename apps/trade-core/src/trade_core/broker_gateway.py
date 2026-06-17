"""Broker gateway boundary used by trade-core without SDK-specific types."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

JsonPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """Stable instrument reference used across trade-core."""

    instrument_id: str
    instrument_uid: str | None = None
    class_code: str | None = None
    ticker: str | None = None


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    """Metadata attached to broker requests for audit and diagnostics."""

    account_id: str | None = None
    request_id: UUID | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerUnaryResponse:
    """SDK-neutral unary response envelope."""

    method_name: str
    data: JsonPayload
    headers: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandleRequest:
    instrument: InstrumentRef
    interval: str
    from_: datetime
    to: datetime


@dataclass(frozen=True, slots=True)
class LastPricesRequest:
    instruments: tuple[InstrumentRef, ...]


@dataclass(frozen=True, slots=True)
class OrderBookRequest:
    instrument: InstrumentRef
    depth: int


@dataclass(frozen=True, slots=True)
class TradingStatusRequest:
    instrument: InstrumentRef


@dataclass(frozen=True, slots=True)
class TradingSchedulesRequest:
    exchange: str
    from_: datetime
    to: datetime


@dataclass(frozen=True, slots=True)
class InstrumentResolveRequest:
    tickers: tuple[str, ...]
    class_code: str = "TQBR"


@dataclass(frozen=True, slots=True)
class OrderPlacementRequest:
    account_id: str
    instrument: InstrumentRef
    side: str
    order_type: str
    lot_qty: int
    price: Decimal | None
    time_in_force: str
    client_order_key: str | None = None
    request_order_id: UUID | None = None
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    account_id: str
    request_order_id: UUID | None = None
    exchange_order_id: str | None = None
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderStateRequest:
    account_id: str
    request_order_id: UUID | None = None
    exchange_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class OrdersRequest:
    account_id: str


@dataclass(frozen=True, slots=True)
class PortfolioRequest:
    account_id: str


@dataclass(frozen=True, slots=True)
class PositionsRequest:
    account_id: str


@dataclass(frozen=True, slots=True)
class AccountsRequest:
    pass


@dataclass(frozen=True, slots=True)
class StopOrderPlacementRequest:
    account_id: str
    instrument: InstrumentRef
    side: str
    stop_order_type: str
    lot_qty: int
    stop_price: Decimal
    price: Decimal | None
    expiration_type: str
    expire_date: datetime | None = None
    client_order_key: str | None = None
    request_order_id: UUID | None = None
    payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """SDK-neutral event from a broker stream."""

    stream_name: str
    event_type: str
    payload: JsonPayload
    tracking_id: str | None = None
    received_at: datetime | None = None


class BrokerGateway(Protocol):
    """Public broker boundary consumed by trade-core engines."""

    async def trading_schedules(
        self,
        request: TradingSchedulesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def resolve_instruments(
        self,
        request: InstrumentResolveRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_trading_status(
        self,
        request: TradingStatusRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_candles(
        self,
        request: CandleRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_last_prices(
        self,
        request: LastPricesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_order_book(
        self,
        request: OrderBookRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def post_order(
        self,
        request: OrderPlacementRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def cancel_order(
        self,
        request: CancelOrderRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_order_state(
        self,
        request: OrderStateRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_orders(
        self,
        request: OrdersRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def post_stop_order(
        self,
        request: StopOrderPlacementRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def reconcile_order_state(
        self,
        request: OrderStateRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def reconcile_open_orders(
        self,
        request: OrdersRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_portfolio(
        self,
        request: PortfolioRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_positions(
        self,
        request: PositionsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    async def get_accounts(
        self,
        request: AccountsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse: ...

    def stream_market_data(self, stream_name: str) -> AsyncIterator[StreamEvent]: ...

    def stream_orders(self, account_id: str) -> AsyncIterator[StreamEvent]: ...

    async def recover_after_stream_gap(
        self,
        stream_name: str,
        account_id: str | None = None,
    ) -> None: ...

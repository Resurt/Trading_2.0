"""T-Bank BrokerGateway implementation with SDK-neutral public methods."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from trade_core.broker_gateway import (
    BrokerUnaryResponse,
    CancelOrderRequest,
    CandleRequest,
    InstrumentRef,
    LastPricesRequest,
    OrderBookRequest,
    OrderPlacementRequest,
    OrdersRequest,
    OrderStateRequest,
    RequestMetadata,
    StopOrderPlacementRequest,
    StreamEvent,
    TradingSchedulesRequest,
    TradingStatusRequest,
)
from trade_core.infra.tbank.config import TBankBrokerConfig
from trade_core.infra.tbank.deadlines import deadline_for
from trade_core.infra.tbank.errors import BrokerGatewayError, map_exception
from trade_core.infra.tbank.headers import auth_metadata, capture_response_headers
from trade_core.infra.tbank.idempotency import OrderIdempotencyStore
from trade_core.infra.tbank.protocols import TBankStreamClient, TBankUnaryClient, UnaryCallResult
from trade_core.infra.tbank.retry import ExponentialBackoff, retry_async
from trade_core.infra.tbank.sdk_clients import TBankSdkStreamClient, TBankSdkUnaryClient
from trade_core.infra.tbank.secrets import TBankTokenBundle, load_tbank_tokens
from trade_core.infra.tbank.streams import StreamSupervisor
from trading_common.observability import DomainEventType
from trading_common.telemetry import get_logger, log_event

JsonPayload = dict[str, Any]
LOGGER = get_logger(__name__)


class TBankBrokerGateway:
    """Adapter that isolates trade-core from T-Invest SDK/protobuf details."""

    def __init__(
        self,
        *,
        config: TBankBrokerConfig | None = None,
        tokens: TBankTokenBundle | None = None,
        unary_client: TBankUnaryClient | None = None,
        stream_client: TBankStreamClient | None = None,
        idempotency_store: OrderIdempotencyStore | None = None,
        backoff: ExponentialBackoff | None = None,
    ) -> None:
        self.config = config or TBankBrokerConfig.from_env()
        self.tokens = tokens or load_tbank_tokens()
        self._unary_client = unary_client or TBankSdkUnaryClient(config=self.config)
        self._stream_client = stream_client or TBankSdkStreamClient(config=self.config)
        self._idempotency_store = idempotency_store or OrderIdempotencyStore()
        self._backoff = backoff or ExponentialBackoff(
            initial_seconds=self.config.backoff_initial_seconds,
            multiplier=self.config.backoff_multiplier,
            max_seconds=self.config.backoff_max_seconds,
        )
        self._stream_supervisor = StreamSupervisor(
            backoff=self._backoff,
            ping_timeout_seconds=self.config.stream_ping_timeout_seconds,
        )

    async def trading_schedules(
        self,
        request: TradingSchedulesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "TradingSchedules",
            {
                "exchange": request.exchange,
                "from": _datetime_to_iso(request.from_),
                "to": _datetime_to_iso(request.to),
            },
            metadata,
        )

    async def get_trading_status(
        self,
        request: TradingStatusRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "GetTradingStatus",
            {"instrument": _instrument_payload(request.instrument)},
            metadata,
        )

    async def get_candles(
        self,
        request: CandleRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "GetCandles",
            {
                "instrument": _instrument_payload(request.instrument),
                "interval": request.interval,
                "from": _datetime_to_iso(request.from_),
                "to": _datetime_to_iso(request.to),
            },
            metadata,
        )

    async def get_last_prices(
        self,
        request: LastPricesRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "GetLastPrices",
            {
                "instruments": [
                    _instrument_payload(instrument)
                    for instrument in request.instruments
                ]
            },
            metadata,
        )

    async def get_order_book(
        self,
        request: OrderBookRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "GetOrderBook",
            {
                "instrument": _instrument_payload(request.instrument),
                "depth": request.depth,
            },
            metadata,
        )

    async def post_order(
        self,
        request: OrderPlacementRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        request_order_id = self._request_order_id_for_order(request)
        payload = {
            "account_id": request.account_id,
            "instrument": _instrument_payload(request.instrument),
            "side": request.side,
            "order_type": request.order_type,
            "lot_qty": request.lot_qty,
            "price": _decimal_to_str(request.price),
            "time_in_force": request.time_in_force,
            "request_order_id": str(request_order_id),
            "order_id": str(request_order_id),
            "payload": request.payload,
        }
        return await self._call_trading("PostOrder", payload, metadata)

    async def cancel_order(
        self,
        request: CancelOrderRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        payload = {
            "account_id": request.account_id,
            "request_order_id": _uuid_to_str(request.request_order_id),
            "exchange_order_id": request.exchange_order_id,
            "payload": request.payload,
        }
        return await self._call_trading("CancelOrder", payload, metadata)

    async def get_order_state(
        self,
        request: OrderStateRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        payload = {
            "account_id": request.account_id,
            "request_order_id": _uuid_to_str(request.request_order_id),
            "exchange_order_id": request.exchange_order_id,
        }
        return await self._call_readonly("GetOrderState", payload, metadata)

    async def get_orders(
        self,
        request: OrdersRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly("GetOrders", {"account_id": request.account_id}, metadata)

    async def post_stop_order(
        self,
        request: StopOrderPlacementRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        request_order_id = self._request_order_id_for_stop_order(request)
        payload = {
            "account_id": request.account_id,
            "instrument": _instrument_payload(request.instrument),
            "side": request.side,
            "stop_order_type": request.stop_order_type,
            "lot_qty": request.lot_qty,
            "stop_price": _decimal_to_str(request.stop_price),
            "price": _decimal_to_str(request.price),
            "expiration_type": request.expiration_type,
            "expire_date": _datetime_to_iso(request.expire_date),
            "request_order_id": str(request_order_id),
            "order_id": str(request_order_id),
            "payload": request.payload,
        }
        return await self._call_trading("PostStopOrder", payload, metadata)

    async def reconcile_order_state(
        self,
        request: OrderStateRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self.get_order_state(request, metadata)

    async def reconcile_open_orders(
        self,
        request: OrdersRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self.get_orders(request, metadata)

    def stream_market_data(self, stream_name: str) -> AsyncIterator[StreamEvent]:
        async def recover_gap(name: str) -> None:
            await self.recover_after_stream_gap(name)

        return self._stream_supervisor.run(
            stream_name=stream_name,
            stream_factory=lambda: self._open_market_stream(stream_name),
            gap_recovery_hook=recover_gap,
        )

    def stream_orders(self, account_id: str) -> AsyncIterator[StreamEvent]:
        stream_name = "OrderStateStream"

        async def recover_gap(name: str) -> None:
            await self.recover_after_stream_gap(name)

        return self._stream_supervisor.run(
            stream_name=stream_name,
            stream_factory=lambda: self._open_order_stream(account_id),
            gap_recovery_hook=recover_gap,
        )

    async def recover_after_stream_gap(self, stream_name: str) -> None:
        log_event(
            logger=LOGGER,
            level="WARNING",
            event_type=DomainEventType.STREAM_GAP_RECOVERY_REQUESTED.value,
            component="tbank.gateway",
            stream_name=stream_name,
            target=self.config.target,
        )

    async def _call_readonly(
        self,
        method_name: str,
        payload: JsonPayload,
        request_metadata: RequestMetadata | None,
    ) -> BrokerUnaryResponse:
        return await self._call_unary(
            method_name,
            payload,
            token=self.tokens.token_for_readonly(),
            request_metadata=request_metadata,
        )

    async def _call_trading(
        self,
        method_name: str,
        payload: JsonPayload,
        request_metadata: RequestMetadata | None,
    ) -> BrokerUnaryResponse:
        return await self._call_unary(
            method_name,
            payload,
            token=self.tokens.token_for_trading(),
            request_metadata=request_metadata,
        )

    async def _call_unary(
        self,
        method_name: str,
        payload: JsonPayload,
        *,
        token: str,
        request_metadata: RequestMetadata | None,
    ) -> BrokerUnaryResponse:
        if self._unary_client is None:
            msg = "TBankUnaryClient is not configured."
            raise RuntimeError(msg)
        unary_client = self._unary_client

        deadline = deadline_for(method_name)
        outbound_metadata = auth_metadata(token, self.config.app_name)
        if request_metadata is not None:
            payload = {
                **payload,
                "request_metadata": {
                    "account_id": request_metadata.account_id,
                    "request_id": _uuid_to_str(request_metadata.request_id),
                    "correlation_id": request_metadata.correlation_id,
                },
            }

        async def operation() -> BrokerUnaryResponse:
            try:
                result = await unary_client.call_unary(
                    method_name,
                    payload,
                    metadata=outbound_metadata,
                    timeout_seconds=deadline.seconds,
                )
            except BrokerGatewayError:
                raise
            except Exception as exc:
                raise map_exception(
                    exc,
                    method_name=method_name,
                    headers=capture_response_headers(_headers_from_exception(exc)),
                ) from exc
            return self._build_response(method_name, result)

        return await retry_async(
            operation,
            max_attempts=self.config.max_retry_attempts,
            backoff=self._backoff,
        )

    def _build_response(self, method_name: str, result: UnaryCallResult) -> BrokerUnaryResponse:
        response_headers = capture_response_headers(result.headers)
        log_event(
            logger=LOGGER,
            event_type="tbank_unary_response_headers",
            component="tbank.gateway",
            tracking_id=response_headers.tracking_id,
            method_name=method_name,
            headers=response_headers.as_log_context(),
        )
        return BrokerUnaryResponse(
            method_name=method_name,
            data=result.data,
            headers=response_headers.as_log_context(),
        )

    def _request_order_id_for_order(self, request: OrderPlacementRequest) -> UUID:
        key = request.client_order_key
        if request.request_order_id is not None:
            if key is not None:
                return self._idempotency_store.remember(key, request.request_order_id)
            return request.request_order_id
        return self._idempotency_store.get_or_create(key)

    def _request_order_id_for_stop_order(self, request: StopOrderPlacementRequest) -> UUID:
        key = request.client_order_key
        if request.request_order_id is not None:
            if key is not None:
                return self._idempotency_store.remember(key, request.request_order_id)
            return request.request_order_id
        return self._idempotency_store.get_or_create(key)

    def _open_market_stream(self, stream_name: str) -> AsyncIterator[StreamEvent]:
        if self._stream_client is None:
            msg = "TBankStreamClient is not configured."
            raise RuntimeError(msg)
        return self._stream_client.open_market_data_stream(
            stream_name,
            metadata=auth_metadata(self.tokens.token_for_readonly(), self.config.app_name),
            ping_interval_seconds=self.config.stream_ping_interval_seconds,
        )

    def _open_order_stream(self, account_id: str) -> AsyncIterator[StreamEvent]:
        if self._stream_client is None:
            msg = "TBankStreamClient is not configured."
            raise RuntimeError(msg)
        return self._stream_client.open_order_state_stream(
            account_id,
            metadata=auth_metadata(self.tokens.token_for_readonly(), self.config.app_name),
            ping_interval_seconds=self.config.stream_ping_interval_seconds,
        )


def _instrument_payload(instrument: InstrumentRef) -> JsonPayload:
    return {
        "instrument_id": instrument.instrument_id,
        "instrument_uid": instrument.instrument_uid,
        "class_code": instrument.class_code,
        "ticker": instrument.ticker,
    }


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _uuid_to_str(value: UUID | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _headers_from_exception(exc: Exception) -> Mapping[str, object] | None:
    headers = getattr(exc, "headers", None) or getattr(exc, "metadata", None)
    if isinstance(headers, Mapping):
        return headers
    if isinstance(headers, Sequence) and not isinstance(headers, str | bytes):
        normalized: dict[str, object] = {}
        for item in headers:
            if isinstance(item, tuple) and len(item) == 2:
                normalized[str(item[0])] = item[1]
        return normalized
    return None

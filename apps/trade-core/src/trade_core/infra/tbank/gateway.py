"""T-Bank BrokerGateway implementation with SDK-neutral public methods."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from trade_core.broker_gateway import (
    AccountsRequest,
    BrokerUnaryResponse,
    CancelOrderRequest,
    CandleRequest,
    DividendsRequest,
    InstrumentRef,
    InstrumentResolveRequest,
    LastPricesRequest,
    OrderBookRequest,
    OrderPlacementRequest,
    OrdersRequest,
    OrderStateRequest,
    PortfolioRequest,
    PositionsRequest,
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
StreamGapRecoveryHook = Callable[[str, str | None], Awaitable[None]]
LOGGER = get_logger(__name__)
DEFAULT_GAP_RECOVERY_INSTRUMENTS_ENV = "TBANK_STREAM_INSTRUMENT_IDS"
GAP_RECOVERY_TIMEFRAMES_ENV = "TBANK_GAP_RECOVERY_TIMEFRAMES"
GAP_RECOVERY_LOOKBACK_MINUTES_ENV = "TBANK_GAP_RECOVERY_LOOKBACK_MINUTES"


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
        self._gap_recovery_hook: StreamGapRecoveryHook | None = None

    def set_stream_gap_recovery_hook(self, hook: StreamGapRecoveryHook | None) -> None:
        """Delegate stream gap recovery to trade-core runtime when it is available."""

        self._gap_recovery_hook = hook

    def set_market_stream_instruments(self, instruments: tuple[InstrumentRef, ...]) -> None:
        """Use resolved broker IDs for all subsequent market stream subscriptions."""

        stream_ids = tuple(
            instrument.instrument_uid or instrument.instrument_id for instrument in instruments
        )
        self._stream_client = TBankSdkStreamClient(config=self.config, instruments=stream_ids)

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

    async def get_dividends(
        self,
        request: DividendsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "GetDividends",
            {
                "instrument": _instrument_payload(request.instrument),
                "from": _datetime_to_iso(request.from_),
                "to": _datetime_to_iso(request.to),
                "date_filter": request.date_filter,
            },
            metadata,
        )

    async def resolve_instruments(
        self,
        request: InstrumentResolveRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "ResolveInstruments",
            {
                "tickers": list(request.tickers),
                "class_code": request.class_code,
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

    async def get_portfolio(
        self,
        request: PortfolioRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "GetPortfolio",
            {"account_id": request.account_id},
            metadata,
        )

    async def get_positions(
        self,
        request: PositionsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        return await self._call_readonly(
            "GetPositions",
            {"account_id": request.account_id},
            metadata,
        )

    async def get_accounts(
        self,
        request: AccountsRequest,
        metadata: RequestMetadata | None = None,
    ) -> BrokerUnaryResponse:
        del request
        return await self._call_readonly("GetAccounts", {}, metadata)

    def stream_market_data(self, stream_name: str) -> AsyncIterator[StreamEvent]:
        async def recover_gap(name: str) -> None:
            await self._recover_stream_gap(name)

        return self._stream_supervisor.run(
            stream_name=stream_name,
            stream_factory=lambda: self._open_market_stream(stream_name),
            gap_recovery_hook=recover_gap,
        )

    def stream_orders(self, account_id: str) -> AsyncIterator[StreamEvent]:
        stream_name = "OrderStateStream"

        async def recover_gap(name: str) -> None:
            await self._recover_stream_gap(name, account_id=account_id)

        return self._stream_supervisor.run(
            stream_name=stream_name,
            stream_factory=lambda: self._open_order_stream(account_id),
            gap_recovery_hook=recover_gap,
        )

    async def _recover_stream_gap(
        self,
        stream_name: str,
        account_id: str | None = None,
    ) -> None:
        if self._gap_recovery_hook is not None:
            await self._gap_recovery_hook(stream_name, account_id)
            return
        await self.recover_after_stream_gap(stream_name, account_id=account_id)

    async def recover_after_stream_gap(
        self,
        stream_name: str,
        account_id: str | None = None,
    ) -> None:
        log_event(
            logger=LOGGER,
            level="WARNING",
            event_type=DomainEventType.STREAM_GAP_RECOVERY_REQUESTED.value,
            component="tbank.gateway",
            stream_name=stream_name,
            target=self.config.target,
            account_id_present=account_id is not None,
        )
        recovered_candles = 0
        open_orders_refreshed = False
        order_states_refreshed = 0
        try:
            if _stream_needs_candle_backfill(stream_name):
                recovered_candles = await self._recover_recent_candles_after_gap()
            if account_id is not None:
                await self.reconcile_open_orders(OrdersRequest(account_id=account_id))
                open_orders_refreshed = True
                for request_order_id in self._idempotency_store.request_order_ids():
                    await self.reconcile_order_state(
                        OrderStateRequest(
                            account_id=account_id,
                            request_order_id=request_order_id,
                        )
                    )
                    order_states_refreshed += 1
        except Exception as exc:
            log_event(
                logger=LOGGER,
                level="WARNING",
                event_type="stream_gap_recovery_failed",
                component="tbank.gateway",
                stream_name=stream_name,
                error_message=str(exc),
            )
            return
        log_event(
            logger=LOGGER,
            event_type=DomainEventType.STREAM_GAP_RECOVERY_COMPLETED.value,
            component="tbank.gateway",
            stream_name=stream_name,
            recovered_candles=recovered_candles,
            open_orders_refreshed=open_orders_refreshed,
            order_states_refreshed=order_states_refreshed,
        )

    async def _recover_recent_candles_after_gap(self) -> int:
        now = datetime.now(tz=UTC)
        lookback_minutes = int(os.getenv(GAP_RECOVERY_LOOKBACK_MINUTES_ENV, "30"))
        from_ts = now - timedelta(minutes=lookback_minutes)
        recovered = 0
        for instrument in _gap_recovery_instruments():
            for timeframe in _gap_recovery_timeframes():
                response = await self.get_candles(
                    CandleRequest(
                        instrument=instrument,
                        interval=timeframe,
                        from_=from_ts,
                        to=now,
                    )
                )
                candles = response.data.get("candles", ())
                if isinstance(candles, list | tuple):
                    recovered += len(candles)
        return recovered

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
                    timeout_seconds=max(
                        deadline.seconds,
                        self.config.unary_timeout_floor_seconds,
                    ),
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


def _stream_needs_candle_backfill(stream_name: str) -> bool:
    normalized = stream_name.lower()
    return (
        "candle" in normalized
        or "last" in normalized
        or "order_book" in normalized
        or "book" in normalized
        or "trade" in normalized
        or "status" in normalized
        or "info" in normalized
    )


def _gap_recovery_instruments() -> tuple[InstrumentRef, ...]:
    raw = os.getenv(DEFAULT_GAP_RECOVERY_INSTRUMENTS_ENV, "MOEX:SBER")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return tuple(InstrumentRef(instrument_id=value) for value in values)


def _gap_recovery_timeframes() -> tuple[str, ...]:
    raw = os.getenv(GAP_RECOVERY_TIMEFRAMES_ENV, "1m,5m,10m,15m")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or ("1m",)

"""Concrete T-Bank Python SDK clients behind the SDK-neutral gateway protocols."""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import TracebackType
from typing import Any, Protocol

from trade_core.broker_gateway import StreamEvent
from trade_core.infra.tbank.config import TBankBrokerConfig
from trade_core.infra.tbank.headers import (
    HEADER_APP_NAME,
    HEADER_TRACKING_ID,
)
from trade_core.infra.tbank.protocols import JsonPayload, UnaryCallResult

SDK_PACKAGE_NAME = "t_tech.invest"
DEFAULT_STREAM_INSTRUMENTS_ENV = "TBANK_STREAM_INSTRUMENT_IDS"
NANO = Decimal("1000000000")


class TBankSdkNotInstalledError(RuntimeError):
    """Raised when concrete SDK clients are used without the optional SDK extra."""


class ServicesContext(Protocol):
    def __enter__(self) -> Any: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


ServicesFactory = Callable[[str, str, str], ServicesContext]


@dataclass(frozen=True, slots=True)
class SdkRuntime:
    """Loaded SDK module plus a services context factory."""

    sdk: Any
    services_factory: ServicesFactory


class TBankSdkUnaryClient:
    """Unary client implemented with the official T-Bank Python SDK."""

    def __init__(
        self,
        *,
        config: TBankBrokerConfig,
        sdk_module: Any | None = None,
        services_factory: ServicesFactory | None = None,
    ) -> None:
        self._config = config
        self._sdk_module = sdk_module
        self._services_factory = services_factory

    async def call_unary(
        self,
        method_name: str,
        payload: JsonPayload,
        *,
        metadata: tuple[tuple[str, str], ...],
        timeout_seconds: float,
    ) -> UnaryCallResult:
        return await asyncio.wait_for(
            asyncio.to_thread(self._call_unary_sync, method_name, payload, metadata),
            timeout=timeout_seconds,
        )

    def _call_unary_sync(
        self,
        method_name: str,
        payload: JsonPayload,
        metadata: tuple[tuple[str, str], ...],
    ) -> UnaryCallResult:
        runtime = self._runtime()
        token = _token_from_metadata(metadata)
        app_name = _metadata_value(metadata, HEADER_APP_NAME) or self._config.app_name
        with runtime.services_factory(token, self._config.target, app_name) as services:
            response = _call_sdk_method(runtime.sdk, services, method_name, payload)
        return UnaryCallResult(
            data=normalize_sdk_response(method_name, response, request_payload=payload),
            headers=headers_from_sdk_response(response),
        )

    def _runtime(self) -> SdkRuntime:
        sdk = self._sdk_module or load_tbank_sdk()
        factory = self._services_factory or _real_services_factory(sdk)
        return SdkRuntime(sdk=sdk, services_factory=factory)


class TBankSdkStreamClient:
    """Stream client implemented with the official T-Bank Python SDK."""

    def __init__(
        self,
        *,
        config: TBankBrokerConfig,
        instruments: tuple[str, ...] | None = None,
        depth: int = 20,
        sdk_module: Any | None = None,
        services_factory: ServicesFactory | None = None,
    ) -> None:
        self._config = config
        self._instruments = instruments or _stream_instruments_from_env()
        self._depth = depth
        self._sdk_module = sdk_module
        self._services_factory = services_factory

    async def open_market_data_stream(
        self,
        stream_name: str,
        *,
        metadata: tuple[tuple[str, str], ...],
        ping_interval_seconds: float,
    ) -> AsyncIterator[StreamEvent]:
        runtime = self._runtime()
        token = _token_from_metadata(metadata)
        app_name = _metadata_value(metadata, HEADER_APP_NAME) or self._config.app_name
        with runtime.services_factory(token, self._config.target, app_name) as services:
            request_iterator = iter(
                _market_stream_requests(
                    runtime.sdk,
                    stream_name=stream_name,
                    instruments=self._instruments,
                    depth=self._depth,
                    ping_interval_seconds=ping_interval_seconds,
                )
            )
            response_iterator = services.market_data_stream.market_data_stream(request_iterator)
            while True:
                response = await asyncio.to_thread(_next_or_none, response_iterator)
                if response is None:
                    return
                for event in stream_events_from_sdk_response(stream_name, response):
                    yield event

    async def open_order_state_stream(
        self,
        account_id: str,
        *,
        metadata: tuple[tuple[str, str], ...],
        ping_interval_seconds: float,
    ) -> AsyncIterator[StreamEvent]:
        runtime = self._runtime()
        token = _token_from_metadata(metadata)
        app_name = _metadata_value(metadata, HEADER_APP_NAME) or self._config.app_name
        with runtime.services_factory(token, self._config.target, app_name) as services:
            request = _sdk_type(runtime.sdk, "OrderStateStreamRequest")(
                accounts=[account_id],
                ping_delay_millis=int(ping_interval_seconds * 1000),
            )
            response_iterator = services.orders_stream.order_state_stream(request=request)
            while True:
                response = await asyncio.to_thread(_next_or_none, response_iterator)
                if response is None:
                    return
                for event in stream_events_from_sdk_response("OrderStateStream", response):
                    yield event

    def _runtime(self) -> SdkRuntime:
        sdk = self._sdk_module or load_tbank_sdk()
        factory = self._services_factory or _real_services_factory(sdk)
        return SdkRuntime(sdk=sdk, services_factory=factory)


def load_tbank_sdk() -> Any:
    """Import the current T-Bank SDK lazily so default CI does not need the extra."""

    try:
        return importlib.import_module(SDK_PACKAGE_NAME)
    except ModuleNotFoundError as exc:
        msg = (
            "T-Bank SDK is not installed. Install optional extra with: "
            "python -m pip install -e .[tbank] --extra-index-url "
            "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
        )
        raise TBankSdkNotInstalledError(msg) from exc


def normalize_sdk_response(
    method_name: str,
    response: Any,
    *,
    request_payload: Mapping[str, object] | None = None,
) -> JsonPayload:
    """Convert SDK dataclasses/protobuf wrappers into SDK-neutral payloads."""

    request_payload = request_payload or {}
    if method_name == "TradingSchedules":
        return _trading_schedules_payload(response)
    if method_name == "ResolveInstruments":
        if isinstance(response, Mapping):
            return {str(key): value for key, value in response.items()}
        return {
            "instruments": [
                _instrument_catalog_payload(instrument)
                for instrument in _list_attr(response, "instruments")
            ]
        }
    if method_name == "GetTradingStatus":
        return _trading_status_payload(response, request_payload=request_payload)
    if method_name == "GetCandles":
        return {
            "candles": [
                _historic_candle_payload(candle, request_payload)
                for candle in _list_attr(response, "candles")
            ]
        }
    if method_name == "GetDividends":
        return {
            "instrument_id": _instrument_id_from_request_payload(request_payload),
            "dividends": [
                _dividend_payload(dividend, request_payload)
                for dividend in _list_attr(response, "dividends")
            ],
        }
    if method_name == "GetLastPrices":
        return {
            "prices": [
                _last_price_payload(item)
                for item in _list_attr(response, "last_prices")
            ]
        }
    if method_name == "GetLastTrades":
        return {
            "trades": [
                _market_trade_payload(item)
                for item in _list_attr(response, "trades")
            ]
        }
    if method_name == "GetOrderBook":
        return _order_book_payload(response)
    if method_name == "PostOrder":
        return _order_state_payload(response, default_status="posted")
    if method_name == "CancelOrder":
        return {
            "broker_status": "cancelled",
            "cancelled_at": _iso_or_none(_attr(response, "time")),
        }
    if method_name == "GetOrderState":
        return _order_state_payload(response, default_status="observed")
    if method_name == "GetOrders":
        return {
            "orders": [
                _order_state_payload(order, default_status="observed")
                for order in _list_attr(response, "orders")
            ]
        }
    if method_name == "GetPortfolio":
        return _portfolio_payload(response, request_payload=request_payload)
    if method_name == "GetPositions":
        return _positions_payload(response, request_payload=request_payload)
    if method_name == "GetAccounts":
        return {
            "accounts": [_account_payload(account) for account in _list_attr(response, "accounts")]
        }
    if method_name == "PostStopOrder":
        return {
            "exchange_order_id": _str_or_none(_attr(response, "stop_order_id")),
            "order_id": _str_or_none(_attr(response, "stop_order_id")),
            "request_order_id": _str_or_none(_attr(response, "order_request_id")),
            "broker_status": "posted",
        }
    return {"raw": _dataclass_payload(response)}


def headers_from_sdk_response(response: Any) -> dict[str, object]:
    """Extract support diagnostics exposed by SDK response metadata."""

    metadata = _attr(response, "response_metadata")
    tracking_id = _str_or_none(_attr(metadata, "tracking_id"))
    headers: dict[str, object] = {}
    if tracking_id:
        headers[HEADER_TRACKING_ID] = tracking_id
    message = _str_or_none(_attr(response, "message"))
    if message:
        headers["message"] = message
    return headers


def stream_events_from_sdk_response(stream_name: str, response: Any) -> tuple[StreamEvent, ...]:
    received_at = datetime.now(tz=UTC)
    events: list[StreamEvent] = []
    if _is_present(_attr(response, "ping")):
        events.append(
            StreamEvent(
                stream_name=stream_name,
                event_type="ping",
                payload=_dataclass_payload(_attr(response, "ping")),
                received_at=received_at,
            )
        )
    if _is_present(_attr(response, "candle")):
        events.append(
            StreamEvent(
                stream_name=stream_name,
                event_type="candle",
                payload=_stream_candle_payload(_attr(response, "candle")),
                received_at=received_at,
            )
        )
    if _is_present(_attr(response, "orderbook")):
        events.append(
            StreamEvent(
                stream_name=stream_name,
                event_type="order_book",
                payload=_order_book_payload(_attr(response, "orderbook")),
                received_at=received_at,
            )
        )
    if _is_present(_attr(response, "last_price")):
        events.append(
            StreamEvent(
                stream_name=stream_name,
                event_type="last_price",
                payload=_last_price_payload(_attr(response, "last_price")),
                received_at=received_at,
            )
        )
    if _is_present(_attr(response, "trading_status")):
        events.append(
            StreamEvent(
                stream_name=stream_name,
                event_type="trading_status",
                payload=_trading_status_stream_payload(_attr(response, "trading_status")),
                received_at=received_at,
            )
        )
    if _is_present(_attr(response, "trade")):
        events.append(
            StreamEvent(
                stream_name=stream_name,
                event_type="market_trade",
                payload=_market_trade_payload(_attr(response, "trade")),
                received_at=received_at,
            )
        )
    if _is_present(_attr(response, "order_state")):
        events.append(
            StreamEvent(
                stream_name=stream_name,
                event_type="user_order_state",
                payload=_order_state_stream_payload(_attr(response, "order_state")),
                received_at=received_at,
            )
        )
    return tuple(events)


def _call_sdk_method(sdk: Any, services: Any, method_name: str, payload: JsonPayload) -> Any:
    if method_name == "TradingSchedules":
        return services.instruments.trading_schedules(
            exchange=str(payload["exchange"]),
            from_=_datetime_from_payload(payload["from"]),
            to=_datetime_from_payload(payload["to"]),
        )
    if method_name == "ResolveInstruments":
        tickers = tuple(str(ticker).upper() for ticker in payload.get("tickers", ()))
        class_code = str(payload.get("class_code") or "TQBR")
        instruments: list[JsonPayload] = []
        for ticker in tickers:
            try:
                response = services.instruments.share_by(
                    id_type=_enum(sdk, "InstrumentIdType", "INSTRUMENT_ID_TYPE_TICKER"),
                    class_code=class_code,
                    id=ticker,
                )
                instrument = _attr(response, "instrument")
            except Exception:
                response = services.instruments.find_instrument(
                    query=ticker,
                    instrument_kind=_enum_or_none(
                        sdk,
                        "InstrumentType",
                        "INSTRUMENT_TYPE_SHARE",
                    ),
                    api_trade_available_flag=None,
                )
                instrument = _best_instrument_for_ticker(
                    _list_attr(response, "instruments"),
                    ticker=ticker,
                    class_code=class_code,
                )
            if _is_present(instrument):
                instruments.append(_instrument_catalog_payload(instrument))
        return {"instruments": instruments}
    if method_name == "GetTradingStatus":
        return services.market_data.get_trading_status(
            instrument_id=_instrument_id(payload["instrument"])
        )
    if method_name == "GetCandles":
        return services.market_data.get_candles(
            instrument_id=_instrument_id(payload["instrument"]),
            from_=_datetime_from_payload(payload["from"]),
            to=_datetime_from_payload(payload["to"]),
            interval=_candle_interval(sdk, str(payload["interval"])),
            candle_source_type=_enum_or_none(sdk, "CandleSource", "CANDLE_SOURCE_EXCHANGE"),
        )
    if method_name == "GetDividends":
        return services.instruments.get_dividends(
            instrument_id=_instrument_id(payload["instrument"]),
            from_=_datetime_from_payload(payload["from"]),
            to=_datetime_from_payload(payload["to"]),
        )
    if method_name == "GetLastPrices":
        last_price_instruments = cast_list_of_dict(payload["instruments"])
        return services.market_data.get_last_prices(
            instrument_id=[_instrument_id(item) for item in last_price_instruments],
            last_price_type=_enum_or_none(sdk, "LastPriceType", "LAST_PRICE_EXCHANGE"),
        )
    if method_name == "GetLastTrades":
        return services.market_data.get_last_trades(
            instrument_id=_instrument_id(payload["instrument"]),
            from_=_datetime_from_payload(payload["from"]),
            to=_datetime_from_payload(payload["to"]),
            trade_source=_trade_source_type(sdk, str(payload.get("trade_source") or "all")),
        )
    if method_name == "GetOrderBook":
        return services.market_data.get_order_book(
            instrument_id=_instrument_id(payload["instrument"]),
            depth=int(payload["depth"]),
        )
    if method_name == "PostOrder":
        return services.orders.post_order(
            quantity=int(payload["lot_qty"]),
            price=_quotation(sdk, payload.get("price")),
            direction=_order_direction(sdk, str(payload["side"])),
            account_id=str(payload["account_id"]),
            order_type=_order_type(sdk, str(payload["order_type"])),
            order_id=str(payload["request_order_id"]),
            instrument_id=_instrument_id(payload["instrument"]),
            time_in_force=_time_in_force(sdk, str(payload["time_in_force"])),
            price_type=_enum_or_none(sdk, "PriceType", "PRICE_TYPE_CURRENCY"),
            confirm_margin_trade=False,
        )
    if method_name == "CancelOrder":
        order_id, order_id_type = _order_id_and_type(sdk, payload)
        return services.orders.cancel_order(
            account_id=str(payload["account_id"]),
            order_id=order_id,
            order_id_type=order_id_type,
        )
    if method_name == "GetOrderState":
        order_id, order_id_type = _order_id_and_type(sdk, payload)
        return services.orders.get_order_state(
            account_id=str(payload["account_id"]),
            order_id=order_id,
            order_id_type=order_id_type,
            price_type=_enum_or_none(sdk, "PriceType", "PRICE_TYPE_CURRENCY"),
        )
    if method_name == "GetOrders":
        return services.orders.get_orders(
            account_id=str(payload["account_id"])
        )
    if method_name == "GetPortfolio":
        return services.operations.get_portfolio(account_id=str(payload["account_id"]))
    if method_name == "GetPositions":
        return services.operations.get_positions(account_id=str(payload["account_id"]))
    if method_name == "GetAccounts":
        return services.users.get_accounts()
    if method_name == "PostStopOrder":
        return services.stop_orders.post_stop_order(
            quantity=int(payload["lot_qty"]),
            price=_quotation(sdk, payload.get("price")),
            stop_price=_quotation(sdk, payload.get("stop_price")),
            direction=_stop_order_direction(sdk, str(payload["side"])),
            account_id=str(payload["account_id"]),
            expiration_type=_stop_expiration_type(sdk, str(payload["expiration_type"])),
            stop_order_type=_stop_order_type(sdk, str(payload["stop_order_type"])),
            expire_date=_datetime_from_payload(payload.get("expire_date")),
            instrument_id=_instrument_id(payload["instrument"]),
            exchange_order_type=_enum_or_none(
                sdk,
                "ExchangeOrderType",
                "EXCHANGE_ORDER_TYPE_LIMIT",
            ),
            take_profit_type=_enum_or_none(
                sdk,
                "TakeProfitType",
                "TAKE_PROFIT_TYPE_REGULAR",
            ),
            price_type=_enum_or_none(sdk, "PriceType", "PRICE_TYPE_CURRENCY"),
            order_id=str(payload["request_order_id"]),
            confirm_margin_trade=False,
        )
    msg = f"Unsupported T-Bank SDK unary method: {method_name}"
    raise NotImplementedError(msg)


def _market_stream_requests(
    sdk: Any,
    *,
    stream_name: str,
    instruments: tuple[str, ...],
    depth: int,
    ping_interval_seconds: float,
) -> tuple[Any, ...]:
    stream = stream_name.lower()
    requests: list[Any] = []
    if "candle" in stream:
        requests.append(
            _sdk_type(sdk, "MarketDataRequest")(
                subscribe_candles_request=_sdk_type(sdk, "SubscribeCandlesRequest")(
                    subscription_action=_subscribe_action(sdk),
                    instruments=[
                        _sdk_type(sdk, "CandleInstrument")(
                            instrument_id=instrument_id,
                            interval=_enum(
                                sdk,
                                "SubscriptionInterval",
                                "SUBSCRIPTION_INTERVAL_ONE_MINUTE",
                            ),
                        )
                        for instrument_id in instruments
                    ],
                    waiting_close=True,
                    candle_source_type=_enum_or_none(
                        sdk,
                        "CandleSource",
                        "CANDLE_SOURCE_EXCHANGE",
                    ),
                )
            )
        )
    elif "order_book" in stream or "book" in stream:
        requests.append(
            _sdk_type(sdk, "MarketDataRequest")(
                subscribe_order_book_request=_sdk_type(sdk, "SubscribeOrderBookRequest")(
                    subscription_action=_subscribe_action(sdk),
                    instruments=[
                        _sdk_type(sdk, "OrderBookInstrument")(
                            instrument_id=instrument_id,
                            depth=depth,
                            order_book_type=_enum_or_none(
                                sdk,
                                "OrderBookType",
                                "ORDERBOOK_TYPE_EXCHANGE",
                            ),
                        )
                        for instrument_id in instruments
                    ],
                )
            )
        )
    elif "last" in stream:
        requests.append(
            _sdk_type(sdk, "MarketDataRequest")(
                subscribe_last_price_request=_sdk_type(sdk, "SubscribeLastPriceRequest")(
                    subscription_action=_subscribe_action(sdk),
                    instruments=[
                        _sdk_type(sdk, "LastPriceInstrument")(instrument_id=instrument_id)
                        for instrument_id in instruments
                    ],
                )
            )
        )
    elif "status" in stream or "info" in stream:
        requests.append(
            _sdk_type(sdk, "MarketDataRequest")(
                subscribe_info_request=_sdk_type(sdk, "SubscribeInfoRequest")(
                    subscription_action=_subscribe_action(sdk),
                    instruments=[
                        _sdk_type(sdk, "InfoInstrument")(instrument_id=instrument_id)
                        for instrument_id in instruments
                    ],
                )
            )
        )
    elif "trade" in stream:
        requests.append(
            _sdk_type(sdk, "MarketDataRequest")(
                subscribe_trades_request=_sdk_type(sdk, "SubscribeTradesRequest")(
                    subscription_action=_subscribe_action(sdk),
                    instruments=[
                        _sdk_type(sdk, "TradeInstrument")(instrument_id=instrument_id)
                        for instrument_id in instruments
                    ],
                    trade_source=_enum_or_none(
                        sdk,
                        "TradeSourceType",
                        "TRADE_SOURCE_EXCHANGE",
                    ),
                    with_open_interest=False,
                )
            )
        )
    requests.append(_ping_settings_request(sdk, ping_interval_seconds))
    return tuple(requests)


def _ping_settings_request(sdk: Any, ping_interval_seconds: float) -> Any:
    ping_settings_type = _sdk_type(sdk, "PingDelaySettings")
    market_data_request_type = _sdk_type(sdk, "MarketDataRequest")
    return market_data_request_type(
        ping_settings=ping_settings_type(
            ping_delay_ms=int(ping_interval_seconds * 1000),
        )
    )


@contextmanager
def _real_services(sdk: Any, token: str, target: str, app_name: str) -> Iterator[Any]:
    os.environ.setdefault("SSL_TBANK_VERIFY", "true")
    with sdk.Client(token, target=target, app_name=app_name) as services:
        yield services


def _real_services_factory(sdk: Any) -> ServicesFactory:
    return lambda token, target, app_name: _real_services(sdk, token, target, app_name)


def _next_or_none(iterator: Iterator[Any]) -> Any | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _token_from_metadata(metadata: tuple[tuple[str, str], ...]) -> str:
    authorization = _metadata_value(metadata, "authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    msg = "T-Bank SDK client requires authorization metadata"
    raise RuntimeError(msg)


def _metadata_value(metadata: tuple[tuple[str, str], ...], key: str) -> str | None:
    normalized = key.lower()
    for item_key, value in metadata:
        if item_key.lower() == normalized:
            return value
    return None


def _stream_instruments_from_env() -> tuple[str, ...]:
    raw = os.getenv(DEFAULT_STREAM_INSTRUMENTS_ENV, "MOEX:SBER")
    instruments = tuple(item.strip() for item in raw.split(",") if item.strip())
    return instruments or ("MOEX:SBER",)


def _sdk_type(sdk: Any, name: str) -> Any:
    value = getattr(sdk, name, None)
    if value is not None:
        return value
    schemas = getattr(sdk, "schemas", None)
    value = getattr(schemas, name, None) if schemas is not None else None
    if value is None:
        msg = f"T-Bank SDK type not found: {name}"
        raise AttributeError(msg)
    return value


def _enum(sdk: Any, enum_name: str, member_name: str) -> Any:
    enum_type = _sdk_type(sdk, enum_name)
    return getattr(enum_type, member_name)


def _enum_or_none(sdk: Any, enum_name: str, member_name: str) -> Any | None:
    try:
        return _enum(sdk, enum_name, member_name)
    except AttributeError:
        return None


def _subscribe_action(sdk: Any) -> Any:
    return _enum(sdk, "SubscriptionAction", "SUBSCRIPTION_ACTION_SUBSCRIBE")


def _candle_interval(sdk: Any, interval: str) -> Any:
    mapping = {
        "1m": "CANDLE_INTERVAL_1_MIN",
        "5m": "CANDLE_INTERVAL_5_MIN",
        "10m": "CANDLE_INTERVAL_10_MIN",
        "15m": "CANDLE_INTERVAL_15_MIN",
        "30m": "CANDLE_INTERVAL_30_MIN",
        "1h": "CANDLE_INTERVAL_HOUR",
    }
    return _enum(sdk, "CandleInterval", mapping.get(interval.lower(), "CANDLE_INTERVAL_1_MIN"))


def _trade_source_type(sdk: Any, value: str) -> Any | None:
    mapping = {
        "all": "TRADE_SOURCE_ALL",
        "dealer": "TRADE_SOURCE_DEALER",
        "otc": "TRADE_SOURCE_DEALER",
        "exchange": "TRADE_SOURCE_EXCHANGE",
    }
    return _enum_or_none(
        sdk,
        "TradeSourceType",
        mapping.get(value.lower(), "TRADE_SOURCE_ALL"),
    )


def _order_direction(sdk: Any, side: str) -> Any:
    return _enum(
        sdk,
        "OrderDirection",
        "ORDER_DIRECTION_SELL" if side.lower() == "sell" else "ORDER_DIRECTION_BUY",
    )


def _stop_order_direction(sdk: Any, side: str) -> Any:
    return _enum(
        sdk,
        "StopOrderDirection",
        "STOP_ORDER_DIRECTION_SELL" if side.lower() == "sell" else "STOP_ORDER_DIRECTION_BUY",
    )


def _order_type(sdk: Any, order_type: str) -> Any:
    mapping = {
        "limit": "ORDER_TYPE_LIMIT",
        "market": "ORDER_TYPE_MARKET",
        "bestprice": "ORDER_TYPE_BESTPRICE",
        "best_price": "ORDER_TYPE_BESTPRICE",
    }
    return _enum(sdk, "OrderType", mapping.get(order_type.lower(), "ORDER_TYPE_LIMIT"))


def _time_in_force(sdk: Any, value: str) -> Any:
    mapping = {
        "day": "TIME_IN_FORCE_DAY",
        "fill_and_kill": "TIME_IN_FORCE_FILL_AND_KILL",
        "fill_or_kill": "TIME_IN_FORCE_FILL_OR_KILL",
    }
    return _enum(sdk, "TimeInForceType", mapping.get(value.lower(), "TIME_IN_FORCE_DAY"))


def _stop_order_type(sdk: Any, value: str) -> Any:
    mapping = {
        "take_profit": "STOP_ORDER_TYPE_TAKE_PROFIT",
        "stop_loss": "STOP_ORDER_TYPE_STOP_LOSS",
        "stop_limit": "STOP_ORDER_TYPE_STOP_LIMIT",
    }
    return _enum(sdk, "StopOrderType", mapping.get(value.lower(), "STOP_ORDER_TYPE_STOP_LIMIT"))


def _stop_expiration_type(sdk: Any, value: str) -> Any:
    mapping = {
        "good_till_cancel": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
        "good_till_date": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_DATE",
    }
    return _enum(
        sdk,
        "StopOrderExpirationType",
        mapping.get(value.lower(), "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL"),
    )


def _order_id_and_type(sdk: Any, payload: Mapping[str, object]) -> tuple[str, Any | None]:
    exchange_order_id = _str_or_none(payload.get("exchange_order_id"))
    if exchange_order_id:
        return exchange_order_id, _enum_or_none(sdk, "OrderIdType", "ORDER_ID_TYPE_EXCHANGE")
    request_order_id = _str_or_none(payload.get("request_order_id"))
    if request_order_id:
        return request_order_id, _enum_or_none(sdk, "OrderIdType", "ORDER_ID_TYPE_REQUEST")
    msg = "Either exchange_order_id or request_order_id is required"
    raise ValueError(msg)


def _quotation(sdk: Any, value: object) -> Any | None:
    if value is None:
        return None
    decimal_value = Decimal(str(value))
    units = int(decimal_value)
    nano = int((decimal_value - Decimal(units)) * NANO)
    return _sdk_type(sdk, "Quotation")(units=units, nano=nano)


def _instrument_id(value: object) -> str:
    if isinstance(value, Mapping):
        raw = (
            value.get("instrument_uid")
            or value.get("figi")
            or value.get("instrument_id")
        )
        if raw:
            identifier = str(raw)
            if identifier.upper().startswith("MOEX:"):
                msg = "T-Bank broker calls require resolved instrument_uid or figi"
                raise ValueError(msg)
            return identifier
    msg = "instrument payload must contain instrument_uid or figi"
    raise ValueError(msg)


def cast_list_of_dict(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list | tuple):
        msg = "Expected list of instrument payloads"
        raise TypeError(msg)
    return [item for item in value if isinstance(item, Mapping)]


def _trading_schedules_payload(response: Any) -> JsonPayload:
    windows: list[JsonPayload] = []
    exchanges: list[JsonPayload] = []
    for exchange in _list_attr(response, "exchanges"):
        exchange_payload = _dataclass_payload(exchange)
        exchanges.append(exchange_payload)
        for day in _list_attr(exchange, "days"):
            if not bool(_attr(day, "is_trading_day")):
                continue
            windows.extend(_windows_from_trading_day(day))
    return {"exchanges": exchanges, "windows": windows}


def _windows_from_trading_day(day: Any) -> list[JsonPayload]:
    trading_date = _date_iso(_attr(day, "date"))
    return [
        window
        for window in (
            _schedule_window(
                day,
                session_type="weekday_morning",
                start_field="premarket_start_time",
                end_field="premarket_end_time",
                trading_date=trading_date,
            ),
            _schedule_window(
                day,
                session_type="weekday_main",
                start_field="start_time",
                end_field="end_time",
                trading_date=trading_date,
            ),
            _schedule_window(
                day,
                session_type="weekday_evening",
                start_field="evening_start_time",
                end_field="evening_end_time",
                trading_date=trading_date,
            ),
        )
        if window is not None
    ]


def _schedule_window(
    day: Any,
    *,
    session_type: str,
    start_field: str,
    end_field: str,
    trading_date: str | None,
) -> JsonPayload | None:
    start = _attr(day, start_field)
    end = _attr(day, end_field)
    if not _is_present(start) or not _is_present(end) or start == end:
        return None
    normalized_session_type = (
        "weekend" if _is_weekend_date(trading_date) else session_type
    )
    return {
        "session_type": normalized_session_type,
        "session_phase": "continuous_trading",
        "start_at": _iso_or_none(start),
        "end_at": _iso_or_none(end),
        "trading_date": trading_date,
        "calendar_date": trading_date,
    }


def _is_weekend_date(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return date.fromisoformat(value).weekday() >= 5
    except ValueError:
        return False


def _trading_status_payload(response: Any, *, request_payload: Mapping[str, object]) -> JsonPayload:
    instrument_payload = request_payload.get("instrument")
    instrument_id = (
        _instrument_id(instrument_payload)
        if isinstance(instrument_payload, Mapping)
        else None
    )
    return {
        "instrument_id": instrument_id or _str_or_none(_attr(response, "instrument_uid")),
        "instrument_uid": _str_or_none(_attr(response, "instrument_uid")),
        "figi": _str_or_none(_attr(response, "figi")),
        "trading_status": _enum_name(_attr(response, "trading_status")),
        "status": _enum_name(_attr(response, "trading_status")),
        "api_trade_available": bool(_attr(response, "api_trade_available_flag")),
        "limit_order_available": bool(_attr(response, "limit_order_available_flag")),
        "market_order_available": bool(_attr(response, "market_order_available_flag")),
        "bestprice_order_available": bool(_attr(response, "bestprice_order_available_flag")),
    }


def _trading_status_stream_payload(status: Any) -> JsonPayload:
    instrument_id = _str_or_none(_attr(status, "instrument_uid")) or _str_or_none(
        _attr(status, "figi")
    )
    return {
        "instrument_id": instrument_id,
        "instrument_uid": _str_or_none(_attr(status, "instrument_uid")),
        "figi": _str_or_none(_attr(status, "figi")),
        "trading_status": _enum_name(_attr(status, "trading_status")),
        "status": _enum_name(_attr(status, "trading_status")),
        "api_trade_available": bool(_attr(status, "limit_order_available_flag"))
        or bool(_attr(status, "market_order_available_flag")),
        "exchange_ts": _iso_or_none(_attr(status, "time")),
    }


def _historic_candle_payload(candle: Any, request_payload: Mapping[str, object]) -> JsonPayload:
    open_ts = _attr(candle, "time")
    interval = str(request_payload.get("interval", "1m"))
    close_ts = _add_interval(open_ts, interval)
    instrument_payload = request_payload.get("instrument")
    instrument_id = (
        _instrument_id(instrument_payload)
        if isinstance(instrument_payload, Mapping)
        else _str_or_none(_attr(candle, "instrument_uid"))
    )
    return {
        "instrument_id": instrument_id,
        "figi": _str_or_none(_attr(candle, "figi")),
        "instrument_uid": _str_or_none(_attr(candle, "instrument_uid")),
        "timeframe": interval,
        "open_ts_utc": _iso_or_none(open_ts),
        "close_ts_utc": _iso_or_none(close_ts),
        "exchange_open_ts": _iso_or_none(open_ts),
        "exchange_close_ts": _iso_or_none(close_ts),
        "open_price": str(_decimal_from_quotation(_attr(candle, "open"))),
        "high_price": str(_decimal_from_quotation(_attr(candle, "high"))),
        "low_price": str(_decimal_from_quotation(_attr(candle, "low"))),
        "close_price": str(_decimal_from_quotation(_attr(candle, "close"))),
        "volume_lots": str(_attr(candle, "volume") or 0),
        "is_closed": bool(_attr(candle, "is_complete")),
        "source": "tbank_get_candles",
    }


def _dividend_payload(dividend: Any, request_payload: Mapping[str, object]) -> JsonPayload:
    instrument_id = _instrument_id_from_request_payload(request_payload)
    raw_payload = _dataclass_payload(dividend)
    return {
        "instrument_id": instrument_id,
        "declared_date": _date_or_datetime_iso(_attr(dividend, "declared_date")),
        "record_date": _date_or_datetime_iso(_attr(dividend, "record_date")),
        "last_buy_date": _date_or_datetime_iso(_attr(dividend, "last_buy_date")),
        "payment_date": _date_or_datetime_iso(_attr(dividend, "payment_date")),
        "dividend_type": _str_or_none(_attr(dividend, "dividend_type")),
        "regularity": _str_or_none(_attr(dividend, "regularity")),
        "amount_per_share": _decimal_str_or_none(_attr(dividend, "dividend_net")),
        "currency": _currency_from_money(_attr(dividend, "dividend_net")),
        "close_price": _decimal_str_or_none(_attr(dividend, "close_price")),
        "yield_value": _decimal_str_or_none(_attr(dividend, "yield_value")),
        "created_at": _iso_or_none(_attr(dividend, "created_at")),
        "raw_payload": raw_payload,
    }


def _stream_candle_payload(candle: Any) -> JsonPayload:
    open_ts = _attr(candle, "time")
    interval = _stream_interval_value(_attr(candle, "interval"))
    close_ts = _add_interval(open_ts, interval)
    instrument_id = _str_or_none(_attr(candle, "instrument_uid")) or _str_or_none(
        _attr(candle, "figi")
    )
    return {
        "instrument_id": instrument_id,
        "figi": _str_or_none(_attr(candle, "figi")),
        "instrument_uid": _str_or_none(_attr(candle, "instrument_uid")),
        "ticker": _str_or_none(_attr(candle, "ticker")),
        "class_code": _str_or_none(_attr(candle, "class_code")),
        "timeframe": interval,
        "open_ts_utc": _iso_or_none(open_ts),
        "close_ts_utc": _iso_or_none(close_ts),
        "exchange_open_ts": _iso_or_none(open_ts),
        "exchange_close_ts": _iso_or_none(close_ts),
        "open_price": str(_decimal_from_quotation(_attr(candle, "open"))),
        "high_price": str(_decimal_from_quotation(_attr(candle, "high"))),
        "low_price": str(_decimal_from_quotation(_attr(candle, "low"))),
        "close_price": str(_decimal_from_quotation(_attr(candle, "close"))),
        "volume_lots": str(_attr(candle, "volume") or 0),
        "is_closed": True,
        "complete": True,
        "source": "tbank_waiting_close_stream",
    }


def _last_price_payload(item: Any) -> JsonPayload:
    instrument_id = _str_or_none(_attr(item, "instrument_uid")) or _str_or_none(
        _attr(item, "figi")
    )
    return {
        "instrument_id": instrument_id,
        "figi": _str_or_none(_attr(item, "figi")),
        "instrument_uid": _str_or_none(_attr(item, "instrument_uid")),
        "price": str(_decimal_from_quotation(_attr(item, "price"))),
        "exchange_ts": _iso_or_none(_attr(item, "time")),
    }


def _order_book_payload(book: Any) -> JsonPayload:
    exchange_ts = _attr(book, "orderbook_ts") or _attr(book, "time")
    instrument_id = _str_or_none(_attr(book, "instrument_uid")) or _str_or_none(
        _attr(book, "figi")
    )
    return {
        "instrument_id": instrument_id,
        "figi": _str_or_none(_attr(book, "figi")),
        "instrument_uid": _str_or_none(_attr(book, "instrument_uid")),
        "depth": int(_attr(book, "depth") or 0),
        "exchange_ts": _iso_or_none(exchange_ts),
        "bids": [_price_level_payload(level) for level in _list_attr(book, "bids")],
        "asks": [_price_level_payload(level) for level in _list_attr(book, "asks")],
        "is_consistent": bool(_attr(book, "is_consistent", default=True)),
    }


def _price_level_payload(level: Any) -> JsonPayload:
    return {
        "price": str(_decimal_from_quotation(_attr(level, "price"))),
        "quantity_lots": str(_attr(level, "quantity") or 0),
    }


def _market_trade_payload(trade: Any) -> JsonPayload:
    instrument_id = _str_or_none(_attr(trade, "instrument_uid")) or _str_or_none(
        _attr(trade, "figi")
    )
    return {
        "instrument_id": instrument_id,
        "figi": _str_or_none(_attr(trade, "figi")),
        "instrument_uid": _str_or_none(_attr(trade, "instrument_uid")),
        "price": str(_decimal_from_quotation(_attr(trade, "price"))),
        "quantity_lots": str(_attr(trade, "quantity") or 0),
        "side": _enum_name(_attr(trade, "direction")),
        "exchange_ts": _iso_or_none(_attr(trade, "time")),
    }


def _order_state_payload(order: Any, *, default_status: str) -> JsonPayload:
    status = _broker_status(_attr(order, "execution_report_status"), default=default_status)
    stages = _list_attr(order, "stages")
    return {
        "exchange_order_id": _str_or_none(_attr(order, "order_id")),
        "order_id": _str_or_none(_attr(order, "order_id")),
        "request_order_id": _str_or_none(_attr(order, "order_request_id")),
        "broker_status": status,
        "status": status,
        "lots_requested": int(_attr(order, "lots_requested") or 0),
        "lots_executed": int(_attr(order, "lots_executed") or 0),
        "instrument_uid": _str_or_none(_attr(order, "instrument_uid")),
        "figi": _str_or_none(_attr(order, "figi")),
        "direction": _enum_name(_attr(order, "direction")),
        "order_type": _enum_name(_attr(order, "order_type")),
        "order_date": _iso_or_none(_attr(order, "order_date")),
        "message": _str_or_none(_attr(order, "message")),
        "fills": [
            _fill_payload(stage, order=order, index=index)
            for index, stage in enumerate(stages)
        ],
    }


def _order_state_stream_payload(order: Any) -> JsonPayload:
    status = _broker_status(_attr(order, "execution_report_status"), default="observed")
    trades = _list_attr(order, "trades")
    exchange_order_id = _str_or_none(_attr(order, "order_id")) or _str_or_none(
        _attr(order, "trade_order_id")
    )
    return {
        "account_id": _str_or_none(_attr(order, "account_id")),
        "request_order_id": _str_or_none(_attr(order, "order_request_id")),
        "exchange_order_id": exchange_order_id,
        "broker_status": status,
        "status": status,
        "ticker": _str_or_none(_attr(order, "ticker")),
        "class_code": _str_or_none(_attr(order, "class_code")),
        "instrument_uid": _str_or_none(_attr(order, "instrument_uid")),
        "lots_requested": int(_attr(order, "lots_requested") or 0),
        "lots_executed": int(_attr(order, "lots_executed") or 0),
        "lots_left": int(_attr(order, "lots_left") or 0),
        "created_at": _iso_or_none(_attr(order, "created_at")),
        "completion_time": _iso_or_none(_attr(order, "completion_time")),
        "fills": [_dataclass_payload(trade) for trade in trades],
    }


def _portfolio_payload(
    response: Any,
    *,
    request_payload: Mapping[str, object],
) -> JsonPayload:
    positions = [_portfolio_position_payload(item) for item in _list_attr(response, "positions")]
    return {
        "account_id": _str_or_none(request_payload.get("account_id")),
        "positions": positions,
        "total_amount_portfolio": str(
            _decimal_from_quotation(_attr(response, "total_amount_portfolio"))
        ),
        "expected_yield": str(_decimal_from_quotation(_attr(response, "expected_yield"))),
        "total_amount_shares": str(_decimal_from_quotation(_attr(response, "total_amount_shares"))),
        "total_amount_bonds": str(_decimal_from_quotation(_attr(response, "total_amount_bonds"))),
        "total_amount_etf": str(_decimal_from_quotation(_attr(response, "total_amount_etf"))),
        "total_amount_currencies": str(
            _decimal_from_quotation(_attr(response, "total_amount_currencies"))
        ),
        "total_amount_futures": str(
            _decimal_from_quotation(_attr(response, "total_amount_futures"))
        ),
        "available_margin": str(_decimal_from_quotation(_attr(response, "available_margin"))),
    }


def _positions_payload(
    response: Any,
    *,
    request_payload: Mapping[str, object],
) -> JsonPayload:
    positions = [
        _security_position_payload(item)
        for item in (
            _list_attr(response, "securities")
            + _list_attr(response, "futures")
            + _list_attr(response, "options")
        )
    ]
    return {
        "account_id": _str_or_none(request_payload.get("account_id")),
        "positions": positions,
        "money": [_dataclass_payload(item) for item in _list_attr(response, "money")],
        "blocked": [_dataclass_payload(item) for item in _list_attr(response, "blocked")],
        "limits_loading_in_progress": bool(_attr(response, "limits_loading_in_progress")),
    }


def _portfolio_position_payload(item: Any) -> JsonPayload:
    quantity_lots = _decimal_from_quotation(_attr(item, "quantity_lots"))
    if quantity_lots == Decimal("0"):
        quantity_lots = _decimal_from_quotation(_attr(item, "quantity"))
    current_price = _decimal_from_quotation(_attr(item, "current_price"))
    return {
        "instrument_id": _position_instrument_id(item),
        "figi": _str_or_none(_attr(item, "figi")),
        "instrument_uid": _str_or_none(_attr(item, "instrument_uid")),
        "position_uid": _str_or_none(_attr(item, "position_uid")),
        "instrument_type": _str_or_none(_attr(item, "instrument_type")),
        "qty_lots": str(quantity_lots),
        "position_side": _position_side(quantity_lots),
        "avg_price": str(_decimal_from_quotation(_attr(item, "average_position_price"))),
        "market_price": str(current_price),
        "unrealized_pnl": str(_decimal_from_quotation(_attr(item, "expected_yield"))),
        "realised_pnl": None,
        "exposure": str(abs(quantity_lots) * current_price),
        "blocked_lots": str(_decimal_from_quotation(_attr(item, "blocked_lots"))),
        "short_available": _bool_attr(item, "short_enabled_flag", default=True),
    }


def _security_position_payload(item: Any) -> JsonPayload:
    balance = Decimal(str(_attr(item, "balance", default=0) or 0))
    return {
        "instrument_id": _position_instrument_id(item),
        "figi": _str_or_none(_attr(item, "figi")),
        "instrument_uid": _str_or_none(_attr(item, "instrument_uid")),
        "position_uid": _str_or_none(_attr(item, "position_uid")),
        "instrument_type": _str_or_none(_attr(item, "instrument_type")),
        "qty_lots": str(balance),
        "position_side": _position_side(balance),
        "avg_price": None,
        "market_price": None,
        "unrealized_pnl": None,
        "realised_pnl": None,
        "exposure": None,
        "blocked_lots": str(_attr(item, "blocked", default=0) or 0),
        "exchange_blocked": bool(_attr(item, "exchange_blocked")),
        "short_available": _bool_attr(item, "short_enabled_flag", default=True),
    }


def _account_payload(account: Any) -> JsonPayload:
    return {
        "account_id": _str_or_none(_attr(account, "id")),
        "name": _str_or_none(_attr(account, "name")),
        "type": _enum_name(_attr(account, "type")),
        "status": _enum_name(_attr(account, "status")),
        "access_level": _enum_name(_attr(account, "access_level")),
        "opened_date": _iso_or_none(_attr(account, "opened_date")),
        "closed_date": _iso_or_none(_attr(account, "closed_date")),
    }


def _best_instrument_for_ticker(
    instruments: list[Any],
    *,
    ticker: str,
    class_code: str,
) -> Any | None:
    for instrument in instruments:
        if (
            _str_or_none(_attr(instrument, "ticker")) == ticker
            and _str_or_none(_attr(instrument, "class_code")) == class_code
        ):
            return instrument
    for instrument in instruments:
        if _str_or_none(_attr(instrument, "ticker")) == ticker:
            return instrument
    return instruments[0] if instruments else None


def _instrument_catalog_payload(instrument: Any) -> JsonPayload:
    instrument_uid = _str_or_none(_attr(instrument, "uid")) or _str_or_none(
        _attr(instrument, "instrument_uid")
    )
    ticker = _str_or_none(_attr(instrument, "ticker"))
    class_code = _str_or_none(_attr(instrument, "class_code"))
    return {
        "instrument_id": instrument_uid or _str_or_none(_attr(instrument, "figi")) or ticker,
        "instrument_uid": instrument_uid,
        "figi": _str_or_none(_attr(instrument, "figi")),
        "ticker": ticker,
        "class_code": class_code,
        "name": _str_or_none(_attr(instrument, "name")) or ticker or instrument_uid,
        "lot_size": int(_attr(instrument, "lot", default=1) or 1),
        "min_price_increment": str(
            _decimal_from_quotation(_attr(instrument, "min_price_increment"))
        ),
        "currency": (_str_or_none(_attr(instrument, "currency")) or "RUB").upper(),
        "api_trade_available": _bool_attr(instrument, "api_trade_available_flag", default=False),
        "buy_available": _bool_attr(instrument, "buy_available_flag", default=False),
        "sell_available": _bool_attr(instrument, "sell_available_flag", default=False),
        "short_available": _bool_attr(instrument, "short_enabled_flag", default=False),
        "supports_weekend": _bool_attr(instrument, "weekend_flag", default=False),
        "exchange": _str_or_none(_attr(instrument, "exchange")),
        "real_exchange": _enum_name(_attr(instrument, "real_exchange")),
        "instrument_exchange": _enum_name(_attr(instrument, "instrument_exchange")),
    }


def _position_instrument_id(item: Any) -> str | None:
    return (
        _str_or_none(_attr(item, "instrument_uid"))
        or _str_or_none(_attr(item, "figi"))
        or _str_or_none(_attr(item, "ticker"))
    )


def _position_side(quantity_lots: Decimal) -> str:
    if quantity_lots < 0:
        return "short"
    if quantity_lots > 0:
        return "long"
    return "flat"


def _bool_attr(item: Any, name: str, *, default: bool) -> bool:
    value = _attr(item, name, default=None)
    if value is None:
        return default
    return bool(value)


def _fill_payload(stage: Any, *, order: Any, index: int) -> JsonPayload:
    exchange_order_id = _str_or_none(_attr(order, "order_id")) or "unknown"
    trade_id = _str_or_none(_attr(stage, "trade_id")) or f"{exchange_order_id}:{index}"
    return {
        "exchange_order_id": exchange_order_id,
        "broker_fill_id": trade_id,
        "price": str(_decimal_from_quotation(_attr(stage, "price"))),
        "lot_qty": int(_attr(stage, "quantity") or 0),
        "exchange_ts": _iso_or_none(_attr(stage, "execution_time")),
    }


def _broker_status(value: Any, *, default: str) -> str:
    raw = _enum_name(value)
    normalized = raw.lower().removeprefix("execution_report_status_")
    if "partiallyfill" in normalized or "partially_fill" in normalized:
        return "partially_filled"
    if "fill" in normalized:
        return "filled"
    if "reject" in normalized:
        return "rejected"
    if "cancel" in normalized:
        return "cancelled"
    if "new" in normalized:
        return "posted"
    return normalized or default


def _stream_interval_value(value: Any) -> str:
    name = _enum_name(value).upper()
    mapping = {
        "SUBSCRIPTION_INTERVAL_ONE_MINUTE": "1m",
        "SUBSCRIPTION_INTERVAL_FIVE_MINUTES": "5m",
        "SUBSCRIPTION_INTERVAL_10_MIN": "10m",
        "SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES": "15m",
        "SUBSCRIPTION_INTERVAL_30_MIN": "30m",
        "SUBSCRIPTION_INTERVAL_ONE_HOUR": "1h",
    }
    return mapping.get(name, "1m")


def _add_interval(moment: Any, interval: str) -> Any:
    if not isinstance(moment, datetime):
        return moment
    minutes = int(interval.removesuffix("m")) if interval.endswith("m") else 60
    return moment + timedelta(minutes=minutes)


def _decimal_from_quotation(value: Any) -> Decimal:
    if not _is_present(value):
        return Decimal("0")
    units = Decimal(str(_attr(value, "units", default=0) or 0))
    nano = Decimal(str(_attr(value, "nano", default=0) or 0))
    return units + (nano / NANO)


def _decimal_from_money_or_quotation(value: Any) -> Decimal:
    return _decimal_from_quotation(value)


def _decimal_str_or_none(value: Any) -> str | None:
    if not _is_present(value):
        return None
    return str(_decimal_from_money_or_quotation(value))


def _currency_from_money(value: Any) -> str | None:
    currency = _str_or_none(_attr(value, "currency"))
    return currency.upper() if currency else None


def _instrument_id_from_request_payload(request_payload: Mapping[str, object]) -> str:
    instrument_payload = request_payload.get("instrument")
    if isinstance(instrument_payload, Mapping):
        return _instrument_id(instrument_payload)
    return ""


def _dataclass_payload(value: Any) -> JsonPayload:
    if not _is_present(value):
        return {}
    payload: JsonPayload = {}
    annotations = getattr(value, "__annotations__", {})
    keys = annotations.keys() if isinstance(annotations, dict) else vars(value).keys()
    for key in keys:
        item = _attr(value, str(key))
        if not _is_present(item):
            continue
        if isinstance(item, datetime):
            payload[str(key)] = item.isoformat()
        elif isinstance(item, Enum):
            payload[str(key)] = item.name.lower()
        elif isinstance(item, list | tuple):
            payload[str(key)] = [_json_value(child) for child in item]
        else:
            payload[str(key)] = _json_value(item)
    return payload


def _json_value(value: Any) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.name.lower()
    if hasattr(value, "__annotations__"):
        return _dataclass_payload(value)
    return value


def _datetime_from_payload(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    msg = f"Cannot parse datetime payload: {value!r}"
    raise TypeError(msg)


def _date_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return None


def _date_or_datetime_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _iso_or_none(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _enum_name(value: Any) -> str:
    if isinstance(value, Enum):
        return value.name.lower()
    if hasattr(value, "name"):
        return str(value.name).lower()
    return str(value).lower() if _is_present(value) else ""


def _str_or_none(value: object) -> str | None:
    if not _is_present(value):
        return None
    return str(value)


def _attr(value: Any, name: str, *, default: Any = None) -> Any:
    if not _is_present(value):
        return default
    return getattr(value, name, default)


def _list_attr(value: Any, name: str) -> list[Any]:
    items = _attr(value, name, default=[])
    return list(items) if isinstance(items, list | tuple) else []


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    return type(value) is not object

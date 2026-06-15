from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from uuid import UUID

import pytest

from trade_core.broker_gateway import InstrumentRef, OrderPlacementRequest
from trade_core.infra.tbank.config import TBankBrokerConfig, TBankEnvironment
from trade_core.infra.tbank.errors import BrokerGatewayError
from trade_core.infra.tbank.gateway import TBankBrokerGateway
from trade_core.infra.tbank.headers import auth_metadata
from trade_core.infra.tbank.protocols import JsonPayload
from trade_core.infra.tbank.retry import ExponentialBackoff
from trade_core.infra.tbank.sdk_clients import (
    ServicesFactory,
    TBankSdkStreamClient,
    TBankSdkUnaryClient,
)
from trade_core.infra.tbank.secrets import TBankTokenBundle


class Box:
    def __init__(self, **kwargs: object) -> None:
        self.__annotations__ = {key: object for key in kwargs}
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeSubscriptionAction(Enum):
    SUBSCRIPTION_ACTION_SUBSCRIBE = 1


class FakeSubscriptionInterval(Enum):
    SUBSCRIPTION_INTERVAL_ONE_MINUTE = 1
    SUBSCRIPTION_INTERVAL_10_MIN = 10


class FakeCandleInterval(Enum):
    CANDLE_INTERVAL_1_MIN = 1
    CANDLE_INTERVAL_5_MIN = 5
    CANDLE_INTERVAL_10_MIN = 10
    CANDLE_INTERVAL_15_MIN = 15


class FakeCandleSource(Enum):
    CANDLE_SOURCE_EXCHANGE = 1


class FakeLastPriceType(Enum):
    LAST_PRICE_EXCHANGE = 1


class FakeOrderBookType(Enum):
    ORDERBOOK_TYPE_EXCHANGE = 1


class FakeTradeSourceType(Enum):
    TRADE_SOURCE_EXCHANGE = 1


class FakeOrderDirection(Enum):
    ORDER_DIRECTION_BUY = 1
    ORDER_DIRECTION_SELL = 2


class FakeStopOrderDirection(Enum):
    STOP_ORDER_DIRECTION_BUY = 1
    STOP_ORDER_DIRECTION_SELL = 2


class FakeOrderType(Enum):
    ORDER_TYPE_LIMIT = 1
    ORDER_TYPE_MARKET = 2
    ORDER_TYPE_BESTPRICE = 3


class FakeTimeInForceType(Enum):
    TIME_IN_FORCE_DAY = 1
    TIME_IN_FORCE_FILL_AND_KILL = 2
    TIME_IN_FORCE_FILL_OR_KILL = 3


class FakePriceType(Enum):
    PRICE_TYPE_CURRENCY = 1


class FakeStopOrderType(Enum):
    STOP_ORDER_TYPE_STOP_LIMIT = 1
    STOP_ORDER_TYPE_STOP_LOSS = 2
    STOP_ORDER_TYPE_TAKE_PROFIT = 3


class FakeStopOrderExpirationType(Enum):
    STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL = 1
    STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_DATE = 2


class FakeExchangeOrderType(Enum):
    EXCHANGE_ORDER_TYPE_LIMIT = 1


class FakeTakeProfitType(Enum):
    TAKE_PROFIT_TYPE_REGULAR = 1


class FakeOrderIdType(Enum):
    ORDER_ID_TYPE_EXCHANGE = 1
    ORDER_ID_TYPE_REQUEST = 2


class FakeExecutionReportStatus(Enum):
    EXECUTION_REPORT_STATUS_NEW = 1
    EXECUTION_REPORT_STATUS_FILL = 2
    EXECUTION_REPORT_STATUS_REJECTED = 3


class FakeStatus(Enum):
    INVALID_ARGUMENT = 1


class FakeSdkException(Exception):
    def __init__(self) -> None:
        super().__init__("30028 invalid order")
        self.code = FakeStatus.INVALID_ARGUMENT
        self.details = "30028 invalid order"
        self.metadata = (("x-tracking-id", "tracking-error"),)


class FakeMarketDataService:
    def __init__(self) -> None:
        self.get_candles_kwargs: dict[str, object] | None = None

    def get_candles(self, **kwargs: object) -> Box:
        self.get_candles_kwargs = dict(kwargs)
        return Box(
            candles=[
                Box(
                    instrument_uid="uid-sber",
                    figi="figi-sber",
                    time=datetime(2026, 6, 15, 7, 0, tzinfo=UTC),
                    open=Box(units=300, nano=100_000_000),
                    high=Box(units=301, nano=0),
                    low=Box(units=299, nano=900_000_000),
                    close=Box(units=300, nano=500_000_000),
                    volume=120,
                    is_complete=True,
                )
            ]
        )


class FakeOrdersService:
    def __init__(self, *, raise_on_post: bool = False) -> None:
        self.raise_on_post = raise_on_post
        self.post_order_kwargs: dict[str, object] | None = None

    def post_order(self, **kwargs: object) -> Box:
        self.post_order_kwargs = dict(kwargs)
        if self.raise_on_post:
            raise FakeSdkException()
        return Box(
            order_id="exchange-order-1",
            order_request_id=str(kwargs["order_id"]),
            execution_report_status=FakeExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
            lots_requested=kwargs["quantity"],
            lots_executed=0,
            instrument_uid=kwargs["instrument_id"],
            direction=kwargs["direction"],
            order_type=kwargs["order_type"],
            response_metadata=Box(tracking_id="tracking-post-order"),
        )


class FakeMarketDataStreamService:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def market_data_stream(self, request_iterator: Iterator[object]) -> Iterator[Box]:
        self.requests = list(request_iterator)
        return iter(
            [
                Box(
                    candle=Box(
                        instrument_uid="uid-sber",
                        figi="figi-sber",
                        time=datetime(2026, 6, 15, 7, 0, tzinfo=UTC),
                        interval=FakeSubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                        open=Box(units=300, nano=0),
                        high=Box(units=301, nano=0),
                        low=Box(units=299, nano=0),
                        close=Box(units=300, nano=500_000_000),
                        volume=50,
                    )
                )
            ]
        )


class FakeOrdersStreamService:
    def __init__(self) -> None:
        self.request: object | None = None

    def order_state_stream(self, *, request: object) -> Iterator[Box]:
        self.request = request
        return iter(
            [
                Box(
                    order_state=Box(
                        account_id="account-1",
                        order_id="exchange-order-1",
                        order_request_id="request-order-1",
                        execution_report_status=FakeExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
                        instrument_uid="uid-sber",
                        lots_requested=1,
                        lots_executed=1,
                        lots_left=0,
                        trades=[],
                    )
                )
            ]
        )


class FakeServices:
    def __init__(self, *, raise_on_post: bool = False) -> None:
        self.market_data = FakeMarketDataService()
        self.orders = FakeOrdersService(raise_on_post=raise_on_post)
        self.market_data_stream = FakeMarketDataStreamService()
        self.orders_stream = FakeOrdersStreamService()


class FakeServicesContext(AbstractContextManager[FakeServices]):
    def __init__(self, services: FakeServices) -> None:
        self._services = services

    def __enter__(self) -> FakeServices:
        return self._services

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def fake_sdk() -> SimpleNamespace:
    return SimpleNamespace(
        CandleInterval=FakeCandleInterval,
        CandleSource=FakeCandleSource,
        LastPriceType=FakeLastPriceType,
        OrderBookType=FakeOrderBookType,
        OrderDirection=FakeOrderDirection,
        StopOrderDirection=FakeStopOrderDirection,
        OrderType=FakeOrderType,
        TimeInForceType=FakeTimeInForceType,
        PriceType=FakePriceType,
        StopOrderType=FakeStopOrderType,
        StopOrderExpirationType=FakeStopOrderExpirationType,
        ExchangeOrderType=FakeExchangeOrderType,
        TakeProfitType=FakeTakeProfitType,
        OrderIdType=FakeOrderIdType,
        SubscriptionAction=FakeSubscriptionAction,
        SubscriptionInterval=FakeSubscriptionInterval,
        TradeSourceType=FakeTradeSourceType,
        Quotation=Box,
        MarketDataRequest=Box,
        SubscribeCandlesRequest=Box,
        CandleInstrument=Box,
        SubscribeOrderBookRequest=Box,
        OrderBookInstrument=Box,
        SubscribeLastPriceRequest=Box,
        LastPriceInstrument=Box,
        SubscribeInfoRequest=Box,
        InfoInstrument=Box,
        SubscribeTradesRequest=Box,
        TradeInstrument=Box,
        PingDelaySettings=Box,
        OrderStateStreamRequest=Box,
    )


def config() -> TBankBrokerConfig:
    return TBankBrokerConfig(
        environment=TBankEnvironment.SANDBOX,
        backoff_initial_seconds=0.0,
        backoff_max_seconds=0.0,
    )


def tokens() -> TBankTokenBundle:
    return TBankTokenBundle(
        full_access_token="full-access-token-for-tests",
        readonly_token="readonly-token-for-tests",
    )


def instrument() -> InstrumentRef:
    return InstrumentRef(
        instrument_id="MOEX:SBER",
        instrument_uid="uid-sber",
        ticker="SBER",
        class_code="TQBR",
    )


def services_factory(
    services: FakeServices,
) -> ServicesFactory:
    def factory(token: str, target: str, app_name: str) -> FakeServicesContext:
        assert token
        assert target
        assert app_name
        return FakeServicesContext(services)

    return factory


def test_gateway_uses_sdk_unary_client_and_generates_uuid_order_id() -> None:
    services = FakeServices()
    unary_client = TBankSdkUnaryClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )
    gateway = TBankBrokerGateway(
        config=config(),
        tokens=tokens(),
        unary_client=unary_client,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )

    response = asyncio.run(
        gateway.post_order(
            OrderPlacementRequest(
                account_id="account-1",
                instrument=instrument(),
                side="buy",
                order_type="limit",
                lot_qty=1,
                price=Decimal("300.10"),
                time_in_force="day",
                client_order_key="candidate-1:entry",
            )
        )
    )

    assert services.orders.post_order_kwargs is not None
    request_order_id = str(services.orders.post_order_kwargs["order_id"])
    assert str(UUID(request_order_id)) == request_order_id
    assert response.data["request_order_id"] == request_order_id
    assert response.data["exchange_order_id"] == "exchange-order-1"
    assert response.headers["x_tracking_id"] == "tracking-post-order"


def test_sdk_unary_get_candles_maps_closed_candle_payload() -> None:
    services = FakeServices()
    client = TBankSdkUnaryClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )

    result = asyncio.run(
        client.call_unary(
            "GetCandles",
            {
                "instrument": {
                    "instrument_id": "MOEX:SBER",
                    "instrument_uid": "uid-sber",
                },
                "interval": "5m",
                "from": "2026-06-15T07:00:00+00:00",
                "to": "2026-06-15T07:05:00+00:00",
            },
            metadata=auth_metadata("readonly-token-for-tests", "Resurt.Trading_2_0"),
            timeout_seconds=1.0,
        )
    )

    assert services.market_data.get_candles_kwargs is not None
    assert services.market_data.get_candles_kwargs["candle_source_type"] is (
        FakeCandleSource.CANDLE_SOURCE_EXCHANGE
    )
    candle = result.data["candles"][0]
    assert candle["instrument_id"] == "uid-sber"
    assert candle["timeframe"] == "5m"
    assert candle["is_closed"] is True
    assert candle["source"] == "tbank_get_candles"


def test_market_data_stream_subscribes_to_waiting_close_candles() -> None:
    services = FakeServices()
    client = TBankSdkStreamClient(
        config=config(),
        instruments=("uid-sber",),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )

    async def first_event() -> JsonPayload:
        async for event in client.open_market_data_stream(
            "candles",
            metadata=auth_metadata("readonly-token-for-tests", "Resurt.Trading_2_0"),
            ping_interval_seconds=30.0,
        ):
            assert event.event_type == "candle"
            return event.payload
        msg = "stream returned no events"
        raise AssertionError(msg)

    payload = asyncio.run(first_event())

    assert services.market_data_stream.requests
    stream_request = cast(Any, services.market_data_stream.requests[0])
    candles_request = stream_request.subscribe_candles_request
    assert candles_request.waiting_close is True
    assert candles_request.candle_source_type is FakeCandleSource.CANDLE_SOURCE_EXCHANGE
    assert payload["is_closed"] is True
    assert payload["source"] == "tbank_waiting_close_stream"


def test_order_state_stream_maps_own_order_state_events() -> None:
    services = FakeServices()
    client = TBankSdkStreamClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )

    async def first_event() -> JsonPayload:
        async for event in client.open_order_state_stream(
            "account-1",
            metadata=auth_metadata("readonly-token-for-tests", "Resurt.Trading_2_0"),
            ping_interval_seconds=30.0,
        ):
            assert event.event_type == "user_order_state"
            return event.payload
        msg = "stream returned no events"
        raise AssertionError(msg)

    payload = asyncio.run(first_event())

    stream_request = cast(Any, services.orders_stream.request)
    assert stream_request is not None
    assert stream_request.accounts == ["account-1"]
    assert stream_request.ping_delay_millis == 30_000
    assert payload["exchange_order_id"] == "exchange-order-1"
    assert payload["request_order_id"] == "request-order-1"
    assert payload["broker_status"] == "filled"


def test_sdk_like_error_maps_details_code_and_metadata_headers() -> None:
    services = FakeServices(raise_on_post=True)
    unary_client = TBankSdkUnaryClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )
    gateway = TBankBrokerGateway(
        config=config(),
        tokens=tokens(),
        unary_client=unary_client,
        backoff=ExponentialBackoff(initial_seconds=0.0, max_seconds=0.0),
    )

    with pytest.raises(BrokerGatewayError) as exc_info:
        asyncio.run(
            gateway.post_order(
                OrderPlacementRequest(
                    account_id="account-1",
                    instrument=instrument(),
                    side="buy",
                    order_type="limit",
                    lot_qty=1,
                    price=Decimal("300.10"),
                    time_in_force="day",
                )
            )
        )

    assert exc_info.value.reason_code == "invalid_argument"
    assert exc_info.value.headers.tracking_id == "tracking-error"

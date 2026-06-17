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


class FakeTradingStatus(Enum):
    SECURITY_TRADING_STATUS_NORMAL_TRADING = 1
    SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING = 2


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


class FakeInstrumentIdType(Enum):
    INSTRUMENT_ID_TYPE_TICKER = 1


class FakeInstrumentType(Enum):
    INSTRUMENT_TYPE_SHARE = 1


class FakeStatus(Enum):
    INVALID_ARGUMENT = 1


class FakeTradeDirection(Enum):
    TRADE_DIRECTION_BUY = 1
    TRADE_DIRECTION_SELL = 2


class FakeSdkException(Exception):
    def __init__(self) -> None:
        super().__init__("30028 invalid order")
        self.code = FakeStatus.INVALID_ARGUMENT
        self.details = "30028 invalid order"
        self.metadata = (("x-tracking-id", "tracking-error"),)


class FakeMarketDataService:
    def __init__(self) -> None:
        self.get_candles_kwargs: dict[str, object] | None = None
        self.get_trading_status_kwargs: dict[str, object] | None = None
        self.get_last_prices_kwargs: dict[str, object] | None = None
        self.get_order_book_kwargs: dict[str, object] | None = None

    def get_trading_status(self, **kwargs: object) -> Box:
        self.get_trading_status_kwargs = dict(kwargs)
        return Box(
            instrument_uid="uid-sber",
            figi="figi-sber",
            trading_status=FakeTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING,
            api_trade_available_flag=True,
            limit_order_available_flag=True,
            market_order_available_flag=False,
            bestprice_order_available_flag=True,
        )

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

    def get_last_prices(self, **kwargs: object) -> Box:
        self.get_last_prices_kwargs = dict(kwargs)
        return Box(
            last_prices=[
                Box(
                    instrument_uid="uid-sber",
                    figi="figi-sber",
                    price=Box(units=300, nano=500_000_000),
                    time=datetime(2026, 6, 15, 7, 1, tzinfo=UTC),
                )
            ]
        )

    def get_order_book(self, **kwargs: object) -> Box:
        self.get_order_book_kwargs = dict(kwargs)
        return Box(
            instrument_uid="uid-sber",
            figi="figi-sber",
            depth=20,
            orderbook_ts=datetime(2026, 6, 15, 7, 1, tzinfo=UTC),
            bids=[Box(price=Box(units=300, nano=0), quantity=10)],
            asks=[Box(price=Box(units=301, nano=0), quantity=8)],
            is_consistent=True,
        )


class FakeInstrumentsService:
    def __init__(self) -> None:
        self.trading_schedules_kwargs: dict[str, object] | None = None
        self.share_by_kwargs: dict[str, object] | None = None

    def trading_schedules(self, **kwargs: object) -> Box:
        self.trading_schedules_kwargs = dict(kwargs)
        trading_date = datetime(2026, 6, 15, tzinfo=UTC)
        return Box(
            exchanges=[
                Box(
                    days=[
                        Box(
                            is_trading_day=True,
                            date=trading_date,
                            premarket_start_time=datetime(2026, 6, 15, 7, 0, tzinfo=UTC),
                            premarket_end_time=datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
                            start_time=datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
                            end_time=datetime(2026, 6, 15, 18, 50, tzinfo=UTC),
                            evening_start_time=datetime(2026, 6, 15, 19, 0, tzinfo=UTC),
                            evening_end_time=datetime(2026, 6, 15, 23, 50, tzinfo=UTC),
                        )
                    ]
                )
            ]
        )

    def share_by(self, **kwargs: object) -> Box:
        self.share_by_kwargs = dict(kwargs)
        ticker = str(kwargs["id"])
        return Box(
            instrument=Box(
                figi=f"figi-{ticker.lower()}",
                uid=f"uid-{ticker.lower()}",
                ticker=ticker,
                class_code=str(kwargs["class_code"]),
                name=ticker,
                lot=10,
                currency="rub",
                min_price_increment=Box(units=0, nano=10_000_000),
                api_trade_available_flag=True,
                buy_available_flag=True,
                sell_available_flag=True,
                short_enabled_flag=ticker != "GAZP",
                weekend_flag=False,
                exchange="MOEX",
            )
        )


class FakeOrdersService:
    def __init__(self, *, raise_on_post: bool = False) -> None:
        self.raise_on_post = raise_on_post
        self.post_order_kwargs: dict[str, object] | None = None
        self.cancel_order_kwargs: dict[str, object] | None = None
        self.get_order_state_kwargs: dict[str, object] | None = None
        self.get_orders_kwargs: dict[str, object] | None = None

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

    def cancel_order(self, **kwargs: object) -> Box:
        self.cancel_order_kwargs = dict(kwargs)
        return Box(
            time=datetime(2026, 6, 15, 7, 2, tzinfo=UTC),
            response_metadata=Box(tracking_id="tracking-cancel-order"),
        )

    def get_order_state(self, **kwargs: object) -> Box:
        self.get_order_state_kwargs = dict(kwargs)
        return _fake_order_state(
            order_id="exchange-order-1",
            order_request_id=str(kwargs["order_id"]),
            status=FakeExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
        )

    def get_orders(self, **kwargs: object) -> Box:
        self.get_orders_kwargs = dict(kwargs)
        return Box(
            orders=[
                _fake_order_state(
                    order_id="exchange-order-1",
                    order_request_id="request-order-1",
                    status=FakeExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
                )
            ]
        )


class FakeOperationsService:
    def __init__(self) -> None:
        self.get_portfolio_kwargs: dict[str, object] | None = None
        self.get_positions_kwargs: dict[str, object] | None = None

    def get_portfolio(self, **kwargs: object) -> Box:
        self.get_portfolio_kwargs = dict(kwargs)
        return Box(
            total_amount_portfolio=Box(units=30_000, nano=0),
            expected_yield=Box(units=125, nano=500_000_000),
            total_amount_shares=Box(units=30_000, nano=0),
            total_amount_bonds=Box(units=0, nano=0),
            total_amount_etf=Box(units=0, nano=0),
            total_amount_currencies=Box(units=0, nano=0),
            total_amount_futures=Box(units=0, nano=0),
            available_margin=Box(units=10_000, nano=0),
            positions=[
                Box(
                    figi="figi-sber",
                    instrument_uid="uid-sber",
                    position_uid="position-sber",
                    instrument_type="share",
                    quantity_lots=Box(units=10, nano=0),
                    quantity=Box(units=10, nano=0),
                    average_position_price=Box(units=299, nano=500_000_000),
                    current_price=Box(units=300, nano=0),
                    expected_yield=Box(units=5, nano=0),
                    blocked_lots=Box(units=0, nano=0),
                    short_enabled_flag=True,
                )
            ],
        )

    def get_positions(self, **kwargs: object) -> Box:
        self.get_positions_kwargs = dict(kwargs)
        return Box(
            securities=[
                Box(
                    figi="figi-sber",
                    instrument_uid="uid-sber",
                    position_uid="position-sber",
                    instrument_type="share",
                    balance=10,
                    blocked=0,
                    exchange_blocked=False,
                    short_enabled_flag=True,
                )
            ],
            futures=[],
            options=[],
            money=[],
            blocked=[],
            limits_loading_in_progress=False,
        )


class FakeUsersService:
    def __init__(self) -> None:
        self.get_accounts_called = False

    def get_accounts(self) -> Box:
        self.get_accounts_called = True
        return Box(
            accounts=[
                Box(
                    id="account-1",
                    name="Sandbox",
                    type="broker",
                    status="open",
                    access_level="full_access",
                    opened_date=datetime(2026, 1, 1, tzinfo=UTC),
                    closed_date=None,
                )
            ]
        )


class FakeStopOrdersService:
    def __init__(self) -> None:
        self.post_stop_order_kwargs: dict[str, object] | None = None

    def post_stop_order(self, **kwargs: object) -> Box:
        self.post_stop_order_kwargs = dict(kwargs)
        return Box(
            stop_order_id="stop-order-1",
            order_request_id=str(kwargs["order_id"]),
            response_metadata=Box(tracking_id="tracking-stop-order"),
        )


class FakeMarketDataStreamService:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def market_data_stream(self, request_iterator: Iterator[object]) -> Iterator[Box]:
        self.requests = list(request_iterator)
        request = cast(Any, self.requests[0])
        if hasattr(request, "subscribe_order_book_request"):
            return iter(
                [
                    Box(
                        orderbook=Box(
                            instrument_uid="uid-sber",
                            figi="figi-sber",
                            depth=20,
                            orderbook_ts=datetime(2026, 6, 15, 7, 1, tzinfo=UTC),
                            bids=[Box(price=Box(units=300, nano=0), quantity=10)],
                            asks=[Box(price=Box(units=301, nano=0), quantity=8)],
                        )
                    )
                ]
            )
        if hasattr(request, "subscribe_last_price_request"):
            return iter(
                [
                    Box(
                        last_price=Box(
                            instrument_uid="uid-sber",
                            figi="figi-sber",
                            price=Box(units=300, nano=500_000_000),
                            time=datetime(2026, 6, 15, 7, 1, tzinfo=UTC),
                        )
                    )
                ]
            )
        if hasattr(request, "subscribe_info_request"):
            return iter(
                [
                    Box(
                        trading_status=Box(
                            instrument_uid="uid-sber",
                            figi="figi-sber",
                            trading_status=FakeTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING,
                            limit_order_available_flag=True,
                            market_order_available_flag=True,
                            time=datetime(2026, 6, 15, 7, 1, tzinfo=UTC),
                        )
                    )
                ]
            )
        if hasattr(request, "subscribe_trades_request"):
            return iter(
                [
                    Box(
                        trade=Box(
                            instrument_uid="uid-sber",
                            figi="figi-sber",
                            price=Box(units=300, nano=250_000_000),
                            quantity=3,
                            direction=FakeTradeDirection.TRADE_DIRECTION_BUY,
                            time=datetime(2026, 6, 15, 7, 1, tzinfo=UTC),
                        )
                    )
                ]
            )
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
        self.instruments = FakeInstrumentsService()
        self.market_data = FakeMarketDataService()
        self.orders = FakeOrdersService(raise_on_post=raise_on_post)
        self.operations = FakeOperationsService()
        self.users = FakeUsersService()
        self.stop_orders = FakeStopOrdersService()
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
        TradingStatus=FakeTradingStatus,
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
        InstrumentIdType=FakeInstrumentIdType,
        InstrumentType=FakeInstrumentType,
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


def _fake_order_state(
    *,
    order_id: str,
    order_request_id: str,
    status: FakeExecutionReportStatus,
) -> Box:
    return Box(
        order_id=order_id,
        order_request_id=order_request_id,
        execution_report_status=status,
        lots_requested=1,
        lots_executed=1 if status is FakeExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL else 0,
        instrument_uid="uid-sber",
        figi="figi-sber",
        direction=FakeOrderDirection.ORDER_DIRECTION_BUY,
        order_type=FakeOrderType.ORDER_TYPE_LIMIT,
        order_date=datetime(2026, 6, 15, 7, 2, tzinfo=UTC),
        stages=[
            Box(
                trade_id="fill-1",
                price=Box(units=300, nano=0),
                quantity=1,
                execution_time=datetime(2026, 6, 15, 7, 3, tzinfo=UTC),
            )
        ],
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


def test_sdk_unary_maps_market_payload_shapes() -> None:
    services = FakeServices()
    client = TBankSdkUnaryClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )
    metadata = auth_metadata("readonly-token-for-tests", "Resurt.Trading_2_0")

    schedules = asyncio.run(
        client.call_unary(
            "TradingSchedules",
            {
                "exchange": "MOEX",
                "from": "2026-06-15T00:00:00+00:00",
                "to": "2026-06-16T00:00:00+00:00",
            },
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    trading_status = asyncio.run(
        client.call_unary(
            "GetTradingStatus",
            {"instrument": {"instrument_uid": "uid-sber"}},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    last_prices = asyncio.run(
        client.call_unary(
            "GetLastPrices",
            {"instruments": [{"instrument_uid": "uid-sber"}]},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    order_book = asyncio.run(
        client.call_unary(
            "GetOrderBook",
            {"instrument": {"instrument_uid": "uid-sber"}, "depth": 20},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )

    assert len(schedules.data["windows"]) == 3
    assert services.instruments.trading_schedules_kwargs == {
        "exchange": "MOEX",
        "from_": datetime(2026, 6, 15, tzinfo=UTC),
        "to": datetime(2026, 6, 16, tzinfo=UTC),
    }
    assert trading_status.data["instrument_id"] == "uid-sber"
    assert trading_status.data["api_trade_available"] is True
    assert last_prices.data["prices"][0]["price"] == "300.5"
    assert services.market_data.get_last_prices_kwargs is not None
    assert services.market_data.get_last_prices_kwargs["last_price_type"] is (
        FakeLastPriceType.LAST_PRICE_EXCHANGE
    )
    assert order_book.data["bids"][0]["price"] == "300"
    assert order_book.data["asks"][0]["quantity_lots"] == "8"


def test_sdk_unary_resolves_share_instruments_by_ticker() -> None:
    services = FakeServices()
    client = TBankSdkUnaryClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )
    metadata = auth_metadata("readonly-token-for-tests", "Resurt.Trading_2_0")

    resolved = asyncio.run(
        client.call_unary(
            "ResolveInstruments",
            {"tickers": ["SBER"], "class_code": "TQBR"},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )

    assert services.instruments.share_by_kwargs == {
        "id_type": FakeInstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
        "class_code": "TQBR",
        "id": "SBER",
    }
    instrument_payload = resolved.data["instruments"][0]
    assert instrument_payload["instrument_id"] == "uid-sber"
    assert instrument_payload["instrument_uid"] == "uid-sber"
    assert instrument_payload["ticker"] == "SBER"
    assert instrument_payload["lot_size"] == 10
    assert instrument_payload["min_price_increment"] == "0.01"


def test_sdk_unary_maps_portfolio_positions_and_accounts_payload_shapes() -> None:
    services = FakeServices()
    client = TBankSdkUnaryClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )
    metadata = auth_metadata("readonly-token-for-tests", "Resurt.Trading_2_0")

    portfolio = asyncio.run(
        client.call_unary(
            "GetPortfolio",
            {"account_id": "account-1"},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    positions = asyncio.run(
        client.call_unary(
            "GetPositions",
            {"account_id": "account-1"},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    accounts = asyncio.run(
        client.call_unary(
            "GetAccounts",
            {},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )

    assert services.operations.get_portfolio_kwargs == {"account_id": "account-1"}
    assert services.operations.get_positions_kwargs == {"account_id": "account-1"}
    assert services.users.get_accounts_called is True
    assert portfolio.data["total_amount_portfolio"] == "30000"
    assert portfolio.data["positions"][0]["instrument_id"] == "uid-sber"
    assert portfolio.data["positions"][0]["position_side"] == "long"
    assert portfolio.data["positions"][0]["exposure"] == "3000"
    assert positions.data["positions"][0]["qty_lots"] == "10"
    assert positions.data["positions"][0]["short_available"] is True
    assert accounts.data["accounts"][0]["account_id"] == "account-1"


def test_sdk_unary_maps_order_lifecycle_payload_shapes() -> None:
    services = FakeServices()
    client = TBankSdkUnaryClient(
        config=config(),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )
    metadata = auth_metadata("full-access-token-for-tests", "Resurt.Trading_2_0")

    cancel_response = asyncio.run(
        client.call_unary(
            "CancelOrder",
            {
                "account_id": "account-1",
                "exchange_order_id": "exchange-order-1",
                "request_order_id": None,
            },
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    order_state = asyncio.run(
        client.call_unary(
            "GetOrderState",
            {
                "account_id": "account-1",
                "exchange_order_id": None,
                "request_order_id": "request-order-1",
            },
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    orders = asyncio.run(
        client.call_unary(
            "GetOrders",
            {"account_id": "account-1"},
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )
    stop_order = asyncio.run(
        client.call_unary(
            "PostStopOrder",
            {
                "account_id": "account-1",
                "instrument": {"instrument_uid": "uid-sber"},
                "side": "sell",
                "stop_order_type": "stop_loss",
                "lot_qty": 1,
                "stop_price": "299.50",
                "price": "299.40",
                "expiration_type": "good_till_cancel",
                "expire_date": None,
                "request_order_id": "request-stop-order-1",
            },
            metadata=metadata,
            timeout_seconds=1.0,
        )
    )

    assert services.orders.cancel_order_kwargs is not None
    assert services.orders.cancel_order_kwargs["order_id_type"] is (
        FakeOrderIdType.ORDER_ID_TYPE_EXCHANGE
    )
    assert cancel_response.data["broker_status"] == "cancelled"
    assert services.orders.get_order_state_kwargs is not None
    assert services.orders.get_order_state_kwargs["order_id_type"] is (
        FakeOrderIdType.ORDER_ID_TYPE_REQUEST
    )
    assert order_state.data["broker_status"] == "filled"
    assert order_state.data["fills"][0]["broker_fill_id"] == "fill-1"
    assert orders.data["orders"][0]["broker_status"] == "posted"
    assert services.stop_orders.post_stop_order_kwargs is not None
    assert services.stop_orders.post_stop_order_kwargs["direction"] is (
        FakeStopOrderDirection.STOP_ORDER_DIRECTION_SELL
    )
    assert stop_order.data["exchange_order_id"] == "stop-order-1"
    assert stop_order.headers["x-tracking-id"] == "tracking-stop-order"


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


@pytest.mark.parametrize(
    ("stream_name", "request_attr", "event_type", "payload_key"),
    (
        ("order_book", "subscribe_order_book_request", "order_book", "bids"),
        ("last_prices", "subscribe_last_price_request", "last_price", "price"),
        ("trading_status", "subscribe_info_request", "trading_status", "status"),
        ("market_trades", "subscribe_trades_request", "market_trade", "quantity_lots"),
    ),
)
def test_market_data_stream_maps_non_candle_stream_payloads(
    stream_name: str,
    request_attr: str,
    event_type: str,
    payload_key: str,
) -> None:
    services = FakeServices()
    client = TBankSdkStreamClient(
        config=config(),
        instruments=("uid-sber",),
        sdk_module=fake_sdk(),
        services_factory=services_factory(services),
    )

    async def first_event() -> JsonPayload:
        async for event in client.open_market_data_stream(
            stream_name,
            metadata=auth_metadata("readonly-token-for-tests", "Resurt.Trading_2_0"),
            ping_interval_seconds=30.0,
        ):
            assert event.event_type == event_type
            return event.payload
        msg = "stream returned no events"
        raise AssertionError(msg)

    payload = asyncio.run(first_event())

    assert services.market_data_stream.requests
    request = cast(Any, services.market_data_stream.requests[0])
    subscription_request = getattr(request, request_attr)
    assert subscription_request.subscription_action is (
        FakeSubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE
    )
    assert payload["instrument_id"] == "uid-sber"
    assert payload_key in payload
    if stream_name == "market_trades":
        assert subscription_request.trade_source is FakeTradeSourceType.TRADE_SOURCE_EXCHANGE
        assert subscription_request.with_open_interest is False


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

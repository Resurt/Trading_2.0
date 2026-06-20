from __future__ import annotations

import asyncio
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    InstrumentRef,
    TradingSchedulesRequest,
    TradingStatusRequest,
)
from trade_core.session import TradingSessionPreflightConfig, TradingSessionPreflightService

MSK = ZoneInfo("Europe/Moscow")


def msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def instrument() -> InstrumentRef:
    return InstrumentRef(
        instrument_id="MOEX:SBER",
        instrument_uid="uid-sber",
        ticker="SBER",
        class_code="TQBR",
    )


class FakePreflightGateway:
    def __init__(
        self,
        *,
        windows: list[dict[str, object]] | None = None,
        schedule_error: bool = False,
        api_trade_available: bool = True,
    ) -> None:
        self.windows = windows
        self.schedule_error = schedule_error
        self.api_trade_available = api_trade_available
        self.trading_status_calls: list[TradingStatusRequest] = []

    async def trading_schedules(
        self,
        request: TradingSchedulesRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        if self.schedule_error:
            raise RuntimeError("TradingSchedules unavailable")
        return BrokerUnaryResponse(
            method_name="TradingSchedules",
            data={"windows": list(self.windows or [])},
        )

    async def get_trading_status(
        self,
        request: TradingStatusRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.trading_status_calls.append(request)
        return BrokerUnaryResponse(
            method_name="GetTradingStatus",
            data={
                "instrument_id": request.instrument.instrument_id,
                "trading_status": "normal_trading",
                "api_trade_available": self.api_trade_available,
            },
        )


def test_saturday_fallback_window_is_weekend_continuous_trading() -> None:
    gateway = FakePreflightGateway(schedule_error=True, api_trade_available=True)

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 20, 11),
            )
        )
    )

    assert result.market_open is True
    assert result.session_type == "weekend"
    assert result.session_phase == "continuous_trading"
    assert result.source == "fallback_weekend_time_rules"
    assert "fallback_schedule_used" in result.warnings


def test_saturday_after_weekend_window_is_expected_closed() -> None:
    gateway = FakePreflightGateway(schedule_error=True)

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(now=msk(2026, 6, 20, 22))
        )
    )

    assert result.market_open is False
    assert result.market_closed_expected is True
    assert result.reason_code == "weekend_session_closed"
    assert result.next_session_at is not None


def test_sunday_outside_trading_window_is_expected_closed() -> None:
    gateway = FakePreflightGateway(schedule_error=True)

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(now=msk(2026, 6, 21, 8))
        )
    )

    assert result.market_open is False
    assert result.market_closed_expected is True
    assert result.session_type == "weekend"


def test_weekday_fallback_sessions_are_classified() -> None:
    gateway = FakePreflightGateway(schedule_error=True)
    service = TradingSessionPreflightService(cast(BrokerGateway, gateway))

    morning = asyncio.run(service.run(TradingSessionPreflightConfig(now=msk(2026, 6, 22, 8))))
    main = asyncio.run(service.run(TradingSessionPreflightConfig(now=msk(2026, 6, 22, 11))))
    evening = asyncio.run(service.run(TradingSessionPreflightConfig(now=msk(2026, 6, 22, 20))))

    assert morning.session_type == "weekday_morning"
    assert main.session_type == "weekday_main"
    assert evening.session_type == "weekday_evening"


def test_broker_weekend_schedule_overrides_weekday_label() -> None:
    gateway = FakePreflightGateway(
        windows=[
            {
                "session_type": "weekday_main",
                "session_phase": "continuous_trading",
                "start_at": "2026-06-20T10:00:00+03:00",
                "end_at": "2026-06-20T19:00:00+03:00",
                "trading_date": "2026-06-20",
                "calendar_date": "2026-06-20",
            }
        ]
    )

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 20, 11),
            )
        )
    )

    assert result.market_open is True
    assert result.session_type == "weekend"
    assert result.source == "broker_trading_schedules"


def test_broker_non_trading_day_does_not_fall_back_to_time_rules() -> None:
    gateway = FakePreflightGateway(windows=[])

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 20, 11),
            )
        )
    )

    assert result.market_open is False
    assert result.market_closed_expected is True
    assert result.source == "broker_trading_schedules"

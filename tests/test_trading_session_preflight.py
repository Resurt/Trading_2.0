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


def instrument(ticker: str = "SBER") -> InstrumentRef:
    return InstrumentRef(
        instrument_id=f"MOEX:{ticker}",
        instrument_uid=f"uid-{ticker.lower()}",
        ticker=ticker,
        class_code="TQBR",
    )


class FakePreflightGateway:
    def __init__(
        self,
        *,
        windows: list[dict[str, object]] | None = None,
        schedule_error: bool = False,
        api_trade_available: bool = True,
        trading_status: str = "normal_trading",
        api_trade_available_by_ticker: dict[str, bool] | None = None,
        trading_status_by_ticker: dict[str, str] | None = None,
        status_errors: set[str] | None = None,
    ) -> None:
        self.windows = windows
        self.schedule_error = schedule_error
        self.api_trade_available = api_trade_available
        self.trading_status = trading_status
        self.api_trade_available_by_ticker = api_trade_available_by_ticker or {}
        self.trading_status_by_ticker = trading_status_by_ticker or {}
        self.status_errors = status_errors or set()
        self.schedule_requests: list[TradingSchedulesRequest] = []
        self.trading_status_calls: list[TradingStatusRequest] = []

    async def trading_schedules(
        self,
        request: TradingSchedulesRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.schedule_requests.append(request)
        if self.schedule_error:
            raise RuntimeError("TradingSchedules INVALID_ARGUMENT 30003")
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
        ticker = request.instrument.ticker or request.instrument.instrument_id or "unknown"
        if ticker in self.status_errors:
            raise ValueError(f"GetTradingStatus unavailable for {ticker}")
        return BrokerUnaryResponse(
            method_name="GetTradingStatus",
            data={
                "instrument_id": request.instrument.instrument_id,
                "trading_status": self.trading_status_by_ticker.get(
                    ticker,
                    self.trading_status,
                ),
                "api_trade_available": self.api_trade_available_by_ticker.get(
                    ticker,
                    self.api_trade_available,
                ),
            },
        )


class InvalidArgument30003(RuntimeError):
    error_code = "invalid_argument"


def test_saturday_fallback_window_is_weekend_continuous_trading() -> None:
    gateway = FakePreflightGateway(schedule_error=True, api_trade_available=True)

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 13, 11),
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
            TradingSessionPreflightConfig(now=msk(2026, 6, 13, 22))
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
            TradingSessionPreflightConfig(now=msk(2026, 6, 14, 8))
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


def test_trading_schedules_request_does_not_start_before_current_date() -> None:
    gateway = FakePreflightGateway()
    now = msk(2026, 6, 22, 20)

    asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=now,
            )
        )
    )

    assert gateway.schedule_requests[0].from_.date() == now.date()


def test_schedule_30003_and_all_status_unavailable_blocks_collection() -> None:
    gateway = FakePreflightGateway(schedule_error=True, status_errors={"SBER"})

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 22, 20),
            )
        )
    )

    assert result.market_open is False
    assert result.data_only_collection_allowed is False
    assert result.reason_code == "broker_status_unavailable"
    assert result.source == "fallback_time_rules"
    assert result.schedule_source == "tbank_error"
    assert result.schedule_error_code == "30003"
    assert result.fallback_used is True
    assert result.status_success_count == 0
    assert result.status_error_count == 1
    assert result.working_instruments == ()
    assert result.blocked_instruments[0]["reason_code"] == "broker_status_unavailable"


def test_schedule_30003_prefers_numeric_error_code_over_invalid_argument() -> None:
    gateway = FakePreflightGateway(schedule_error=True, status_errors={"SBER"})

    async def invalid_argument_schedule(
        request: TradingSchedulesRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        raise InvalidArgument30003("TradingSchedules INVALID_ARGUMENT 30003")

    gateway.trading_schedules = invalid_argument_schedule  # type: ignore[method-assign]

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 22, 20),
            )
        )
    )

    assert result.schedule_error_code == "30003"


def test_schedule_30003_and_some_statuses_open_returns_working_instruments() -> None:
    gateway = FakePreflightGateway(
        schedule_error=True,
        status_errors={"GAZP"},
        api_trade_available_by_ticker={"SBER": True},
    )

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument("SBER"), instrument("GAZP")),
                now=msk(2026, 6, 22, 20),
            )
        )
    )

    assert result.market_open is True
    assert result.data_only_collection_allowed is True
    assert result.schedule_error_code == "30003"
    assert result.working_instruments == ("MOEX:SBER",)
    assert [item["instrument_id"] for item in result.blocked_instruments] == ["MOEX:GAZP"]


def test_broker_schedule_missing_evening_uses_status_fallback_window() -> None:
    gateway = FakePreflightGateway(
        windows=[
            {
                "session_type": "weekday_main",
                "session_phase": "continuous_trading",
                "start_at": "2026-06-22T07:00:00+00:00",
                "end_at": "2026-06-22T15:54:59+00:00",
                "trading_date": "2026-06-22",
                "calendar_date": "2026-06-22",
            }
        ],
        api_trade_available=True,
        trading_status="normal_trading",
    )

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument("SBER"), instrument("GAZP")),
                now=msk(2026, 6, 22, 22),
            )
        )
    )

    assert result.market_open is True
    assert result.data_only_collection_allowed is True
    assert result.session_type == "weekday_evening"
    assert result.source == "broker_status_fallback_time_rules"
    assert result.schedule_source == "broker_trading_schedules_status_fallback"
    assert result.status_source == "GetTradingStatus"
    assert result.fallback_used is True
    assert result.working_instruments == ("MOEX:SBER", "MOEX:GAZP")
    assert "broker_schedule_missing_active_window" in result.warnings
    assert "broker_status_open_schedule_closed" in result.warnings


def test_broker_schedule_closed_with_open_status_warns_and_stays_closed() -> None:
    gateway = FakePreflightGateway(windows=[], api_trade_available=True)

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 22, 11),
            )
        )
    )

    assert result.market_open is False
    assert result.data_only_collection_allowed is False
    assert result.reason_code == "no_trading_window"
    assert result.source == "broker_trading_schedules"
    assert "broker_status_open_schedule_closed" in result.warnings


def test_broker_weekend_schedule_overrides_weekday_label() -> None:
    gateway = FakePreflightGateway(
        windows=[
            {
                "session_type": "weekday_main",
                "session_phase": "continuous_trading",
                "start_at": "2026-06-13T10:00:00+03:00",
                "end_at": "2026-06-13T19:00:00+03:00",
                "trading_date": "2026-06-13",
                "calendar_date": "2026-06-13",
            }
        ]
    )

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 13, 11),
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
                now=msk(2026, 6, 13, 11),
            )
        )
    )

    assert result.market_open is False
    assert result.market_closed_expected is True
    assert result.source == "broker_trading_schedules"


def test_broker_dealer_status_is_not_official_exchange_without_override() -> None:
    gateway = FakePreflightGateway(
        schedule_error=True,
        api_trade_available=True,
        trading_status="dealer_normal_trading",
    )

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 13, 11),
            )
        )
    )

    assert result.market_open is False
    assert result.market_closed_expected is True
    assert result.official_exchange_open is False
    assert result.official_exchange_closed is False
    assert result.api_trade_available_raw is True
    assert result.api_trade_available_for_exchange is False
    assert result.data_only_collection_allowed is False
    assert result.streams_for_display_allowed is True
    assert result.streams_for_calibration_allowed is False
    assert result.venue_type == "broker_otc"
    assert result.trading_mode == "broker_otc_only"
    assert result.reason_code == "broker_otc_only"


def test_official_moex_override_closes_2026_06_20() -> None:
    gateway = FakePreflightGateway(
        schedule_error=True,
        api_trade_available=True,
        trading_status="dealer_normal_trading",
    )

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
    assert result.official_exchange_closed is True
    assert result.reason_code == "moex_dsvd_cancelled_platform_update"
    assert result.api_trade_available_raw is True
    assert result.api_trade_available_for_exchange is False
    assert result.data_only_collection_allowed is False
    assert result.streams_for_calibration_allowed is False
    assert result.venue_type == "broker_otc"
    assert "official_exchange_closed_overrides_broker_status" in result.warnings


def test_official_moex_override_closes_2026_06_21() -> None:
    gateway = FakePreflightGateway(schedule_error=True, api_trade_available=True)

    result = asyncio.run(
        TradingSessionPreflightService(cast(BrokerGateway, gateway)).run(
            TradingSessionPreflightConfig(
                instruments=(instrument(),),
                now=msk(2026, 6, 21, 11),
            )
        )
    )

    assert result.market_open is False
    assert result.official_exchange_closed is True
    assert result.reason_code == "moex_dsvd_cancelled_platform_update"
    assert result.source == "official_moex_calendar_override"

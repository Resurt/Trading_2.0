"""Calendar and broker preflight for safe data-only live collection."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    InstrumentRef,
    LastPricesRequest,
    OrderBookRequest,
    TradingSchedulesRequest,
    TradingStatusRequest,
)
from trade_core.session.models import ScheduleWindow, TradingSchedule
from trade_core.session.moex_calendar import MoexCalendarService
from trading_common.enums import SessionPhase, SessionType

JsonPayload = dict[str, Any]
MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class TradingSessionPreflightConfig:
    """Inputs for a non-trading preflight check."""

    exchange: str = "MOEX"
    instruments: tuple[InstrumentRef, ...] = ()
    now: datetime | None = None
    allow_fallback_schedule: bool = True
    lookahead_days: int = 7


@dataclass(frozen=True, slots=True)
class TradingSessionPreflightResult:
    """Session readiness summary used before starting live data-only streams."""

    market_open: bool
    market_closed_expected: bool
    now_msk: datetime
    trading_date: date
    calendar_date: date
    session_type: str
    session_phase: str
    broker_trading_status: str
    api_trade_available: bool
    official_exchange_open: bool
    official_exchange_closed: bool
    official_exchange_reason_code: str | None
    official_exchange_source: str | None
    broker_stream_available: bool
    broker_otc_or_indicative_available: bool
    api_trade_available_raw: bool
    api_trade_available_for_exchange: bool
    quote_source_allowed_for_data_collection: bool
    data_only_collection_allowed: bool
    streams_for_display_allowed: bool
    streams_for_calibration_allowed: bool
    venue_type: str
    trading_mode: str
    broker_availability_ignored_because_official_exchange_closed: bool
    next_session_at: datetime | None
    next_session_type: str | None
    current_window_start_at: datetime | None
    current_window_end_at: datetime | None
    reason_code: str
    instruments_checked: tuple[str, ...]
    per_instrument_status: Mapping[str, JsonPayload]
    source: str
    schedule_source: str
    status_source: str
    schedule_error_code: str | None = None
    schedule_error_message: str | None = None
    status_error_count: int = 0
    status_success_count: int = 0
    fallback_used: bool = False
    cache_hit: bool = False
    cache_key: str | None = None
    requested_instruments: tuple[str, ...] = ()
    working_instruments: tuple[str, ...] = ()
    blocked_instruments: tuple[JsonPayload, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    market_window_open: bool = False
    trading_allowed: bool = False
    blocking_layer: str | None = None
    broker_schedule_windows_count: int | None = None
    fallback_reason: str | None = None
    market_data_probe_success_count: int = 0
    market_data_probe_error_count: int = 0
    market_data_probe: Mapping[str, JsonPayload] = field(default_factory=dict)

    def as_payload(self) -> JsonPayload:
        return {
            "market_open": self.market_open,
            "market_closed_expected": self.market_closed_expected,
            "now_msk": self.now_msk.isoformat(),
            "trading_date": self.trading_date.isoformat(),
            "calendar_date": self.calendar_date.isoformat(),
            "session_type": self.session_type,
            "session_phase": self.session_phase,
            "broker_trading_status": self.broker_trading_status,
            "api_trade_available": self.api_trade_available,
            "official_exchange_open": self.official_exchange_open,
            "official_exchange_closed": self.official_exchange_closed,
            "official_exchange_reason_code": self.official_exchange_reason_code,
            "official_exchange_source": self.official_exchange_source,
            "broker_stream_available": self.broker_stream_available,
            "broker_otc_or_indicative_available": self.broker_otc_or_indicative_available,
            "api_trade_available_raw": self.api_trade_available_raw,
            "api_trade_available_for_exchange": self.api_trade_available_for_exchange,
            "quote_source_allowed_for_data_collection": (
                self.quote_source_allowed_for_data_collection
            ),
            "data_only_collection_allowed": self.data_only_collection_allowed,
            "streams_for_display_allowed": self.streams_for_display_allowed,
            "streams_for_calibration_allowed": self.streams_for_calibration_allowed,
            "venue_type": self.venue_type,
            "trading_mode": self.trading_mode,
            "broker_availability_ignored_because_official_exchange_closed": (
                self.broker_availability_ignored_because_official_exchange_closed
            ),
            "next_session_at": (
                self.next_session_at.isoformat() if self.next_session_at else None
            ),
            "next_session_type": self.next_session_type,
            "current_window_start_at": (
                self.current_window_start_at.isoformat()
                if self.current_window_start_at
                else None
            ),
            "current_window_end_at": (
                self.current_window_end_at.isoformat()
                if self.current_window_end_at
                else None
            ),
            "reason_code": self.reason_code,
            "instruments_checked": list(self.instruments_checked),
            "per_instrument_status": dict(self.per_instrument_status),
            "source": self.source,
            "schedule_source": self.schedule_source,
            "status_source": self.status_source,
            "schedule_error_code": self.schedule_error_code,
            "schedule_error_message": self.schedule_error_message,
            "status_error_count": self.status_error_count,
            "status_success_count": self.status_success_count,
            "fallback_used": self.fallback_used,
            "cache_hit": self.cache_hit,
            "cache_key": self.cache_key,
            "requested_instruments": list(self.requested_instruments),
            "working_instruments": list(self.working_instruments),
            "blocked_instruments": list(self.blocked_instruments),
            "warnings": list(self.warnings),
            "market_window_open": self.market_window_open,
            "trading_allowed": self.trading_allowed,
            "blocking_layer": self.blocking_layer,
            "broker_schedule_windows_count": self.broker_schedule_windows_count,
            "fallback_reason": self.fallback_reason,
            "market_data_probe_success_count": self.market_data_probe_success_count,
            "market_data_probe_error_count": self.market_data_probe_error_count,
            "market_data_probe": dict(self.market_data_probe),
        }


@dataclass(frozen=True, slots=True)
class _ScheduleProbe:
    schedule: TradingSchedule
    source: str
    schedule_source: str
    schedule_error_code: str | None = None
    schedule_error_message: str | None = None
    fallback_used: bool = False
    warnings: tuple[str, ...] = ()
    broker_schedule_windows_count: int | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _MarketDataProbeResult:
    per_instrument: Mapping[str, JsonPayload]
    success_count: int
    error_count: int


class TradingSessionPreflightService:
    """Evaluate broker calendar/status before any live data-only smoke."""

    def __init__(
        self,
        broker_gateway: BrokerGateway,
        *,
        moex_calendar: MoexCalendarService | None = None,
    ) -> None:
        self._broker_gateway = broker_gateway
        self._moex_calendar = moex_calendar or MoexCalendarService()

    async def run(
        self,
        config: TradingSessionPreflightConfig | None = None,
    ) -> TradingSessionPreflightResult:
        cfg = config or TradingSessionPreflightConfig()
        now_msk = _ensure_msk(cfg.now or datetime.now(tz=MSK))
        official_decision = self._moex_calendar.decision(
            now_msk.date(),
            market="stock",
            now_msk=now_msk,
        )
        schedule_probe = await self._schedule(cfg, now_msk)
        schedule = schedule_probe.schedule
        source = schedule_probe.source
        schedule_source = schedule_probe.schedule_source
        fallback_used = schedule_probe.fallback_used
        warnings = schedule_probe.warnings
        current_window = schedule.active_window(now_msk)
        next_window = _next_window(schedule, now_msk)
        per_instrument = await self._instrument_statuses(cfg.instruments)

        status_values = [
            item.get("broker_trading_status")
            for item in per_instrument.values()
            if item.get("status_available") is True
        ]
        status_success_count = sum(
            1 for item in per_instrument.values() if item.get("status_available") is True
        )
        status_error_count = sum(
            1 for item in per_instrument.values() if item.get("status_available") is False
        )
        status_source = _status_source(
            requested=bool(cfg.instruments),
            success_count=status_success_count,
            error_count=status_error_count,
        )
        broker_status = (
            "mixed"
            if len(set(str(item) for item in status_values)) > 1
            else str(status_values[0])
            if status_values
            else "unknown"
        )
        api_trade_available_raw = any(
            item.get("api_trade_available") is True for item in per_instrument.values()
        )
        broker_otc_or_indicative_available = _broker_otc_or_indicative_available(
            status_values
        )
        broker_stream_available = bool(status_values) and (
            api_trade_available_raw or broker_otc_or_indicative_available
        )
        status_unavailable = bool(cfg.instruments) and not status_values
        fallback_schedule = _fallback_schedule(
            now_msk,
            lookahead_days=cfg.lookahead_days,
        )
        fallback_window = fallback_schedule.active_window(now_msk)
        fallback_window_open = (
            fallback_window is not None
            and _public_phase(fallback_window.session_phase) == "continuous_trading"
        )
        should_probe_market_data = (
            bool(cfg.instruments)
            and not official_decision.official_exchange_closed
            and fallback_window_open
            and (
                current_window is None
                or status_unavailable
                or not api_trade_available_raw
            )
        )
        market_data_probe = (
            await self._market_data_probe(cfg.instruments)
            if should_probe_market_data
            else _empty_market_data_probe(cfg.instruments)
        )
        per_instrument = _merge_market_data_probe(
            per_instrument,
            market_data_probe.per_instrument,
        )
        fallback_reason = schedule_probe.fallback_reason
        if (
            current_window is None
            and not official_decision.official_exchange_closed
            and not broker_otc_or_indicative_available
            and fallback_window_open
        ):
            current_window = fallback_window
            next_window = _next_window(fallback_schedule, now_msk)
            source = "broker_status_fallback_time_rules"
            schedule_source = (
                "broker_trading_schedules_status_fallback"
                if schedule_probe.schedule_source == "broker_trading_schedules"
                else schedule_probe.schedule_source
            )
            fallback_used = True
            fallback_reason = "broker_schedule_missing_active_window"
            fallback_warning_items = [
                *warnings,
                "broker_schedule_missing_active_window",
                "fallback_schedule_used",
            ]
            if api_trade_available_raw:
                fallback_warning_items.append("broker_status_open_schedule_closed")
            if status_unavailable and market_data_probe.success_count:
                fallback_warning_items.append("market_data_probe_used_without_status")
            if status_unavailable and not market_data_probe.success_count:
                fallback_warning_items.append("market_data_probe_unavailable")
            warnings = tuple(dict.fromkeys(fallback_warning_items))
        if market_data_probe.success_count:
            warnings = tuple(
                dict.fromkeys(
                    (
                        *warnings,
                        "market_data_probe_available",
                    )
                )
            )

        session_type = (
            _session_type_value(current_window.session_type)
            if current_window is not None
            else ("weekend" if now_msk.weekday() >= 5 else "closed")
        )
        session_phase = (
            _public_phase(current_window.session_phase)
            if current_window is not None
            else "closed"
        )
        trading_date = current_window.trading_date if current_window else now_msk.date()
        calendar_date = (
            current_window.calendar_date or current_window.start_at.date()
            if current_window
            else now_msk.date()
        )

        exchange_session_open_by_schedule = (
            current_window is not None
            and session_phase == "continuous_trading"
        )
        market_window_open = (
            exchange_session_open_by_schedule
            and not official_decision.official_exchange_closed
        )
        broker_exchange_status_available = (
            bool(status_values) and not broker_otc_or_indicative_available
        )
        readonly_market_data_available = (
            api_trade_available_raw
            or market_data_probe.success_count > 0
            or not cfg.instruments
        )
        exchange_status_or_probe_available = (
            broker_exchange_status_available
            or market_data_probe.success_count > 0
            or not cfg.instruments
        )
        official_exchange_closed = official_decision.official_exchange_closed
        official_exchange_open = (
            not official_exchange_closed
            and market_window_open
            and exchange_status_or_probe_available
            and (
                official_decision.official_exchange_open
                or official_decision.reason_code
                in {"no_local_override", "default_weekday_calendar"}
            )
        )
        api_trade_available_for_exchange = (
            readonly_market_data_available
            and official_exchange_open
            and not official_exchange_closed
        )
        market_open = official_exchange_open and api_trade_available_for_exchange
        market_closed_expected = not market_open and (
            current_window is None
            or session_phase in {"closed", "break"}
            or (bool(status_values) and not api_trade_available_for_exchange)
            or official_exchange_closed
        )
        if official_exchange_closed:
            session_phase = "closed"
            market_closed_expected = True
        reason_code = _reason_code(
            market_open=market_open,
            current_window=current_window,
            now_msk=now_msk,
            status_unavailable=status_unavailable,
            api_trade_available=api_trade_available_for_exchange,
            status_values=status_values,
            market_data_probe_success_count=market_data_probe.success_count,
            source="official_moex_calendar_override" if official_exchange_closed else source,
        )
        if official_exchange_closed:
            reason_code = official_decision.reason_code
        elif broker_otc_or_indicative_available and not official_exchange_open:
            reason_code = "broker_otc_only"
            market_closed_expected = True
        if reason_code == "broker_status_unavailable":
            market_closed_expected = False
        if (
            not official_exchange_closed
            and not market_open
            and api_trade_available_raw
            and (current_window is None or session_phase != "continuous_trading")
        ):
            warnings = tuple(
                dict.fromkeys((*warnings, "broker_status_open_schedule_closed"))
            )
        quote_source_allowed_for_data_collection = market_open and official_exchange_open
        per_instrument = _annotate_collection_allowed(
            per_instrument,
            market_open=market_open,
            reason_code=reason_code,
        )
        working_instruments = tuple(
            key
            for key, item in per_instrument.items()
            if item.get("collection_allowed") is True
        )
        blocked_instruments = _blocked_instruments(per_instrument)
        data_only_collection_allowed = quote_source_allowed_for_data_collection and (
            bool(working_instruments) or not cfg.instruments
        )
        streams_for_calibration_allowed = quote_source_allowed_for_data_collection
        streams_for_display_allowed = broker_stream_available or market_open
        venue_type = _venue_type(
            official_exchange_open=official_exchange_open,
            official_exchange_closed=official_exchange_closed,
            broker_otc_or_indicative_available=broker_otc_or_indicative_available,
            broker_stream_available=broker_stream_available,
            current_window=current_window,
        )
        trading_mode = _trading_mode(
            market_open=market_open,
            official_exchange_closed=official_exchange_closed,
            venue_type=venue_type,
            session_type=session_type,
        )
        if official_exchange_closed:
            warnings = tuple(
                dict.fromkeys(
                    (
                        *warnings,
                        "official_exchange_closed_overrides_broker_status",
                    )
                )
            )
            source = "official_moex_calendar_override"
            if official_decision.next_possible_session_at is not None:
                next_window = ScheduleWindow(
                    session_type=SessionType.WEEKDAY_MORNING,
                    session_phase=SessionPhase.CONTINUOUS_TRADING,
                    start_at=official_decision.next_possible_session_at,
                    end_at=official_decision.next_possible_session_at + timedelta(hours=3),
                    trading_date=official_decision.next_possible_session_at.date(),
                    calendar_date=official_decision.next_possible_session_at.date(),
                )

        return TradingSessionPreflightResult(
            market_open=market_open,
            market_closed_expected=market_closed_expected,
            now_msk=now_msk,
            trading_date=trading_date,
            calendar_date=calendar_date,
            session_type=session_type,
            session_phase=session_phase,
            broker_trading_status=broker_status,
            api_trade_available=api_trade_available_for_exchange,
            official_exchange_open=official_exchange_open,
            official_exchange_closed=official_exchange_closed,
            official_exchange_reason_code=(
                official_decision.reason_code if official_decision.is_exception_day else None
            ),
            official_exchange_source=official_decision.source,
            broker_stream_available=broker_stream_available,
            broker_otc_or_indicative_available=broker_otc_or_indicative_available,
            api_trade_available_raw=api_trade_available_raw,
            api_trade_available_for_exchange=api_trade_available_for_exchange,
            quote_source_allowed_for_data_collection=(
                quote_source_allowed_for_data_collection
            ),
            data_only_collection_allowed=data_only_collection_allowed,
            streams_for_display_allowed=streams_for_display_allowed,
            streams_for_calibration_allowed=streams_for_calibration_allowed,
            venue_type=venue_type,
            trading_mode=trading_mode,
            broker_availability_ignored_because_official_exchange_closed=(
                official_exchange_closed and (api_trade_available_raw or broker_stream_available)
            ),
            next_session_at=next_window.start_at if next_window else None,
            next_session_type=(
                _session_type_value(next_window.session_type) if next_window else None
            ),
            current_window_start_at=current_window.start_at if current_window else None,
            current_window_end_at=current_window.end_at if current_window else None,
            reason_code=reason_code,
            instruments_checked=tuple(_instrument_key(item) for item in cfg.instruments),
            per_instrument_status=per_instrument,
            source=source,
            schedule_source=schedule_source,
            status_source=status_source,
            schedule_error_code=schedule_probe.schedule_error_code,
            schedule_error_message=schedule_probe.schedule_error_message,
            status_error_count=status_error_count,
            status_success_count=status_success_count,
            fallback_used=fallback_used,
            requested_instruments=tuple(_instrument_key(item) for item in cfg.instruments),
            working_instruments=working_instruments,
            blocked_instruments=blocked_instruments,
            warnings=warnings,
            market_window_open=market_window_open,
            trading_allowed=False,
            blocking_layer=_blocking_layer(reason_code),
            broker_schedule_windows_count=schedule_probe.broker_schedule_windows_count,
            fallback_reason=fallback_reason,
            market_data_probe_success_count=market_data_probe.success_count,
            market_data_probe_error_count=market_data_probe.error_count,
            market_data_probe=market_data_probe.per_instrument,
        )

    async def _schedule(
        self,
        config: TradingSessionPreflightConfig,
        now_msk: datetime,
    ) -> _ScheduleProbe:
        schedule_error: Exception | None = None
        try:
            response = await self._broker_gateway.trading_schedules(
                TradingSchedulesRequest(
                    exchange=config.exchange,
                    from_=now_msk,
                    to=now_msk + timedelta(days=config.lookahead_days),
                )
            )
            schedule = _schedule_from_response(response)
            if isinstance(response.data.get("windows"), list):
                return _ScheduleProbe(
                    schedule=schedule,
                    source="broker_trading_schedules",
                    schedule_source="broker_trading_schedules",
                    broker_schedule_windows_count=len(schedule.windows),
                )
        except Exception as exc:
            schedule_error = exc
            if not config.allow_fallback_schedule:
                return _ScheduleProbe(
                    schedule=TradingSchedule(windows=()),
                    source="broker_schedule_unavailable",
                    schedule_source="tbank_error",
                    schedule_error_code=_exception_error_code(exc),
                    schedule_error_message=_exception_error_message(exc),
                    broker_schedule_windows_count=None,
                )
        if not config.allow_fallback_schedule:
            return _ScheduleProbe(
                schedule=TradingSchedule(windows=()),
                source="broker_schedule_unavailable",
                schedule_source="broker_schedule_unavailable",
                broker_schedule_windows_count=0,
            )
        source = (
            "fallback_weekend_time_rules"
            if now_msk.weekday() >= 5
            else "fallback_time_rules"
        )
        warnings = ["fallback_schedule_used"]
        if schedule_error is not None:
            error_code = _exception_error_code(schedule_error)
            if error_code:
                warnings.append(f"broker_schedule_error_{error_code}")
            return _ScheduleProbe(
                schedule=_fallback_schedule(now_msk, lookahead_days=config.lookahead_days),
                source=source,
                schedule_source="tbank_error",
                schedule_error_code=error_code,
                schedule_error_message=_exception_error_message(schedule_error),
                fallback_used=True,
                warnings=tuple(dict.fromkeys(warnings)),
                broker_schedule_windows_count=None,
                fallback_reason="broker_schedule_error",
            )
        return _ScheduleProbe(
            schedule=_fallback_schedule(now_msk, lookahead_days=config.lookahead_days),
            source=source,
            schedule_source=source,
            fallback_used=True,
            warnings=tuple(warnings),
            broker_schedule_windows_count=None,
            fallback_reason="broker_schedule_missing",
        )

    async def _instrument_statuses(
        self,
        instruments: tuple[InstrumentRef, ...],
    ) -> dict[str, JsonPayload]:
        semaphore = asyncio.Semaphore(4)

        async def fetch(instrument: InstrumentRef) -> tuple[str, JsonPayload]:
            key = _instrument_key(instrument)
            try:
                async with semaphore:
                    response = await self._broker_gateway.get_trading_status(
                        TradingStatusRequest(instrument=instrument)
                    )
            except Exception as exc:
                error_code = _exception_error_code(exc) or type(exc).__name__
                return key, {
                    "status_available": False,
                    "instrument_id": key,
                    "ticker": instrument.ticker,
                    "broker_status": "unknown",
                    "api_trade_available": False,
                    "broker_trading_status": "unknown",
                    "status_source": "error",
                    "status_error_code": error_code,
                    "status_error_message": _exception_error_message(exc),
                    "collection_allowed": False,
                    "blocked_reason": "broker_status_unavailable",
                    "reason_code": "broker_status_unavailable",
                    "error_code": error_code,
                    "error_message": _exception_error_message(exc),
                }
            return key, _status_payload(response, instrument=instrument, instrument_id=key)

        pairs = await asyncio.gather(*(fetch(instrument) for instrument in instruments))
        return dict(pairs)

    async def _market_data_probe(
        self,
        instruments: tuple[InstrumentRef, ...],
    ) -> _MarketDataProbeResult:
        if not instruments:
            return _MarketDataProbeResult(per_instrument={}, success_count=0, error_count=0)
        started = perf_counter()
        probe: dict[str, JsonPayload] = {
            _instrument_key(instrument): {
                "instrument_id": _instrument_key(instrument),
                "ticker": instrument.ticker,
                "last_price_available": False,
                "order_book_available": False,
                "market_data_available": False,
                "latency_ms": None,
                "source": "market_data_probe",
                "error_code": None,
                "error_message": None,
            }
            for instrument in instruments
        }
        try:
            response = await asyncio.wait_for(
                self._broker_gateway.get_last_prices(LastPricesRequest(instruments=instruments)),
                timeout=5.0,
            )
            matched = _mark_last_price_probe(probe, instruments, response.data)
            if matched == 0 and response.data.get("prices"):
                for payload in probe.values():
                    payload["last_price_available"] = True
        except Exception as exc:
            for payload in probe.values():
                payload["error_code"] = _exception_error_code(exc) or type(exc).__name__
                payload["error_message"] = _exception_error_message(exc)

        selected = instruments[0]
        selected_key = _instrument_key(selected)
        try:
            response = await asyncio.wait_for(
                self._broker_gateway.get_order_book(
                    OrderBookRequest(instrument=selected, depth=10)
                ),
                timeout=5.0,
            )
            selected_probe = probe[selected_key]
            selected_probe["order_book_available"] = bool(
                response.data.get("bids") or response.data.get("asks")
            )
            if selected_probe["order_book_available"]:
                selected_probe["error_code"] = None
                selected_probe["error_message"] = None
        except Exception as exc:
            selected_probe = probe[selected_key]
            if selected_probe.get("error_code") is None:
                selected_probe["error_code"] = _exception_error_code(exc) or type(exc).__name__
                selected_probe["error_message"] = _exception_error_message(exc)

        latency_ms = round((perf_counter() - started) * 1000, 3)
        success_count = 0
        for payload in probe.values():
            market_data_available = bool(
                payload.get("last_price_available") or payload.get("order_book_available")
            )
            payload["market_data_available"] = market_data_available
            payload["latency_ms"] = latency_ms
            if market_data_available:
                success_count += 1
                payload["error_code"] = None
                payload["error_message"] = None
        return _MarketDataProbeResult(
            per_instrument=probe,
            success_count=success_count,
            error_count=len(probe) - success_count,
        )


def _schedule_from_response(response: BrokerUnaryResponse) -> TradingSchedule:
    raw_windows = response.data.get("windows")
    if not isinstance(raw_windows, list):
        return TradingSchedule(windows=())
    windows: list[ScheduleWindow] = []
    for item in raw_windows:
        if not isinstance(item, Mapping):
            continue
        window = _window_from_mapping(item)
        if window is not None:
            windows.append(window)
    return TradingSchedule(windows=tuple(sorted(windows, key=lambda item: item.start_at)))


def _window_from_mapping(item: Mapping[str, Any]) -> ScheduleWindow | None:
    try:
        start_at = _ensure_msk(datetime.fromisoformat(str(item["start_at"])))
        end_at = _ensure_msk(datetime.fromisoformat(str(item["end_at"])))
        trading_date = date.fromisoformat(str(item["trading_date"]))
        calendar_date = date.fromisoformat(str(item.get("calendar_date", trading_date)))
        session_type = _normalize_session_type(str(item["session_type"]), calendar_date)
        session_phase = _normalize_session_phase(
            str(item.get("session_phase", "continuous_trading"))
        )
    except (KeyError, ValueError, TypeError):
        return None
    return ScheduleWindow(
        session_type=session_type,
        session_phase=session_phase,
        start_at=start_at,
        end_at=end_at,
        trading_date=trading_date,
        calendar_date=calendar_date,
    )


def _fallback_schedule(now_msk: datetime, *, lookahead_days: int) -> TradingSchedule:
    windows: list[ScheduleWindow] = []
    start_day = now_msk.date()
    for offset in range(lookahead_days + 1):
        day = start_day + timedelta(days=offset)
        if day.weekday() >= 5:
            windows.append(_window(day, SessionType.WEEKEND, time(10, 0), time(19, 0)))
            continue
        windows.extend(
            (
                _window(day, SessionType.WEEKDAY_MORNING, time(7, 0), time(10, 0)),
                _window(day, SessionType.WEEKDAY_MAIN, time(10, 0), time(18, 59)),
                _window(day, SessionType.WEEKDAY_EVENING, time(19, 0), time(23, 50)),
            )
        )
    return TradingSchedule(windows=tuple(windows))


def _schedule_has_calendar_date(schedule: TradingSchedule, calendar_date: date) -> bool:
    return any(
        (window.calendar_date or window.start_at.date()) == calendar_date
        for window in schedule.windows
    )


def _window(
    trading_date: date,
    session_type: SessionType,
    start_time: time,
    end_time: time,
) -> ScheduleWindow:
    return ScheduleWindow(
        session_type=session_type,
        session_phase=SessionPhase.CONTINUOUS_TRADING,
        start_at=datetime.combine(trading_date, start_time, tzinfo=MSK),
        end_at=datetime.combine(trading_date, end_time, tzinfo=MSK),
        trading_date=trading_date,
        calendar_date=trading_date,
    )


def _status_payload(
    response: BrokerUnaryResponse,
    *,
    instrument: InstrumentRef,
    instrument_id: str,
) -> JsonPayload:
    status = str(response.data.get("trading_status", response.data.get("status", "unknown")))
    normalized_status = status.lower().removeprefix("security_trading_status_")
    api_trade_available = bool(response.data.get("api_trade_available", False))
    return {
        "status_available": True,
        "instrument_id": str(response.data.get("instrument_id") or instrument_id),
        "ticker": instrument.ticker,
        "broker_status": normalized_status,
        "broker_trading_status": normalized_status,
        "api_trade_available": api_trade_available,
        "limit_order_available": bool(response.data.get("limit_order_available", False)),
        "market_order_available": bool(response.data.get("market_order_available", False)),
        "status_source": response.method_name,
        "status_error_code": None,
        "status_error_message": None,
        "collection_allowed": False,
        "blocked_reason": None,
        "source": response.method_name,
    }


def _status_source(*, requested: bool, success_count: int, error_count: int) -> str:
    if not requested:
        return "not_requested"
    if success_count and error_count:
        return "GetTradingStatus_partial"
    if success_count:
        return "GetTradingStatus"
    if error_count:
        return "GetTradingStatus_error"
    return "unknown"


def _empty_market_data_probe(instruments: tuple[InstrumentRef, ...]) -> _MarketDataProbeResult:
    return _MarketDataProbeResult(
        per_instrument={
            _instrument_key(instrument): {
                "instrument_id": _instrument_key(instrument),
                "ticker": instrument.ticker,
                "last_price_available": False,
                "order_book_available": False,
                "market_data_available": False,
                "latency_ms": None,
                "source": "not_run",
                "error_code": None,
                "error_message": None,
            }
            for instrument in instruments
        },
        success_count=0,
        error_count=0,
    )


def _merge_market_data_probe(
    statuses: Mapping[str, JsonPayload],
    probe: Mapping[str, JsonPayload],
) -> dict[str, JsonPayload]:
    merged: dict[str, JsonPayload] = {}
    for key, item in statuses.items():
        payload = dict(item)
        probe_payload = dict(probe.get(key) or {})
        payload["last_price_available"] = bool(
            probe_payload.get("last_price_available", False)
        )
        payload["order_book_available"] = bool(
            probe_payload.get("order_book_available", False)
        )
        payload["market_data_available"] = bool(
            probe_payload.get("market_data_available", False)
        )
        payload["market_data_probe_error_code"] = probe_payload.get("error_code")
        payload["market_data_probe_error_message"] = probe_payload.get("error_message")
        payload["market_data_probe_source"] = probe_payload.get("source")
        payload["market_data_probe_latency_ms"] = probe_payload.get("latency_ms")
        merged[key] = payload
    return merged


def _mark_last_price_probe(
    probe: Mapping[str, JsonPayload],
    instruments: tuple[InstrumentRef, ...],
    payload: Mapping[str, Any],
) -> int:
    lookup: dict[str, str] = {}
    for instrument in instruments:
        key = _instrument_key(instrument)
        for candidate in (
            instrument.instrument_uid,
            instrument.figi,
            instrument.instrument_id,
            key,
        ):
            if candidate:
                lookup[str(candidate)] = key
    matched = 0
    prices = payload.get("prices")
    if not isinstance(prices, list):
        return matched
    mutable_probe = dict(probe)
    for item in prices:
        if not isinstance(item, Mapping):
            continue
        price = item.get("price")
        if price in (None, "", "0", "0.0", "0.000000000"):
            continue
        item_key = None
        for candidate in (
            item.get("instrument_uid"),
            item.get("figi"),
            item.get("instrument_id"),
        ):
            if candidate is not None and str(candidate) in lookup:
                item_key = lookup[str(candidate)]
                break
        if item_key is not None and item_key in mutable_probe:
            mutable_probe[item_key]["last_price_available"] = True
            matched += 1
    return matched


def _annotate_collection_allowed(
    statuses: Mapping[str, JsonPayload],
    *,
    market_open: bool,
    reason_code: str,
) -> dict[str, JsonPayload]:
    annotated: dict[str, JsonPayload] = {}
    for key, item in statuses.items():
        payload = dict(item)
        collection_allowed = (
            market_open
            and (
                (
                    payload.get("status_available") is True
                    and payload.get("api_trade_available") is True
                )
                or payload.get("market_data_available") is True
            )
        )
        payload["collection_allowed"] = collection_allowed
        payload["blocked_reason"] = (
            None
            if collection_allowed
            else _instrument_blocked_reason(
                payload,
                market_open=market_open,
                reason_code=reason_code,
            )
        )
        annotated[key] = payload
    return annotated


def _instrument_blocked_reason(
    payload: Mapping[str, Any],
    *,
    market_open: bool,
    reason_code: str,
) -> str:
    if not market_open:
        return reason_code
    if payload.get("market_data_available") is True:
        return "unknown"
    if payload.get("status_available") is False:
        return "broker_status_unavailable"
    if payload.get("api_trade_available") is not True:
        status = str(payload.get("broker_trading_status") or payload.get("broker_status") or "")
        if _is_closed_status_value(status):
            return "broker_status_closed"
        return "instrument_not_tradeable"
    return "unknown"


def _blocked_instruments(statuses: Mapping[str, JsonPayload]) -> tuple[JsonPayload, ...]:
    blocked: list[JsonPayload] = []
    for key, item in statuses.items():
        if item.get("collection_allowed") is True:
            continue
        blocked.append(
            {
                "instrument_id": str(item.get("instrument_id") or key),
                "ticker": item.get("ticker"),
                "broker_status": item.get("broker_status")
                or item.get("broker_trading_status"),
                "api_trade_available": item.get("api_trade_available"),
                "status_source": item.get("status_source") or item.get("source"),
                "status_error_code": item.get("status_error_code") or item.get("error_code"),
                "status_error_message": item.get("status_error_message")
                or item.get("error_message"),
                "reason_code": item.get("blocked_reason") or item.get("reason_code"),
            }
        )
    return tuple(blocked)


def _broker_otc_or_indicative_available(status_values: list[object]) -> bool:
    normalized = [str(item).lower() for item in status_values]
    return any(
        "dealer" in item
        or "otc" in item
        or "indicative" in item
        or item.startswith("dealer_")
        for item in normalized
    )


def _venue_type(
    *,
    official_exchange_open: bool,
    official_exchange_closed: bool,
    broker_otc_or_indicative_available: bool,
    broker_stream_available: bool,
    current_window: ScheduleWindow | None,
) -> str:
    if official_exchange_open:
        return "official_exchange"
    if broker_otc_or_indicative_available:
        return "broker_otc"
    if broker_stream_available:
        return "broker_indicative"
    if current_window is None:
        return "stale_local"
    return "unknown"


def _trading_mode(
    *,
    market_open: bool,
    official_exchange_closed: bool,
    venue_type: str,
    session_type: str,
) -> str:
    if market_open and venue_type == "official_exchange":
        return "weekend_exchange" if session_type == "weekend" else "standard_exchange"
    if venue_type == "broker_otc":
        return "broker_otc_only"
    if venue_type == "broker_indicative":
        return "indicative_only"
    if official_exchange_closed:
        return "exchange_closed"
    return "unknown"


def _reason_code(
    *,
    market_open: bool,
    current_window: ScheduleWindow | None,
    now_msk: datetime,
    status_unavailable: bool,
    api_trade_available: bool,
    status_values: list[object],
    market_data_probe_success_count: int,
    source: str,
) -> str:
    if market_open:
        return "market_open"
    if source == "broker_schedule_unavailable":
        return "broker_schedule_unavailable"
    if current_window is None:
        return "weekend_session_closed" if now_msk.weekday() >= 5 else "no_trading_window"
    if status_unavailable:
        if market_data_probe_success_count <= 0:
            return "broker_status_and_market_data_unavailable"
        return "broker_status_unavailable"
    if status_values and not api_trade_available:
        if all(_is_closed_status_value(item) for item in status_values):
            return "broker_status_closed"
        return "instrument_not_tradeable"
    if current_window.session_phase is SessionPhase.BREAK:
        return "market_closed_expected"
    return "market_closed_expected"


def _blocking_layer(reason_code: str) -> str:
    if reason_code == "market_open":
        return "none"
    if reason_code in {"weekend_session_closed", "no_trading_window", "market_closed_expected"}:
        return "schedule"
    if reason_code in {
        "broker_status_unavailable",
        "broker_preflight_timeout",
        "broker_status_and_market_data_unavailable",
        "broker_status_closed",
        "instrument_not_tradeable",
        "broker_otc_only",
    }:
        return "broker"
    if reason_code in {"official_exchange_closed", "moex_dsvd_cancelled_platform_update"}:
        return "official_calendar"
    return "unknown"


def _next_window(schedule: TradingSchedule, now_msk: datetime) -> ScheduleWindow | None:
    for window in sorted(schedule.windows, key=lambda item: item.start_at):
        if window.start_at > now_msk:
            return window
    return None


def _normalize_session_type(raw: str, calendar_date: date) -> SessionType:
    if calendar_date.weekday() >= 5:
        return SessionType.WEEKEND
    try:
        return SessionType(raw)
    except ValueError:
        return SessionType.WEEKDAY_MAIN


def _normalize_session_phase(raw: str) -> SessionPhase:
    aliases = {
        "auction": SessionPhase.OPENING_AUCTION,
        "opening": SessionPhase.OPENING_AUCTION,
        "closing": SessionPhase.CLOSING_AUCTION,
        "continuous": SessionPhase.CONTINUOUS_TRADING,
        "normal_trading": SessionPhase.CONTINUOUS_TRADING,
    }
    normalized = raw.strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    try:
        return SessionPhase(normalized)
    except ValueError:
        return SessionPhase.CLOSED


def _public_phase(phase: SessionPhase) -> str:
    if phase in {SessionPhase.OPENING_AUCTION, SessionPhase.CLOSING_AUCTION}:
        return "auction"
    if phase is SessionPhase.CONTINUOUS_TRADING:
        return "continuous_trading"
    if phase is SessionPhase.BREAK:
        return "break"
    if phase is SessionPhase.CLOSED:
        return "closed"
    return "unknown"


def _session_type_value(value: SessionType) -> str:
    return value.value


def _instrument_key(instrument: InstrumentRef) -> str:
    return instrument.instrument_id or instrument.instrument_uid or instrument.ticker or "unknown"


def _is_closed_status_value(value: object) -> bool:
    normalized = str(value).lower()
    return (
        "closed" in normalized
        or "break" in normalized
        or normalized
        in {
            "not_available_for_trading",
            "not_available",
            "unknown",
        }
    )


def _exception_error_code(exc: Exception) -> str | None:
    text = f"{type(exc).__name__}: {exc}"
    match = re.search(r"\b\d{4,6}\b", text)
    if match:
        return match.group(0)
    for nested in (getattr(exc, "original_error", None), getattr(exc, "__cause__", None)):
        if isinstance(nested, Exception):
            nested_code = _exception_error_code(nested)
            if nested_code:
                return nested_code
    for attr in ("error_code", "code", "reason_code"):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            text = str(value)
            match = re.search(r"\b\d{4,6}\b", text)
            return match.group(0) if match else text
    return None


def _exception_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _ensure_msk(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MSK)
    return value.astimezone(MSK)

"""Calendar and broker preflight for safe data-only live collection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    InstrumentRef,
    TradingSchedulesRequest,
    TradingStatusRequest,
)
from trade_core.session.models import ScheduleWindow, TradingSchedule
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
    next_session_at: datetime | None
    next_session_type: str | None
    current_window_start_at: datetime | None
    current_window_end_at: datetime | None
    reason_code: str
    instruments_checked: tuple[str, ...]
    per_instrument_status: Mapping[str, JsonPayload]
    source: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

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
            "warnings": list(self.warnings),
        }


class TradingSessionPreflightService:
    """Evaluate broker calendar/status before any live data-only smoke."""

    def __init__(self, broker_gateway: BrokerGateway) -> None:
        self._broker_gateway = broker_gateway

    async def run(
        self,
        config: TradingSessionPreflightConfig | None = None,
    ) -> TradingSessionPreflightResult:
        cfg = config or TradingSessionPreflightConfig()
        now_msk = _ensure_msk(cfg.now or datetime.now(tz=MSK))
        schedule, source, warnings = await self._schedule(cfg, now_msk)
        current_window = schedule.active_window(now_msk)
        next_window = _next_window(schedule, now_msk)
        per_instrument = await self._instrument_statuses(cfg.instruments)

        status_values = [
            item.get("broker_trading_status")
            for item in per_instrument.values()
            if item.get("status_available") is True
        ]
        broker_status = (
            "mixed"
            if len(set(str(item) for item in status_values)) > 1
            else str(status_values[0])
            if status_values
            else "unknown"
        )
        api_trade_available = any(
            item.get("api_trade_available") is True for item in per_instrument.values()
        )
        status_unavailable = bool(cfg.instruments) and not status_values

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

        market_open = (
            current_window is not None
            and session_phase == "continuous_trading"
            and (api_trade_available or not cfg.instruments)
            and not status_unavailable
        )
        market_closed_expected = not market_open and (
            current_window is None
            or session_phase in {"closed", "break"}
            or (bool(status_values) and not api_trade_available)
        )
        reason_code = _reason_code(
            market_open=market_open,
            current_window=current_window,
            now_msk=now_msk,
            status_unavailable=status_unavailable,
            api_trade_available=api_trade_available,
            status_values=status_values,
            source=source,
        )
        if reason_code == "broker_status_unavailable":
            market_closed_expected = False

        return TradingSessionPreflightResult(
            market_open=market_open,
            market_closed_expected=market_closed_expected,
            now_msk=now_msk,
            trading_date=trading_date,
            calendar_date=calendar_date,
            session_type=session_type,
            session_phase=session_phase,
            broker_trading_status=broker_status,
            api_trade_available=api_trade_available,
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
            warnings=warnings,
        )

    async def _schedule(
        self,
        config: TradingSessionPreflightConfig,
        now_msk: datetime,
    ) -> tuple[TradingSchedule, str, tuple[str, ...]]:
        try:
            response = await self._broker_gateway.trading_schedules(
                TradingSchedulesRequest(
                    exchange=config.exchange,
                    from_=now_msk - timedelta(days=1),
                    to=now_msk + timedelta(days=config.lookahead_days),
                )
            )
            schedule = _schedule_from_response(response)
            if isinstance(response.data.get("windows"), list):
                return schedule, "broker_trading_schedules", ()
        except Exception:
            if not config.allow_fallback_schedule:
                return TradingSchedule(windows=()), "broker_schedule_unavailable", ()
        if not config.allow_fallback_schedule:
            return TradingSchedule(windows=()), "broker_schedule_unavailable", ()
        source = (
            "fallback_weekend_time_rules"
            if now_msk.weekday() >= 5
            else "fallback_time_rules"
        )
        return (
            _fallback_schedule(now_msk, lookahead_days=config.lookahead_days),
            source,
            ("fallback_schedule_used",),
        )

    async def _instrument_statuses(
        self,
        instruments: tuple[InstrumentRef, ...],
    ) -> dict[str, JsonPayload]:
        statuses: dict[str, JsonPayload] = {}
        for instrument in instruments:
            key = _instrument_key(instrument)
            try:
                response = await self._broker_gateway.get_trading_status(
                    TradingStatusRequest(instrument=instrument)
                )
            except Exception as exc:
                statuses[key] = {
                    "status_available": False,
                    "api_trade_available": False,
                    "broker_trading_status": "unknown",
                    "reason_code": "broker_status_unavailable",
                    "error_code": type(exc).__name__,
                }
                continue
            statuses[key] = _status_payload(response, instrument_id=key)
        return statuses


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


def _status_payload(response: BrokerUnaryResponse, *, instrument_id: str) -> JsonPayload:
    status = str(response.data.get("trading_status", response.data.get("status", "unknown")))
    api_trade_available = bool(response.data.get("api_trade_available", False))
    return {
        "status_available": True,
        "instrument_id": str(response.data.get("instrument_id") or instrument_id),
        "broker_trading_status": status.lower().removeprefix("security_trading_status_"),
        "api_trade_available": api_trade_available,
        "limit_order_available": bool(response.data.get("limit_order_available", False)),
        "market_order_available": bool(response.data.get("market_order_available", False)),
        "source": response.method_name,
    }


def _reason_code(
    *,
    market_open: bool,
    current_window: ScheduleWindow | None,
    now_msk: datetime,
    status_unavailable: bool,
    api_trade_available: bool,
    status_values: list[object],
    source: str,
) -> str:
    if market_open:
        return "market_open"
    if source == "broker_schedule_unavailable":
        return "broker_schedule_unavailable"
    if current_window is None:
        return "weekend_session_closed" if now_msk.weekday() >= 5 else "no_trading_window"
    if status_unavailable:
        return "broker_status_unavailable"
    if status_values and not api_trade_available:
        return "instrument_not_tradeable"
    if current_window.session_phase is SessionPhase.BREAK:
        return "market_closed_expected"
    return "market_closed_expected"


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


def _ensure_msk(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MSK)
    return value.astimezone(MSK)

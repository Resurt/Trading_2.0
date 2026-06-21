"""Local official MOEX calendar overrides used as the top session gate.

The robot must not depend on internet access during runtime.  Known official
exchange exceptions are stored here and can be expanded by a future calendar
sync job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class MoexCalendarOverride:
    """A local official MOEX trading calendar exception."""

    calendar_date: date
    official_exchange_open: bool
    official_exchange_closed: bool
    session_type: str
    reason_code: str
    source: str
    message: str
    affected_markets: tuple[str, ...]
    exchange: str = "MOEX"
    market: str = "stock"
    next_possible_session_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MoexCalendarDecision:
    """Official exchange decision for a calendar date."""

    official_exchange_open: bool
    official_exchange_closed: bool
    exchange: str
    market: str
    calendar_date: date
    session_type: str
    reason_code: str
    source: str
    message: str
    next_possible_session_at: datetime | None
    affected_markets: tuple[str, ...]
    is_exception_day: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "official_exchange_open": self.official_exchange_open,
            "official_exchange_closed": self.official_exchange_closed,
            "exchange": self.exchange,
            "market": self.market,
            "calendar_date": self.calendar_date.isoformat(),
            "session_type": self.session_type,
            "reason_code": self.reason_code,
            "source": self.source,
            "message": self.message,
            "next_possible_session_at": (
                self.next_possible_session_at.isoformat()
                if self.next_possible_session_at
                else None
            ),
            "affected_markets": list(self.affected_markets),
            "is_exception_day": self.is_exception_day,
        }


_JUNE_2026_DSV_D_CANCELLED_MESSAGE = (
    "20-21 июня 2026: ДСВД на фондовом и срочном рынках Мосбиржи "
    "не проводится из-за планового обновления торгово-клиринговых платформ."
)


_OVERRIDES: dict[date, MoexCalendarOverride] = {
    date(2026, 6, 20): MoexCalendarOverride(
        calendar_date=date(2026, 6, 20),
        official_exchange_open=False,
        official_exchange_closed=True,
        session_type="weekend",
        affected_markets=("stock", "derivatives"),
        reason_code="moex_dsvd_cancelled_platform_update",
        source="official_moex_news_2026_06_17",
        message=_JUNE_2026_DSV_D_CANCELLED_MESSAGE,
        next_possible_session_at=datetime(2026, 6, 22, 7, 0, tzinfo=MSK),
    ),
    date(2026, 6, 21): MoexCalendarOverride(
        calendar_date=date(2026, 6, 21),
        official_exchange_open=False,
        official_exchange_closed=True,
        session_type="weekend",
        affected_markets=("stock", "derivatives"),
        reason_code="moex_dsvd_cancelled_platform_update",
        source="official_moex_news_2026_06_17",
        message=_JUNE_2026_DSV_D_CANCELLED_MESSAGE,
        next_possible_session_at=datetime(2026, 6, 22, 7, 0, tzinfo=MSK),
    ),
}


class MoexCalendarService:
    """Resolve official MOEX exchange availability for session preflight."""

    def decision(
        self,
        calendar_date: date,
        *,
        market: str = "stock",
        now_msk: datetime | None = None,
    ) -> MoexCalendarDecision:
        override = _OVERRIDES.get(calendar_date)
        if override is not None and (
            market == "unknown" or market in override.affected_markets
        ):
            return MoexCalendarDecision(
                official_exchange_open=override.official_exchange_open,
                official_exchange_closed=override.official_exchange_closed,
                exchange=override.exchange,
                market=market if market != "unknown" else override.market,
                calendar_date=calendar_date,
                session_type=override.session_type,
                reason_code=override.reason_code,
                source=override.source,
                message=override.message,
                next_possible_session_at=override.next_possible_session_at,
                affected_markets=override.affected_markets,
                is_exception_day=True,
            )

        session_type = "weekend" if calendar_date.weekday() >= 5 else "weekday"
        opened_by_default = calendar_date.weekday() < 5
        next_possible = _next_weekday_morning(calendar_date, now_msk=now_msk)
        return MoexCalendarDecision(
            official_exchange_open=opened_by_default,
            official_exchange_closed=False,
            exchange="MOEX",
            market=market,
            calendar_date=calendar_date,
            session_type=session_type,
            reason_code=(
                "default_weekday_calendar" if opened_by_default else "no_local_override"
            ),
            source="local_moex_calendar_rules",
            message=(
                "Обычный биржевой день MOEX."
                if opened_by_default
                else "Локальный календарь не содержит закрывающего override для этой даты."
            ),
            next_possible_session_at=next_possible,
            affected_markets=(market,),
            is_exception_day=False,
        )


def _next_weekday_morning(
    calendar_date: date,
    *,
    now_msk: datetime | None,
) -> datetime | None:
    start_date = calendar_date
    if now_msk is not None and now_msk.date() == calendar_date and now_msk.time() < time(7):
        return datetime.combine(calendar_date, time(7, 0), tzinfo=MSK)
    for offset in range(1, 8):
        candidate = start_date + timedelta(days=offset)
        if candidate.weekday() < 5:
            return datetime.combine(candidate, time(7, 0), tzinfo=MSK)
    return None

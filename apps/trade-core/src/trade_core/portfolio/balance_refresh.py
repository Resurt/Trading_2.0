"""Readonly broker balance refresh for dashboard state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import AccountsRequest, BrokerGateway, InstrumentRef
from trade_core.portfolio.service import PositionService
from trade_core.session import SessionEventContext
from trading_common.db.models import InstrumentRegistry
from trading_common.enums import SessionPhase, SessionType

JsonPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class BrokerBalanceRefreshResult:
    """Operator-facing readonly balance refresh result."""

    balance_refreshed: bool
    account_id_masked: str | None
    total_portfolio_value_rub: str | None
    available_cash_rub: str | None
    blocked_cash_rub: str | None
    expected_yield_rub: str | None
    free_collateral_rub: str | None
    last_balance_refresh_at: str | None
    balance_degraded: bool
    balance_degraded_reason_code: str | None
    positions_count: int = 0
    source: str = "broker_balance_refresh"

    def as_payload(self) -> JsonPayload:
        return {
            "balance_refreshed": self.balance_refreshed,
            "total_portfolio_value_rub": self.total_portfolio_value_rub,
            "available_cash_rub": self.available_cash_rub,
            "blocked_cash_rub": self.blocked_cash_rub,
            "expected_yield_rub": self.expected_yield_rub,
            "free_collateral_rub": self.free_collateral_rub,
            "account_id_masked": self.account_id_masked,
            "last_balance_refresh_at": self.last_balance_refresh_at,
            "balance_degraded": self.balance_degraded,
            "balance_degraded_reason_code": self.balance_degraded_reason_code,
            "positions_count": self.positions_count,
            "source": self.source,
        }


class BrokerBalanceRefreshService:
    """Refresh portfolio/account state without trading side effects."""

    def __init__(
        self,
        *,
        broker_gateway: BrokerGateway,
        session: Session,
        tracked_instruments: Sequence[InstrumentRef] | None = None,
    ) -> None:
        self._broker_gateway = broker_gateway
        self._session = session
        self._tracked_instruments = tuple(tracked_instruments or _enabled_instruments(session))

    async def refresh(
        self,
        *,
        account_id: str | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> BrokerBalanceRefreshResult:
        if dry_run:
            return _degraded(
                "dry_run_no_broker_calls",
                account_id_masked=_mask_account_id(account_id),
            )
        observed_at = now or datetime.now(tz=UTC)
        try:
            resolved_account_id, account_payload = await self._resolve_account(account_id)
        except Exception as exc:
            return _degraded(
                _reason_from_exception(exc, default="broker_accounts_unavailable"),
                account_id_masked=_mask_account_id(account_id),
            )
        if not self._tracked_instruments:
            return _degraded(
                "no_enabled_instruments_for_balance_snapshot",
                account_id_masked=_mask_account_id(resolved_account_id),
            )
        position_service = PositionService(
            broker_gateway=self._broker_gateway,
            session=self._session,
            session_context_provider=lambda _: _balance_session_context(observed_at),
            tracked_instruments=self._tracked_instruments,
        )
        try:
            refresh = await position_service.refresh_positions(
                resolved_account_id,
                reason="broker_balance_refresh",
                now=observed_at,
                account_payload=account_payload,
            )
        except Exception as exc:
            return _degraded(
                _reason_from_exception(exc, default="broker_balance_refresh_failed"),
                account_id_masked=_mask_account_id(resolved_account_id),
            )
        if not refresh.snapshots:
            return _degraded(
                "no_position_snapshots_written",
                account_id_masked=_mask_account_id(resolved_account_id),
            )
        payload = refresh.snapshots[0].snapshot_payload.get("broker_balance")
        if not isinstance(payload, dict):
            return _degraded(
                "broker_balance_payload_unavailable",
                account_id_masked=_mask_account_id(resolved_account_id),
            )
        return BrokerBalanceRefreshResult(
            balance_refreshed=True,
            account_id_masked=_string_or_none(payload.get("account_id_masked")),
            total_portfolio_value_rub=_string_or_none(payload.get("total_portfolio_value_rub")),
            available_cash_rub=_string_or_none(payload.get("available_cash_rub")),
            blocked_cash_rub=_string_or_none(payload.get("blocked_cash_rub")),
            expected_yield_rub=_string_or_none(payload.get("expected_yield_rub")),
            free_collateral_rub=_string_or_none(payload.get("free_collateral_rub")),
            last_balance_refresh_at=_string_or_none(payload.get("last_balance_refresh_at")),
            balance_degraded=False,
            balance_degraded_reason_code=None,
            positions_count=len(refresh.snapshots),
        )

    async def _resolve_account(self, account_id: str | None) -> tuple[str, JsonPayload]:
        response = await self._broker_gateway.get_accounts(AccountsRequest())
        accounts = response.data.get("accounts")
        if not isinstance(accounts, list):
            msg = "broker_accounts_payload_unavailable"
            raise RuntimeError(msg)
        normalized: list[JsonPayload] = [dict(item) for item in accounts if isinstance(item, dict)]
        if account_id:
            selected_account = next(
                (item for item in normalized if str(item.get("account_id") or "") == account_id),
                {"account_id": account_id},
            )
            return account_id, dict(selected_account)
        selected_existing = next(
            (item for item in normalized if str(item.get("account_id") or "").strip()),
            None,
        )
        if selected_existing is None:
            msg = "broker_accounts_empty"
            raise RuntimeError(msg)
        return str(selected_existing["account_id"]), dict(selected_existing)


def _enabled_instruments(session: Session) -> tuple[InstrumentRef, ...]:
    rows = session.execute(
        select(InstrumentRegistry)
        .where(InstrumentRegistry.is_enabled.is_(True))
        .order_by(InstrumentRegistry.ticker)
    ).scalars()
    return tuple(
        InstrumentRef(
            instrument_id=row.instrument_id,
            instrument_uid=row.instrument_uid,
            figi=row.figi,
            class_code=row.class_code,
            ticker=row.ticker,
        )
        for row in rows
    )


def _balance_session_context(moment: datetime) -> SessionEventContext:
    observed_at = moment.astimezone(UTC)
    return SessionEventContext(
        calendar_date=observed_at.date(),
        trading_date=observed_at.date(),
        session_type=SessionType.WEEKEND
        if observed_at.weekday() >= 5
        else SessionType.WEEKDAY_MAIN,
        session_phase=SessionPhase.CLOSED,
        micro_session_id="broker-balance-refresh",
        broker_trading_status="unknown",
    )


def _degraded(
    reason_code: str,
    *,
    account_id_masked: str | None = None,
) -> BrokerBalanceRefreshResult:
    return BrokerBalanceRefreshResult(
        balance_refreshed=False,
        account_id_masked=account_id_masked,
        total_portfolio_value_rub=None,
        available_cash_rub=None,
        blocked_cash_rub=None,
        expected_yield_rub=None,
        free_collateral_rub=None,
        last_balance_refresh_at=None,
        balance_degraded=True,
        balance_degraded_reason_code=reason_code,
    )


def _mask_account_id(account_id: str | None) -> str | None:
    if not account_id:
        return None
    if len(account_id) <= 6:
        return f"{account_id[:2]}***"
    return f"{account_id[:3]}***{account_id[-3:]}"


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _reason_from_exception(exc: Exception, *, default: str) -> str:
    text = str(exc).strip()
    if text and " " not in text and len(text) <= 96:
        return text
    return default

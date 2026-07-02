"""Instrument-specific commission policy helpers.

The module is intentionally side-effect free: it does not inspect broker state,
does not write execution rows, and only estimates the cost model from explicit
inputs. Future live/shadow execution paths can wire a real executed-trade counter
into the same service without changing the policy rules.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

DEFAULT_COMMISSION_BPS_PER_SIDE = Decimal("5")
T_TECHNOLOGIES_INSTRUMENT_ID = "MOEX:T"
T_TECHNOLOGIES_TICKER = "T"
T_TECHNOLOGIES_ISIN = "RU000A107UL4"
T_PRO_FREE_EXECUTED_TRADES_PER_DAY = 15
T_PRO_BLOCK_AFTER_FREE_QUOTA_ENV = "T_PRO_BLOCK_AFTER_FREE_QUOTA"


class ExecutedTradeCounter(Protocol):
    """Abstraction for future execution-path daily fill counting."""

    def executed_trades_today(
        self,
        *,
        account_id: str | None,
        instrument_id: str,
        trading_date: date,
    ) -> int:
        """Return executed trades/fills, not submitted order count or lot quantity."""


@dataclass(frozen=True, slots=True)
class StaticExecutedTradeCounter:
    """Test/research counter keyed by account, instrument, and trading date."""

    counts: dict[tuple[str | None, str, date], int] = field(default_factory=dict)

    def executed_trades_today(
        self,
        *,
        account_id: str | None,
        instrument_id: str,
        trading_date: date,
    ) -> int:
        return max(
            0,
            int(
                self.counts.get(
                    (account_id, _normalize_instrument_id(instrument_id), trading_date), 0
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class CommissionProfile:
    commission_profile_id: str
    instrument_ids: tuple[str, ...]
    pro_subscription_required: bool
    free_executed_trades_per_day: int
    free_commission_bps: Decimal
    fallback_commission_bps: Decimal
    execution_count_scope: str = "executed_trade"
    reset_timezone: str = "Europe/Moscow"
    fallback_commission_source: str = "project_default_assumed_commission_bps_per_side"

    def matches(self, instrument_id: str) -> bool:
        normalized = _normalize_instrument_id(instrument_id)
        return normalized in {_normalize_instrument_id(value) for value in self.instrument_ids}


@dataclass(frozen=True, slots=True)
class CommissionPolicyResult:
    instrument_id: str
    commission_profile_id: str
    commission_bps: Decimal
    fallback_commission_bps: Decimal
    fallback_commission_source: str
    pro_subscription_required: bool
    pro_subscription_active: bool | None
    free_executed_trades_per_day: int
    executed_trades_today: int
    free_quota_remaining_before_trade: int
    execution_count_scope: str
    reset_timezone: str
    reason_code: str
    block_new_entry_after_free_quota: bool = False

    @property
    def free_commission_applies(self) -> bool:
        return (
            self.commission_bps == Decimal("0") and self.reason_code == "t_pro_free_quota_available"
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "commission_profile_id": self.commission_profile_id,
            "commission_bps": str(self.commission_bps),
            "fallback_commission_bps": str(self.fallback_commission_bps),
            "fallback_commission_source": self.fallback_commission_source,
            "pro_subscription_required": self.pro_subscription_required,
            "pro_subscription_active": self.pro_subscription_active,
            "free_executed_trades_per_day": self.free_executed_trades_per_day,
            "executed_trades_today": self.executed_trades_today,
            "free_quota_remaining_before_trade": self.free_quota_remaining_before_trade,
            "execution_count_scope": self.execution_count_scope,
            "reset_timezone": self.reset_timezone,
            "reason_code": self.reason_code,
            "block_new_entry_after_free_quota": self.block_new_entry_after_free_quota,
        }


def default_commission_profile(
    fallback_commission_bps: Decimal = DEFAULT_COMMISSION_BPS_PER_SIDE,
) -> CommissionProfile:
    return CommissionProfile(
        commission_profile_id="project_default_regular_equity",
        instrument_ids=(),
        pro_subscription_required=False,
        free_executed_trades_per_day=0,
        free_commission_bps=Decimal("0"),
        fallback_commission_bps=fallback_commission_bps,
    )


def t_technologies_pro_commission_profile(
    fallback_commission_bps: Decimal = DEFAULT_COMMISSION_BPS_PER_SIDE,
) -> CommissionProfile:
    return CommissionProfile(
        commission_profile_id="t_technologies_pro_free_quota",
        instrument_ids=(T_TECHNOLOGIES_INSTRUMENT_ID, T_TECHNOLOGIES_TICKER),
        pro_subscription_required=True,
        free_executed_trades_per_day=T_PRO_FREE_EXECUTED_TRADES_PER_DAY,
        free_commission_bps=Decimal("0"),
        fallback_commission_bps=fallback_commission_bps,
    )


class CommissionPolicyService:
    """Estimate commission bps for the next executed trade."""

    def __init__(
        self,
        *,
        fallback_commission_bps: Decimal = DEFAULT_COMMISSION_BPS_PER_SIDE,
        block_t_after_free_quota: bool = False,
    ) -> None:
        self._fallback_commission_bps = max(fallback_commission_bps, Decimal("0"))
        self._block_t_after_free_quota = block_t_after_free_quota
        self._t_profile = t_technologies_pro_commission_profile(self._fallback_commission_bps)
        self._default_profile = default_commission_profile(self._fallback_commission_bps)

    def profile_for(self, instrument_id: str) -> CommissionProfile:
        if self._t_profile.matches(instrument_id):
            return self._t_profile
        return self._default_profile

    def estimate_next_execution(
        self,
        *,
        instrument_id: str,
        executed_trades_today: int = 0,
        pro_subscription_active: bool | None = None,
    ) -> CommissionPolicyResult:
        normalized = _normalize_instrument_id(instrument_id)
        executed = max(0, int(executed_trades_today))
        profile = self.profile_for(normalized)
        if profile.commission_profile_id != "t_technologies_pro_free_quota":
            return CommissionPolicyResult(
                instrument_id=normalized,
                commission_profile_id=profile.commission_profile_id,
                commission_bps=profile.fallback_commission_bps,
                fallback_commission_bps=profile.fallback_commission_bps,
                fallback_commission_source=profile.fallback_commission_source,
                pro_subscription_required=False,
                pro_subscription_active=pro_subscription_active,
                free_executed_trades_per_day=0,
                executed_trades_today=executed,
                free_quota_remaining_before_trade=0,
                execution_count_scope=profile.execution_count_scope,
                reset_timezone=profile.reset_timezone,
                reason_code="regular_project_default_commission",
            )

        remaining = max(profile.free_executed_trades_per_day - executed, 0)
        if pro_subscription_active is True and remaining > 0:
            return CommissionPolicyResult(
                instrument_id=normalized,
                commission_profile_id=profile.commission_profile_id,
                commission_bps=profile.free_commission_bps,
                fallback_commission_bps=profile.fallback_commission_bps,
                fallback_commission_source=profile.fallback_commission_source,
                pro_subscription_required=True,
                pro_subscription_active=True,
                free_executed_trades_per_day=profile.free_executed_trades_per_day,
                executed_trades_today=executed,
                free_quota_remaining_before_trade=remaining,
                execution_count_scope=profile.execution_count_scope,
                reset_timezone=profile.reset_timezone,
                reason_code="t_pro_free_quota_available",
            )

        reason = (
            "t_pro_free_quota_exhausted"
            if pro_subscription_active is True
            else "t_pro_subscription_unknown"
            if pro_subscription_active is None
            else "t_pro_subscription_inactive"
        )
        return CommissionPolicyResult(
            instrument_id=normalized,
            commission_profile_id=profile.commission_profile_id,
            commission_bps=profile.fallback_commission_bps,
            fallback_commission_bps=profile.fallback_commission_bps,
            fallback_commission_source=profile.fallback_commission_source,
            pro_subscription_required=True,
            pro_subscription_active=pro_subscription_active,
            free_executed_trades_per_day=profile.free_executed_trades_per_day,
            executed_trades_today=executed,
            free_quota_remaining_before_trade=remaining,
            execution_count_scope=profile.execution_count_scope,
            reset_timezone=profile.reset_timezone,
            reason_code=reason,
            block_new_entry_after_free_quota=(
                self._block_t_after_free_quota
                and pro_subscription_active is True
                and remaining <= 0
            ),
        )


def estimate_next_execution_commission(
    *,
    instrument_id: str,
    fallback_commission_bps: Decimal = DEFAULT_COMMISSION_BPS_PER_SIDE,
    executed_trades_today: int = 0,
    pro_subscription_active: bool | None = None,
    block_t_after_free_quota: bool = False,
) -> CommissionPolicyResult:
    return CommissionPolicyService(
        fallback_commission_bps=fallback_commission_bps,
        block_t_after_free_quota=block_t_after_free_quota,
    ).estimate_next_execution(
        instrument_id=instrument_id,
        executed_trades_today=executed_trades_today,
        pro_subscription_active=pro_subscription_active,
    )


def count_execution_events(events: Iterable[object]) -> int:
    """Count execution/fill records, deliberately ignoring lot quantity."""

    return sum(1 for _ in events)


def _normalize_instrument_id(value: str) -> str:
    raw = value.strip().upper()
    if raw == T_TECHNOLOGIES_TICKER:
        return T_TECHNOLOGIES_INSTRUMENT_ID
    return raw

"""Load typed strategy/risk config from Postgres `strategy_config` rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from trade_core.strategy.models import (
    ConfigDrivenStrategyConfig,
    RiskLimits,
)
from trading_common.db.models import StrategyConfig
from trading_common.db.repositories import StrategyConfigRepository
from trading_common.enums import SessionType


@dataclass(frozen=True, slots=True)
class LoadedStrategyConfig:
    config: ConfigDrivenStrategyConfig
    risk_limits: RiskLimits
    source: str
    strategy_config_id: str | None = None


class StrategyConfigLoader:
    """Map DB JSON payloads to the typed strategy/risk runtime contracts."""

    def __init__(self, session: Session) -> None:
        self._repository = StrategyConfigRepository(session)

    def load_active(
        self,
        *,
        strategy_id: str,
        session_template: str,
        fallback: ConfigDrivenStrategyConfig,
    ) -> LoadedStrategyConfig:
        row = self._repository.get_active(strategy_id, session_template)
        if row is None:
            config = replace(
                fallback,
                strategy_id=strategy_id,
                session_template=session_template,
            )
            return LoadedStrategyConfig(
                config=config,
                risk_limits=RiskLimits.from_strategy_config(config),
                source="fallback_conservative_default",
            )

        config = self._config_from_row(row, fallback=fallback)
        risk_limits = self._risk_limits_from_row(row, config=config)
        return LoadedStrategyConfig(
            config=config,
            risk_limits=risk_limits,
            source="postgres_strategy_config",
            strategy_config_id=str(row.strategy_config_id),
        )

    def _config_from_row(
        self,
        row: StrategyConfig,
        *,
        fallback: ConfigDrivenStrategyConfig,
    ) -> ConfigDrivenStrategyConfig:
        payload = dict(row.config_payload or {})
        risk_payload = dict(row.risk_limits or {})
        template = _session_type(row.session_template)
        templates = dict(fallback.session_templates)
        if template is not None and template in templates:
            templates[template] = replace(
                templates[template],
                enabled=_bool_value(payload, "enabled", default=templates[template].enabled),
                session_template=row.session_template,
            )
        return replace(
            fallback,
            strategy_id=row.strategy_id,
            strategy_version=row.version,
            session_templates=templates,
            allow_long=_bool_value(payload, "allow_long", risk_payload, fallback.allow_long),
            allow_short=_bool_value(payload, "allow_short", risk_payload, fallback.allow_short),
            max_long_lots=_int_value(
                payload,
                "max_long_lots",
                risk_payload,
                fallback.max_long_lots,
            ),
            max_short_lots=_int_value(
                payload,
                "max_short_lots",
                risk_payload,
                fallback.max_short_lots,
            ),
            max_gross_exposure_rub=_decimal_value(
                payload,
                "max_gross_exposure_rub",
                risk_payload,
                fallback.max_gross_exposure_rub,
            ),
            max_net_exposure_rub=_decimal_value(
                payload,
                "max_net_exposure_rub",
                risk_payload,
                fallback.max_net_exposure_rub,
            ),
            min_expected_edge_bps=_decimal_value(
                payload,
                "min_expected_edge_bps",
                risk_payload,
                fallback.min_expected_edge_bps,
            ),
            assumed_commission_bps_per_side=_decimal_value(
                payload,
                "assumed_commission_bps_per_side",
                risk_payload,
                fallback.assumed_commission_bps_per_side,
            ),
            assumed_slippage_bps=_decimal_value(
                payload,
                "assumed_slippage_bps",
                risk_payload,
                fallback.assumed_slippage_bps,
            ),
            min_edge_after_total_costs_bps=_decimal_value(
                payload,
                "min_edge_after_total_costs_bps",
                risk_payload,
                fallback.min_edge_after_total_costs_bps,
            ),
            session_template=row.session_template,
        )

    def _risk_limits_from_row(
        self,
        row: StrategyConfig,
        *,
        config: ConfigDrivenStrategyConfig,
    ) -> RiskLimits:
        payload = dict(row.risk_limits or {})
        base = RiskLimits.from_strategy_config(config)
        return replace(
            base,
            max_spread_bps=_decimal_value(payload, "max_spread_bps", default=base.max_spread_bps),
            min_market_quality_score=_decimal_value(
                payload,
                "min_market_quality_score",
                default=base.min_market_quality_score,
            ),
            max_data_age_ms=_int_value(payload, "max_data_age_ms", default=base.max_data_age_ms),
            min_edge_after_costs_bps=_decimal_value(
                payload,
                "min_edge_after_costs_bps",
                default=base.min_edge_after_costs_bps,
            ),
            assumed_cost_bps=_decimal_value(
                payload,
                "assumed_cost_bps",
                default=base.assumed_cost_bps,
            ),
            risk_budget_remaining_rub=_decimal_value(
                payload,
                "risk_budget_remaining_rub",
                default=base.risk_budget_remaining_rub,
            ),
            max_daily_loss_rub=_decimal_value(
                payload,
                "max_daily_loss_rub",
                default=base.max_daily_loss_rub,
            ),
            current_daily_pnl_rub=_decimal_value(
                payload,
                "current_daily_pnl_rub",
                default=base.current_daily_pnl_rub,
            ),
            max_position_lots=_int_value(
                payload,
                "max_position_lots",
                default=base.max_position_lots,
            ),
            short_allowed_by_account=_bool_value(
                payload,
                "short_allowed_by_account",
                default=base.short_allowed_by_account,
            ),
            short_allowed_by_instrument=_bool_value(
                payload,
                "short_allowed_by_instrument",
                default=base.short_allowed_by_instrument,
            ),
            margin_or_collateral_available=_bool_value(
                payload,
                "margin_or_collateral_available",
                default=base.margin_or_collateral_available,
            ),
            forced_cover_policy=_bool_value(
                payload,
                "forced_cover_policy",
                default=base.forced_cover_policy,
            ),
            freeze_new_entries=_bool_value(
                payload,
                "freeze_new_entries",
                default=base.freeze_new_entries,
            ),
            block_entries_on_dividend_gap_day=_bool_value(
                payload,
                "block_entries_on_dividend_gap_day",
                default=base.block_entries_on_dividend_gap_day,
            ),
            block_entries_on_corporate_action_day=_bool_value(
                payload,
                "block_entries_on_corporate_action_day",
                default=base.block_entries_on_corporate_action_day,
            ),
            block_short_on_special_day=_bool_value(
                payload,
                "block_short_on_special_day",
                default=base.block_short_on_special_day,
            ),
            special_day_trade_policy=str(
                payload.get("special_day_trade_policy", base.special_day_trade_policy)
            ),
        )


def _session_type(value: str) -> SessionType | None:
    try:
        return SessionType(value)
    except ValueError:
        return None


def _bool_value(
    payload: Mapping[str, Any],
    key: str,
    secondary: Mapping[str, Any] | None = None,
    default: bool = False,
) -> bool:
    value = payload.get(key)
    if value is None and secondary is not None:
        value = secondary.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_value(
    payload: Mapping[str, Any],
    key: str,
    secondary: Mapping[str, Any] | None = None,
    default: int = 0,
) -> int:
    value = payload.get(key)
    if value is None and secondary is not None:
        value = secondary.get(key)
    return default if value is None else int(str(value))


def _decimal_value(
    payload: Mapping[str, Any],
    key: str,
    secondary: Mapping[str, Any] | None = None,
    default: Decimal = Decimal("0"),
) -> Decimal:
    value = payload.get(key)
    if value is None and secondary is not None:
        value = secondary.get(key)
    return default if value is None else Decimal(str(value))

"""Load typed strategy/risk config from Postgres `strategy_config` rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from trade_core.market_data import Timeframe
from trade_core.strategy.models import (
    ConfigDrivenStrategyConfig,
    RiskLimits,
    TimeframeStrategyRule,
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
        templates = _templates_from_payload(payload, fallback_templates=templates)
        if template is not None and template in templates:
            base_template = templates[template]
            templates[template] = replace(
                base_template,
                enabled=_bool_value(payload, "enabled", default=base_template.enabled),
                rules_by_timeframe=_rules_from_payload(
                    payload,
                    fallback_rules=base_template.rules_by_timeframe,
                ),
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


def _templates_from_payload(
    payload: Mapping[str, Any],
    *,
    fallback_templates: Mapping[SessionType, Any],
) -> dict[SessionType, Any]:
    templates = dict(fallback_templates)
    raw_templates = payload.get("session_templates")
    if not isinstance(raw_templates, Mapping):
        return templates
    for raw_session_type, raw_template in raw_templates.items():
        session_type = _session_type(str(raw_session_type))
        if (
            session_type is None
            or session_type not in templates
            or not isinstance(raw_template, Mapping)
        ):
            continue
        base = templates[session_type]
        templates[session_type] = replace(
            base,
            enabled=_bool_value(raw_template, "enabled", default=base.enabled),
            rules_by_timeframe=_rules_from_payload(
                raw_template,
                fallback_rules=base.rules_by_timeframe,
            ),
            session_template=str(raw_template.get("session_template", session_type.value)),
        )
    return templates


def _rules_from_payload(
    payload: Mapping[str, Any],
    *,
    fallback_rules: Mapping[Timeframe, TimeframeStrategyRule],
) -> Mapping[Timeframe, TimeframeStrategyRule]:
    raw_rules = payload.get("rules_by_timeframe") or payload.get("timeframes")
    if not isinstance(raw_rules, Mapping):
        return fallback_rules
    rules = dict(fallback_rules)
    for raw_timeframe, raw_rule in raw_rules.items():
        timeframe = _timeframe(str(raw_timeframe))
        if timeframe is None or timeframe not in rules or not isinstance(raw_rule, Mapping):
            continue
        base = rules[timeframe]
        rules[timeframe] = replace(
            base,
            enabled=_bool_value(raw_rule, "enabled", default=base.enabled),
            min_move_bps=_decimal_value(
                raw_rule,
                "min_move_bps",
                default=base.min_move_bps,
            ),
            lot_qty=_int_value(raw_rule, "lot_qty", default=base.lot_qty),
            order_type=str(raw_rule.get("order_type", base.order_type)),
            time_in_force=str(raw_rule.get("time_in_force", base.time_in_force)),
            expected_holding_minutes=_int_value(
                raw_rule,
                "expected_holding_minutes",
                default=base.expected_holding_minutes,
            ),
            min_expected_edge_bps=_decimal_value(
                raw_rule,
                "min_expected_edge_bps",
                default=base.min_expected_edge_bps,
            ),
        )
    return rules


def _timeframe(value: str) -> Timeframe | None:
    try:
        return Timeframe(value)
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

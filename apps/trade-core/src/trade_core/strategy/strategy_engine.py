"""Configuration-driven placeholder strategy engine."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import Decimal

from trade_core.market_data import Bar, Timeframe
from trade_core.strategy.models import (
    ConfigDrivenStrategyConfig,
    SignalAction,
    SignalCandidateDecision,
    StrategyDecision,
    StrategyEvaluationContext,
    StrategyState,
    TimeframeStrategyRule,
    TradeSide,
)

TEN_THOUSAND = Decimal("10000")
SUPPORTED_SIGNAL_TIMEFRAMES = frozenset({Timeframe.M5, Timeframe.M10, Timeframe.M15})


class ConfigDrivenStrategyEngine:
    """Deterministic strategy stub driven only by versioned configuration.

    The engine intentionally emits explainable candidates from closed 5m/10m/15m
    bars. It is not a profit model; it is a stable scaffold for risk, execution,
    replay, and calibration.
    """

    def __init__(self, config: ConfigDrivenStrategyConfig) -> None:
        self._config = config

    def evaluate(self, context: StrategyEvaluationContext) -> StrategyDecision:
        template = self._config.session_templates.get(context.session_snapshot.session_type)
        if template is None or not template.enabled:
            return StrategyDecision(
                previous_state=context.current_state,
                next_state=StrategyState.WAIT,
                candidates=(),
                reason_code="strategy_disabled",
                decision_payload={
                    "session_type": context.session_snapshot.session_type.value,
                    "configured": template is not None,
                },
            )

        candidates: list[SignalCandidateDecision] = []
        missing_timeframes: list[str] = []
        for timeframe, rule in sorted(
            self._rules_for_context(context, template.rules_by_timeframe).items(),
            key=lambda item: item[0].minutes,
        ):
            if timeframe not in SUPPORTED_SIGNAL_TIMEFRAMES or not rule.enabled:
                continue
            bar = context.latest_closed_bars.get(timeframe)
            if bar is None or not bar.is_closed:
                missing_timeframes.append(timeframe.value)
                continue
            candidate = self._evaluate_rule(context=context, rule=rule, bar=bar)
            if candidate is not None:
                candidates.append(candidate)

        if candidates:
            next_state = StrategyState.CANDIDATE
            reason_code = "candidate_created"
        elif context.current_state is StrategyState.IDLE or missing_timeframes:
            next_state = StrategyState.WARMING_UP
            reason_code = "missing_closed_candle"
        else:
            next_state = StrategyState.WAIT
            reason_code = "no_signal"

        return StrategyDecision(
            previous_state=context.current_state,
            next_state=next_state,
            candidates=tuple(candidates),
            reason_code=reason_code,
            decision_payload={
                "missing_timeframes": missing_timeframes,
                "session_template": template.session_template
                or context.session_snapshot.session_type.value,
                "allow_long": self._config.allow_long,
                "allow_short": self._config.allow_short,
            },
        )

    def _rules_for_context(
        self,
        context: StrategyEvaluationContext,
        base_rules: Mapping[Timeframe, TimeframeStrategyRule],
    ) -> dict[Timeframe, TimeframeStrategyRule]:
        rules = dict(base_rules)
        overrides = self._config.instrument_timeframe_overrides.get(
            context.instrument.instrument_id,
            {},
        )
        rules.update(overrides)
        return rules

    def _evaluate_rule(
        self,
        *,
        context: StrategyEvaluationContext,
        rule: TimeframeStrategyRule,
        bar: Bar,
    ) -> SignalCandidateDecision | None:
        if bar.open_price <= Decimal("0"):
            return None

        move_bps = ((bar.close_price - bar.open_price) / bar.open_price) * TEN_THOUSAND
        abs_move_bps = abs(move_bps).quantize(Decimal("0.0001"))
        if abs_move_bps < rule.min_move_bps:
            return None

        if context.open_position_lots == 0:
            action = SignalAction.ENTRY
            side = TradeSide.BUY if move_bps > Decimal("0") else TradeSide.SELL
        else:
            action = SignalAction.EXIT
            side = TradeSide.SELL if context.open_position_lots > 0 else TradeSide.BUY

        intended_price = (
            context.market_state.mid_price
            if context.market_state is not None and context.market_state.mid_price is not None
            else bar.close_price
        )
        fingerprint = _fingerprint(
            self._config.strategy_id,
            str(self._config.strategy_version),
            context.instrument.instrument_id,
            rule.timeframe.value,
            bar.close_ts_utc.isoformat(),
            action.value,
            side.value,
        )

        return SignalCandidateDecision(
            strategy_id=self._config.strategy_id,
            strategy_version=self._config.strategy_version,
            instrument=context.instrument,
            timeframe=rule.timeframe,
            action=action,
            side=side,
            order_type=rule.order_type,
            lot_qty=rule.lot_qty,
            intended_price=intended_price,
            time_in_force=rule.time_in_force,
            expected_edge_bps=max(
                abs_move_bps,
                rule.min_expected_edge_bps,
                self._config.min_expected_edge_bps,
            ),
            expected_holding_minutes=rule.expected_holding_minutes,
            signal_fingerprint=fingerprint,
            condition_payload={
                "rule": "closed_bar_directional_move",
                "bar_open_ts_utc": bar.open_ts_utc.isoformat(),
                "bar_close_ts_utc": bar.close_ts_utc.isoformat(),
                "bar_open_price": str(bar.open_price),
                "bar_close_price": str(bar.close_price),
                "move_bps": str(move_bps.quantize(Decimal("0.0001"))),
                "min_move_bps": str(rule.min_move_bps),
                "strategy_min_expected_edge_bps": str(self._config.min_expected_edge_bps),
                "assumed_commission_bps_per_side": str(
                    self._config.assumed_commission_bps_per_side
                ),
                "assumed_slippage_bps": str(self._config.assumed_slippage_bps),
                "min_edge_after_total_costs_bps": str(self._config.min_edge_after_total_costs_bps),
                "allow_long": self._config.allow_long,
                "allow_short": self._config.allow_short,
                "lot_size": context.instrument.lot_size,
                "min_price_increment": (
                    str(context.instrument.min_price_increment)
                    if context.instrument.min_price_increment is not None
                    else None
                ),
                "uses_closed_bar": bar.is_closed,
            },
            lot_size=context.instrument.lot_size,
            min_price_increment=context.instrument.min_price_increment,
        )


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]

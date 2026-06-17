"""Risk gate chain with explicit blocker codes."""

from __future__ import annotations

from decimal import Decimal

from trade_core.session import OrderSessionPolicy
from trade_core.session.reason_codes import ORDER_TYPE_FORBIDDEN
from trade_core.strategy.models import (
    BlockerCode,
    PortfolioSnapshot,
    RiskAssessmentInput,
    RiskBlocker,
    RiskDecision,
    SignalAction,
    TradeSide,
)


class DefaultRiskEngine:
    """Deterministic causal gate chain used before execution."""

    def __init__(self, order_session_policy: OrderSessionPolicy | None = None) -> None:
        self._order_session_policy = order_session_policy or OrderSessionPolicy()

    @staticmethod
    def blocker_catalog() -> tuple[BlockerCode, ...]:
        return tuple(BlockerCode)

    def evaluate(self, request: RiskAssessmentInput) -> RiskDecision:
        gates: list[RiskBlocker] = []

        permission = self._order_session_policy.evaluate(
            snapshot=request.session_snapshot,
            action=request.candidate.action.value,
            order_type=request.candidate.order_type,
        )
        if permission.reason_code == ORDER_TYPE_FORBIDDEN:
            session_code = BlockerCode.ORDER_TYPE_FORBIDDEN
        else:
            session_code = BlockerCode.SESSION_FORBIDDEN

        self._append_gate(
            gates,
            code=session_code,
            gate_name="session_order_permission",
            passed=permission.allowed,
            reason_payload={
                "session_policy_reason_code": permission.reason_code,
                "allowed_actions": list(permission.allowed_actions),
                "allowed_order_types": list(permission.allowed_order_types),
                "session_phase": request.session_snapshot.session_phase.value,
            },
        )

        is_entry = request.candidate.action == SignalAction.ENTRY
        is_long_entry = is_entry and request.candidate.side == TradeSide.BUY
        is_short_entry = is_entry and request.candidate.side == TradeSide.SELL

        self._append_gate(
            gates,
            code=BlockerCode.SESSION_FORBIDDEN,
            gate_name="no_new_entries_during_freeze",
            passed=not (is_entry and request.limits.freeze_new_entries),
            reason_payload={
                "freeze_new_entries": request.limits.freeze_new_entries,
                "candidate_action": request.candidate.action.value,
            },
        )
        self._append_gate(
            gates,
            code=BlockerCode.POSITION_STATE_STALE,
            gate_name="position_state_freshness",
            passed=not is_entry or request.portfolio.position_state_fresh,
            reason_payload={
                "position_state_fresh": request.portfolio.position_state_fresh,
                "position_state_age_ms": request.portfolio.position_state_age_ms,
                "position_reason_code": request.portfolio.position_reason_code,
            },
            observed_value=(
                Decimal(request.portfolio.position_state_age_ms)
                if request.portfolio.position_state_age_ms is not None
                else None
            ),
        )
        self._append_gate(
            gates,
            code=BlockerCode.POSITION_RECONCILIATION_MISMATCH,
            gate_name="position_reconciliation",
            passed=not is_entry or request.portfolio.position_reconciliation_matched,
            reason_payload={
                "position_reconciliation_matched": (
                    request.portfolio.position_reconciliation_matched
                ),
                "local_position_lots": request.portfolio.local_position_lots,
                "broker_position_lots": request.portfolio.broker_position_lots,
                "position_reason_code": request.portfolio.position_reason_code,
            },
            observed_value=(
                Decimal(request.portfolio.broker_position_lots)
                if request.portfolio.broker_position_lots is not None
                else None
            ),
        )

        if is_long_entry:
            self._append_gate(
                gates,
                code=BlockerCode.SESSION_FORBIDDEN,
                gate_name="long_allowed_by_config",
                passed=request.limits.allow_long,
                reason_payload={"allow_long": request.limits.allow_long},
            )

        if is_short_entry:
            self._append_gate(
                gates,
                code=BlockerCode.SHORT_NOT_ALLOWED_BY_CONFIG,
                gate_name="short_allowed_by_config",
                passed=request.limits.allow_short,
                reason_payload={"allow_short": request.limits.allow_short},
            )
            self._append_gate(
                gates,
                code=BlockerCode.SHORT_NOT_ALLOWED_BY_BROKER,
                gate_name="short_allowed_by_account",
                passed=request.limits.short_allowed_by_account,
                reason_payload={
                    "short_allowed_by_account": request.limits.short_allowed_by_account
                },
            )
            self._append_gate(
                gates,
                code=BlockerCode.SHORT_NOT_ALLOWED_BY_BROKER,
                gate_name="short_allowed_by_instrument",
                passed=request.limits.short_allowed_by_instrument,
                reason_payload={
                    "short_allowed_by_instrument": request.limits.short_allowed_by_instrument
                },
            )
            self._append_gate(
                gates,
                code=BlockerCode.INSUFFICIENT_MARGIN,
                gate_name="margin_or_collateral_available",
                passed=request.limits.margin_or_collateral_available,
                reason_payload={
                    "margin_or_collateral_available": (
                        request.limits.margin_or_collateral_available
                    )
                },
            )
            self._append_gate(
                gates,
                code=BlockerCode.SESSION_FORBIDDEN,
                gate_name="no_short_during_forbidden_session_phase",
                passed=permission.allowed,
                reason_payload={
                    "session_phase": request.session_snapshot.session_phase.value,
                    "session_policy_reason_code": permission.reason_code,
                },
            )
            self._append_gate(
                gates,
                code=BlockerCode.INSUFFICIENT_MARGIN,
                gate_name="forced_cover_policy",
                passed=not request.limits.forced_cover_policy,
                reason_payload={"forced_cover_policy": request.limits.forced_cover_policy},
            )

        side_conflict = (is_long_entry and _current_short_lots(request.portfolio) > 0) or (
            is_short_entry and _current_long_lots(request.portfolio) > 0
        )
        self._append_gate(
            gates,
            code=BlockerCode.POSITION_SIDE_CONFLICT,
            gate_name="position_side_conflict",
            passed=not side_conflict,
            reason_payload={
                "open_position_lots": request.portfolio.open_position_lots,
                "long_position_lots": _current_long_lots(request.portfolio),
                "short_position_lots": _current_short_lots(request.portfolio),
                "candidate_side": request.candidate.side.value,
                "candidate_action": request.candidate.action.value,
            },
        )

        market_state = request.market_state
        spread_bps = market_state.spread_bps if market_state is not None else None
        self._append_gate(
            gates,
            code=BlockerCode.SPREAD_TOO_WIDE,
            gate_name="spread_limit",
            passed=spread_bps is not None and spread_bps <= request.limits.max_spread_bps,
            limit_value=request.limits.max_spread_bps,
            observed_value=spread_bps,
            reason_payload={"spread_bps": _optional_str(spread_bps)},
        )

        quality_score = market_state.market_quality_score if market_state is not None else None
        self._append_gate(
            gates,
            code=BlockerCode.MARKET_QUALITY_LOW,
            gate_name="market_quality",
            passed=(
                quality_score is not None
                and quality_score >= request.limits.min_market_quality_score
            ),
            limit_value=request.limits.min_market_quality_score,
            observed_value=quality_score,
            reason_payload={"market_quality_score": _optional_str(quality_score)},
        )

        data_age_ms = market_state.feed_freshness.age_ms if market_state is not None else None
        feed_is_stale = market_state.feed_freshness.is_stale if market_state is not None else True
        self._append_gate(
            gates,
            code=BlockerCode.STALE_MARKET_DATA,
            gate_name="feed_freshness",
            passed=(
                data_age_ms is not None
                and data_age_ms <= request.limits.max_data_age_ms
                and not feed_is_stale
            ),
            limit_value=Decimal(request.limits.max_data_age_ms),
            observed_value=Decimal(data_age_ms) if data_age_ms is not None else None,
            reason_payload={
                "data_age_ms": data_age_ms,
                "max_data_age_ms": request.limits.max_data_age_ms,
            },
        )

        edge_after_costs = request.candidate.expected_edge_bps - request.limits.assumed_cost_bps
        self._append_gate(
            gates,
            code=BlockerCode.NO_EDGE_AFTER_COSTS,
            gate_name="edge_after_costs",
            passed=(
                edge_after_costs >= request.limits.min_edge_after_costs_bps
                and request.candidate.expected_edge_bps >= request.limits.min_expected_edge_bps
            ),
            limit_value=max(
                request.limits.min_edge_after_costs_bps,
                request.limits.min_expected_edge_bps,
            ),
            observed_value=edge_after_costs,
            reason_payload={
                "expected_edge_bps": str(request.candidate.expected_edge_bps),
                "min_expected_edge_bps": str(request.limits.min_expected_edge_bps),
                "assumed_cost_bps": str(request.limits.assumed_cost_bps),
                "edge_after_costs_bps": str(edge_after_costs),
            },
        )

        total_costs = _total_expected_costs_bps(
            spread_bps=spread_bps,
            commission_bps_per_side=request.limits.assumed_commission_bps_per_side,
            slippage_bps=request.limits.assumed_slippage_bps,
        )
        edge_after_total_costs = request.candidate.expected_edge_bps - total_costs
        self._append_gate(
            gates,
            code=BlockerCode.TOTAL_COSTS_EXCEED_EDGE,
            gate_name="total_expected_costs",
            passed=edge_after_total_costs >= request.limits.min_edge_after_total_costs_bps,
            limit_value=request.limits.min_edge_after_total_costs_bps,
            observed_value=edge_after_total_costs,
            reason_payload={
                "expected_edge_bps": str(request.candidate.expected_edge_bps),
                "commission_bps_per_side": str(
                    max(request.limits.assumed_commission_bps_per_side, Decimal("5"))
                ),
                "round_trip_commission_bps": str(
                    max(
                        request.limits.assumed_commission_bps_per_side * Decimal("2"),
                        Decimal("10"),
                    )
                ),
                "spread_bps": _optional_str(spread_bps),
                "assumed_slippage_bps": str(max(request.limits.assumed_slippage_bps, Decimal("0"))),
                "total_expected_costs_bps": str(total_costs),
                "edge_after_total_costs_bps": str(edge_after_total_costs),
            },
        )

        estimated_notional = _estimated_notional(
            price=request.candidate.intended_price,
            lot_qty=request.candidate.lot_qty,
        )
        self._append_gate(
            gates,
            code=BlockerCode.RISK_BUDGET_EXCEEDED,
            gate_name="risk_budget",
            passed=(
                request.limits.risk_budget_remaining_rub > Decimal("0")
                and (
                    estimated_notional is None
                    or estimated_notional <= request.limits.risk_budget_remaining_rub
                )
            ),
            limit_value=request.limits.risk_budget_remaining_rub,
            observed_value=estimated_notional,
            reason_payload={
                "estimated_notional_rub": _optional_str(estimated_notional),
                "risk_budget_remaining_rub": str(request.limits.risk_budget_remaining_rub),
            },
        )

        self._append_gate(
            gates,
            code=BlockerCode.MAX_DRAWDOWN_REACHED,
            gate_name="max_drawdown",
            passed=request.limits.current_daily_pnl_rub > -request.limits.max_daily_loss_rub,
            limit_value=-request.limits.max_daily_loss_rub,
            observed_value=request.limits.current_daily_pnl_rub,
            reason_payload={
                "current_daily_pnl_rub": str(request.limits.current_daily_pnl_rub),
                "max_daily_loss_rub": str(request.limits.max_daily_loss_rub),
            },
        )

        self._append_gate(
            gates,
            code=BlockerCode.OPEN_ORDER_CONFLICT,
            gate_name="open_order_conflict",
            passed=request.portfolio.open_order_count == 0,
            limit_value=Decimal("0"),
            observed_value=Decimal(request.portfolio.open_order_count),
            reason_payload={"open_order_count": request.portfolio.open_order_count},
        )

        projected_long_lots = _current_long_lots(request.portfolio) + (
            request.candidate.lot_qty if is_long_entry else 0
        )
        projected_short_lots = _current_short_lots(request.portfolio) + (
            request.candidate.lot_qty if is_short_entry else 0
        )
        if is_long_entry:
            self._append_gate(
                gates,
                code=BlockerCode.POSITION_LIMIT_REACHED,
                gate_name="max_long_position",
                passed=projected_long_lots <= request.limits.max_long_lots,
                limit_value=Decimal(request.limits.max_long_lots),
                observed_value=Decimal(projected_long_lots),
                reason_payload={
                    "current_long_position_lots": _current_long_lots(request.portfolio),
                    "candidate_lot_qty": request.candidate.lot_qty,
                    "projected_long_position_lots": projected_long_lots,
                },
            )
        if is_short_entry:
            self._append_gate(
                gates,
                code=BlockerCode.POSITION_LIMIT_REACHED,
                gate_name="max_short_position",
                passed=projected_short_lots <= request.limits.max_short_lots,
                limit_value=Decimal(request.limits.max_short_lots),
                observed_value=Decimal(projected_short_lots),
                reason_payload={
                    "current_short_position_lots": _current_short_lots(request.portfolio),
                    "candidate_lot_qty": request.candidate.lot_qty,
                    "projected_short_position_lots": projected_short_lots,
                },
            )

        projected_gross_exposure = _projected_gross_exposure(
            request=request,
            estimated_notional=estimated_notional,
            is_entry=is_entry,
        )
        exposure_code = (
            BlockerCode.MAX_SHORT_EXPOSURE_REACHED
            if is_short_entry
            else BlockerCode.MAX_LONG_EXPOSURE_REACHED
        )
        self._append_gate(
            gates,
            code=exposure_code,
            gate_name="max_gross_exposure",
            passed=projected_gross_exposure <= request.limits.max_gross_exposure_rub,
            limit_value=request.limits.max_gross_exposure_rub,
            observed_value=projected_gross_exposure,
            reason_payload={
                "gross_exposure_rub": str(request.portfolio.gross_exposure_rub),
                "estimated_notional_rub": _optional_str(estimated_notional),
                "projected_gross_exposure_rub": str(projected_gross_exposure),
                "max_gross_exposure_rub": str(request.limits.max_gross_exposure_rub),
            },
        )
        projected_net_exposure_abs = abs(
            _projected_net_exposure(
                request=request,
                estimated_notional=estimated_notional,
                is_long_entry=is_long_entry,
                is_short_entry=is_short_entry,
            )
        )
        self._append_gate(
            gates,
            code=exposure_code,
            gate_name="max_net_exposure",
            passed=projected_net_exposure_abs <= request.limits.max_net_exposure_rub,
            limit_value=request.limits.max_net_exposure_rub,
            observed_value=projected_net_exposure_abs,
            reason_payload={
                "net_exposure_rub": str(request.portfolio.net_exposure_rub),
                "estimated_notional_rub": _optional_str(estimated_notional),
                "projected_net_exposure_abs_rub": str(projected_net_exposure_abs),
                "max_net_exposure_rub": str(request.limits.max_net_exposure_rub),
            },
        )

        projected_position = abs(request.portfolio.open_position_lots) + request.candidate.lot_qty
        self._append_gate(
            gates,
            code=BlockerCode.POSITION_LIMIT_REACHED,
            gate_name="position_limit",
            passed=projected_position <= request.limits.max_position_lots,
            limit_value=Decimal(request.limits.max_position_lots),
            observed_value=Decimal(projected_position),
            reason_payload={
                "open_position_lots": request.portfolio.open_position_lots,
                "candidate_lot_qty": request.candidate.lot_qty,
                "projected_position_lots": projected_position,
            },
        )

        first_failed_rank = next(
            (gate.gate_rank for gate in gates if not gate.passed),
            None,
        )
        blockers = tuple(
            RiskBlocker(
                code=gate.code,
                gate_name=gate.gate_name,
                gate_rank=gate.gate_rank,
                passed=gate.passed,
                is_final_blocker=first_failed_rank == gate.gate_rank,
                reason_payload=gate.reason_payload,
                limit_value=gate.limit_value,
                observed_value=gate.observed_value,
            )
            for gate in gates
        )
        return RiskDecision(allowed=first_failed_rank is None, blockers=blockers)

    @staticmethod
    def _append_gate(
        gates: list[RiskBlocker],
        *,
        code: BlockerCode,
        gate_name: str,
        passed: bool,
        reason_payload: dict[str, object],
        limit_value: Decimal | None = None,
        observed_value: Decimal | None = None,
    ) -> None:
        gates.append(
            RiskBlocker(
                code=code,
                gate_name=gate_name,
                gate_rank=len(gates) + 1,
                passed=passed,
                is_final_blocker=False,
                reason_payload=reason_payload,
                limit_value=limit_value,
                observed_value=observed_value,
            )
        )


def _estimated_notional(*, price: Decimal | None, lot_qty: int) -> Decimal | None:
    if price is None:
        return None
    return price * Decimal(lot_qty)


def _current_long_lots(portfolio: PortfolioSnapshot) -> int:
    long_lots = portfolio.long_position_lots
    if long_lots:
        return long_lots
    return max(portfolio.open_position_lots, 0)


def _current_short_lots(portfolio: PortfolioSnapshot) -> int:
    short_lots = portfolio.short_position_lots
    if short_lots:
        return short_lots
    return abs(min(portfolio.open_position_lots, 0))


def _total_expected_costs_bps(
    *,
    spread_bps: Decimal | None,
    commission_bps_per_side: Decimal,
    slippage_bps: Decimal,
) -> Decimal:
    commission_per_side = max(commission_bps_per_side, Decimal("5"))
    round_trip_commission = max(commission_per_side * Decimal("2"), Decimal("10"))
    spread_component = max(spread_bps or Decimal("0"), Decimal("0"))
    slippage_component = max(slippage_bps, Decimal("0"))
    return round_trip_commission + spread_component + slippage_component


def _projected_gross_exposure(
    *,
    request: RiskAssessmentInput,
    estimated_notional: Decimal | None,
    is_entry: bool,
) -> Decimal:
    if not is_entry or estimated_notional is None:
        return request.portfolio.gross_exposure_rub
    return request.portfolio.gross_exposure_rub + estimated_notional


def _projected_net_exposure(
    *,
    request: RiskAssessmentInput,
    estimated_notional: Decimal | None,
    is_long_entry: bool,
    is_short_entry: bool,
) -> Decimal:
    if estimated_notional is None:
        return request.portfolio.net_exposure_rub
    if is_long_entry:
        return request.portfolio.net_exposure_rub + estimated_notional
    if is_short_entry:
        return request.portfolio.net_exposure_rub - estimated_notional
    return request.portfolio.net_exposure_rub


def _optional_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None

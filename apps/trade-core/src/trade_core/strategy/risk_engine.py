"""Risk gate chain with explicit blocker codes."""

from __future__ import annotations

from decimal import Decimal

from trade_core.session import OrderSessionPolicy
from trade_core.session.reason_codes import ORDER_TYPE_FORBIDDEN
from trade_core.strategy.models import (
    BlockerCode,
    RiskAssessmentInput,
    RiskBlocker,
    RiskDecision,
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
            passed=edge_after_costs >= request.limits.min_edge_after_costs_bps,
            limit_value=request.limits.min_edge_after_costs_bps,
            observed_value=edge_after_costs,
            reason_payload={
                "expected_edge_bps": str(request.candidate.expected_edge_bps),
                "assumed_cost_bps": str(request.limits.assumed_cost_bps),
                "edge_after_costs_bps": str(edge_after_costs),
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


def _optional_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None

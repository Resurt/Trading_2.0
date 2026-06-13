"""Pluggable boundaries for strategy, risk, execution, and reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from trade_core.strategy.models import (
    CancelReasonCode,
    OrderIntentRequest,
    OrderLifecycleResult,
    ReconciliationResult,
    RiskAssessmentInput,
    RiskDecision,
    StrategyDecision,
    StrategyEvaluationContext,
)
from trading_common.db.models import OrderIntent


class StrategyEngine(Protocol):
    """Strategy boundary: market/session context in, candidates out."""

    def evaluate(self, context: StrategyEvaluationContext) -> StrategyDecision: ...


class RiskEngine(Protocol):
    """Risk boundary: candidate plus limits in, causal gates out."""

    def evaluate(self, request: RiskAssessmentInput) -> RiskDecision: ...


class ExecutionEngine(Protocol):
    """Execution boundary: idempotent order intent lifecycle."""

    def create_order_intent(self, request: OrderIntentRequest) -> OrderIntent: ...

    async def post_order(self, intent: OrderIntent) -> OrderLifecycleResult: ...

    async def cancel_order(
        self,
        intent: OrderIntent,
        *,
        account_id: str,
        cancel_reason_code: CancelReasonCode,
        cancel_payload: Mapping[str, object],
        exchange_order_id: str | None = None,
    ) -> OrderLifecycleResult: ...

    async def replace_order(
        self,
        old_intent: OrderIntent,
        new_request: OrderIntentRequest,
        *,
        cancel_reason_code: CancelReasonCode,
        cancel_payload: Mapping[str, object],
        exchange_order_id: str | None = None,
    ) -> tuple[OrderLifecycleResult, OrderLifecycleResult]: ...


class ReconciliationService(Protocol):
    """Broker state convergence boundary."""

    async def reconcile_order(
        self,
        *,
        account_id: str,
        request_order_id: UUID,
        exchange_order_id: str | None = None,
    ) -> ReconciliationResult: ...

    async def reconcile_open_orders(self, *, account_id: str) -> ReconciliationResult: ...

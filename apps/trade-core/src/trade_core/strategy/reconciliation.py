"""Broker reconciliation helpers for execution state convergence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    OrdersRequest,
    OrderStateRequest,
    RequestMetadata,
)
from trade_core.strategy.models import ReconciliationResult
from trading_common.db.models import BrokerOrder, OrderIntent
from trading_common.db.repositories import OrderRepository

TERMINAL_STATUSES = frozenset({"filled", "cancelled", "rejected"})


class DefaultReconciliationService:
    """Refreshes local order lifecycle from SDK-neutral broker responses."""

    def __init__(self, *, broker_gateway: BrokerGateway, orders: OrderRepository) -> None:
        self._broker_gateway = broker_gateway
        self._orders = orders

    async def reconcile_order(
        self,
        *,
        account_id: str,
        request_order_id: UUID,
        exchange_order_id: str | None = None,
    ) -> ReconciliationResult:
        response = await self._broker_gateway.reconcile_order_state(
            OrderStateRequest(
                account_id=account_id,
                request_order_id=request_order_id,
                exchange_order_id=exchange_order_id,
            ),
            metadata=RequestMetadata(account_id=account_id, request_id=request_order_id),
        )
        intent = self._orders.get_intent_by_request_order_id(request_order_id)
        if intent is None:
            return ReconciliationResult(
                observed_order_count=1,
                updated_order_count=0,
                payload={"missing_intent_request_order_id": str(request_order_id)},
            )

        broker_order = self._save_observed_order(intent=intent, response=response)
        self._sync_intent_terminal_status(intent=intent, broker_order=broker_order)
        return ReconciliationResult(
            observed_order_count=1,
            updated_order_count=1,
            payload={"request_order_id": str(request_order_id)},
        )

    async def reconcile_open_orders(self, *, account_id: str) -> ReconciliationResult:
        response = await self._broker_gateway.reconcile_open_orders(
            OrdersRequest(account_id=account_id),
            metadata=RequestMetadata(account_id=account_id),
        )
        observed_orders = response.data.get("orders")
        if not isinstance(observed_orders, list):
            return ReconciliationResult(
                observed_order_count=0,
                updated_order_count=0,
                payload={"orders_payload_shape": type(observed_orders).__name__},
            )

        updated = 0
        for raw_order in observed_orders:
            if not isinstance(raw_order, Mapping):
                continue
            request_order_id = _uuid_from_mapping(raw_order, "request_order_id")
            if request_order_id is None:
                continue
            intent = self._orders.get_intent_by_request_order_id(request_order_id)
            if intent is None:
                continue
            order_response = BrokerUnaryResponse(
                method_name=response.method_name,
                data={str(key): value for key, value in raw_order.items()},
                headers=response.headers,
            )
            broker_order = self._save_observed_order(intent=intent, response=order_response)
            self._sync_intent_terminal_status(intent=intent, broker_order=broker_order)
            updated += 1

        return ReconciliationResult(
            observed_order_count=len(observed_orders),
            updated_order_count=updated,
            payload={"source": response.method_name},
        )

    def _save_observed_order(
        self,
        *,
        intent: OrderIntent,
        response: BrokerUnaryResponse,
    ) -> BrokerOrder:
        existing = self._orders.get_broker_order_by_request_order_id(intent.request_order_id)
        observed_at = datetime.now(tz=UTC)
        broker_status = _response_str(response.data, "broker_status") or _response_str(
            response.data,
            "status",
        )
        broker_order = BrokerOrder(
            calendar_date=intent.calendar_date,
            trading_date=intent.trading_date,
            session_type=intent.session_type,
            session_phase=intent.session_phase,
            micro_session_id=intent.micro_session_id,
            broker_trading_status=intent.broker_trading_status,
            order_intent_id=intent.order_intent_id,
            request_order_id=intent.request_order_id,
            exchange_order_id=_response_str(response.data, "exchange_order_id")
            or _response_str(response.data, "order_id"),
            broker_status=broker_status or "observed",
            lifecycle_seq=(existing.lifecycle_seq if existing is not None else 0) + 1,
            posted_at=existing.posted_at if existing is not None else None,
            cancelled_at=(
                observed_at
                if broker_status == "cancelled"
                else (existing.cancelled_at if existing is not None else None)
            ),
            rejected_at=(
                observed_at
                if broker_status == "rejected"
                else (existing.rejected_at if existing is not None else None)
            ),
            reject_reason_code=_response_str(response.data, "reject_reason_code"),
            broker_tracking_id=_response_str(response.headers, "x-tracking-id"),
            last_observed_at=observed_at,
            broker_payload={
                "method_name": response.method_name,
                "data": dict(response.data),
                "headers": dict(response.headers),
            },
        )
        return self._orders.upsert_broker_order_state(broker_order)

    def _sync_intent_terminal_status(
        self,
        *,
        intent: OrderIntent,
        broker_order: BrokerOrder,
    ) -> None:
        if broker_order.broker_status in TERMINAL_STATUSES:
            self._orders.update_intent_status(
                intent,
                status=broker_order.broker_status,
                terminal_ts=broker_order.last_observed_at,
                reject_reason_code=broker_order.reject_reason_code,
            )


def _uuid_from_mapping(payload: Mapping[object, object], key: str) -> UUID | None:
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    return None


def _response_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return str(value) if value is not None and str(value) else None

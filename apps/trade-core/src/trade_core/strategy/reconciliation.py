"""Broker reconciliation helpers for execution state convergence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from uuid import UUID

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    OrdersRequest,
    OrderStateRequest,
    RequestMetadata,
)
from trade_core.strategy.models import ReconciliationResult
from trading_common.db.models import BrokerOrder, FillEvent, OrderIntent, OrderStateEvent
from trading_common.db.repositories import OrderRepository
from trading_common.observability import DomainEventType
from trading_common.telemetry import bind_context, get_logger, log_event

TERMINAL_STATUSES = frozenset({"filled", "cancelled", "rejected"})
LOGGER = get_logger(__name__)


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
        started_at_monotonic = perf_counter()
        response = await self._broker_gateway.reconcile_order_state(
            OrderStateRequest(
                account_id=account_id,
                request_order_id=request_order_id,
                exchange_order_id=exchange_order_id,
            ),
            metadata=RequestMetadata(account_id=account_id, request_id=request_order_id),
        )
        latency_ms = _elapsed_ms(started_at_monotonic)
        intent = self._orders.get_intent_by_request_order_id(request_order_id)
        if intent is None:
            return ReconciliationResult(
                observed_order_count=1,
                updated_order_count=0,
                payload={"missing_intent_request_order_id": str(request_order_id)},
            )

        broker_order = self._save_observed_order(
            intent=intent,
            response=response,
            latency_ms=latency_ms,
        )
        fill_count = self._save_fills(intent=intent, broker_order=broker_order, response=response)
        self._sync_intent_terminal_status(intent=intent, broker_order=broker_order)
        return ReconciliationResult(
            observed_order_count=1,
            updated_order_count=1,
            payload={"request_order_id": str(request_order_id), "fill_count": fill_count},
        )

    async def reconcile_open_orders(self, *, account_id: str) -> ReconciliationResult:
        started_at_monotonic = perf_counter()
        response = await self._broker_gateway.reconcile_open_orders(
            OrdersRequest(account_id=account_id),
            metadata=RequestMetadata(account_id=account_id),
        )
        latency_ms = _elapsed_ms(started_at_monotonic)
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
            broker_order = self._save_observed_order(
                intent=intent,
                response=order_response,
                latency_ms=latency_ms,
            )
            self._save_fills(intent=intent, broker_order=broker_order, response=order_response)
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
        latency_ms: Decimal | None,
    ) -> BrokerOrder:
        existing = self._orders.get_broker_order_by_request_order_id(intent.request_order_id)
        previous_status = existing.broker_status if existing is not None else None
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
            candidate_id=intent.candidate_id,
            instrument_id=intent.instrument_id,
            timeframe=intent.timeframe,
            request_order_id=intent.request_order_id,
            exchange_order_id=_response_str(response.data, "exchange_order_id")
            or _response_str(response.data, "order_id"),
            tracking_id=_tracking_id(response.headers),
            broker_status=broker_status or "observed",
            lifecycle_seq=(existing.lifecycle_seq if existing is not None else 0) + 1,
            latency_ms=latency_ms,
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
            broker_tracking_id=_tracking_id(response.headers),
            last_observed_at=observed_at,
            broker_payload={
                "method_name": response.method_name,
                "data": dict(response.data),
                "headers": dict(response.headers),
                "latency_ms": str(latency_ms) if latency_ms is not None else None,
            },
        )
        persisted = self._orders.upsert_broker_order_state(broker_order)
        self._record_order_state_event(
            intent=intent,
            broker_order=persisted,
            response=response,
            previous_state=previous_status,
            observed_at=observed_at,
            latency_ms=latency_ms,
        )
        return persisted

    def _record_order_state_event(
        self,
        *,
        intent: OrderIntent,
        broker_order: BrokerOrder,
        response: BrokerUnaryResponse,
        previous_state: str | None,
        observed_at: datetime,
        latency_ms: Decimal | None,
    ) -> OrderStateEvent:
        event_type = _broker_event_type(broker_order.broker_status)
        event = self._orders.create_order_state_event_idempotent(
            OrderStateEvent(
                calendar_date=intent.calendar_date,
                trading_date=intent.trading_date,
                session_type=intent.session_type,
                session_phase=intent.session_phase,
                micro_session_id=intent.micro_session_id,
                broker_trading_status=intent.broker_trading_status,
                ts_utc=observed_at,
                exchange_ts=observed_at,
                received_ts=observed_at,
                candidate_id=intent.candidate_id,
                order_intent_id=intent.order_intent_id,
                broker_order_id=broker_order.broker_order_id,
                instrument_id=intent.instrument_id,
                timeframe=intent.timeframe,
                request_order_id=intent.request_order_id,
                exchange_order_id=broker_order.exchange_order_id,
                tracking_id=broker_order.tracking_id or broker_order.broker_tracking_id,
                state_seq=broker_order.lifecycle_seq,
                previous_state=previous_state,
                new_state=broker_order.broker_status,
                event_type=event_type,
                reason_code=broker_order.reject_reason_code,
                cancel_reason_code=None,
                reject_reason_code=broker_order.reject_reason_code,
                latency_ms=latency_ms,
                state_payload={
                    "source": "reconciliation",
                    "broker_method": response.method_name,
                    "broker_data": dict(response.data),
                    "broker_headers": dict(response.headers),
                },
            )
        )
        _log_order_event(
            event_type=event_type,
            intent=intent,
            broker_order=broker_order,
            stage_name="reconciliation_state",
            latency_ms=latency_ms,
            payload={"previous_state": previous_state, "new_state": broker_order.broker_status},
        )
        return event

    def _save_fills(
        self,
        *,
        intent: OrderIntent,
        broker_order: BrokerOrder,
        response: BrokerUnaryResponse,
    ) -> int:
        fills = response.data.get("fills", ())
        if not isinstance(fills, list | tuple):
            return 0
        saved = 0
        for index, raw_fill in enumerate(fills):
            if not isinstance(raw_fill, Mapping):
                continue
            exchange_order_id = (
                _response_str(raw_fill, "exchange_order_id")
                or broker_order.exchange_order_id
                or _response_str(response.data, "exchange_order_id")
            )
            if exchange_order_id is None:
                continue
            observed_at = datetime.now(tz=UTC)
            fill = self._orders.create_fill_event_idempotent(
                FillEvent(
                    calendar_date=intent.calendar_date,
                    trading_date=intent.trading_date,
                    session_type=intent.session_type,
                    session_phase=intent.session_phase,
                    micro_session_id=intent.micro_session_id,
                    broker_trading_status=intent.broker_trading_status,
                    ts_utc=_datetime_from(raw_fill, "ts_utc", default=observed_at),
                    exchange_ts=_datetime_from(raw_fill, "exchange_ts", default=observed_at),
                    received_ts=observed_at,
                    candidate_id=intent.candidate_id,
                    order_intent_id=intent.order_intent_id,
                    request_order_id=intent.request_order_id,
                    exchange_order_id=exchange_order_id,
                    tracking_id=broker_order.tracking_id or broker_order.broker_tracking_id,
                    broker_fill_id=_response_str(raw_fill, "broker_fill_id")
                    or _response_str(raw_fill, "fill_id")
                    or f"{exchange_order_id}:{index}",
                    instrument_id=intent.instrument_id,
                    timeframe=intent.timeframe,
                    side=_response_str(raw_fill, "side") or intent.side,
                    lot_qty=_int_from(raw_fill, "lot_qty", default=intent.lot_qty),
                    price=_decimal_from(raw_fill, "price", default=intent.intended_price),
                    commission=_decimal_from(raw_fill, "commission", default=Decimal("0")),
                    commission_gross=_decimal_or_none(raw_fill.get("commission_gross")),
                    commission_net=_decimal_or_none(raw_fill.get("commission_net")),
                    slippage_bp=_decimal_or_none(raw_fill.get("slippage_bp")),
                    pnl_gross=_decimal_or_none(raw_fill.get("pnl_gross")),
                    pnl_net=_decimal_or_none(raw_fill.get("pnl_net")),
                    liquidity_flag=_response_str(raw_fill, "liquidity_flag"),
                    fill_payload={"source": "reconciliation", "raw_fill": dict(raw_fill)},
                )
            )
            saved += 1
            _log_order_event(
                event_type=DomainEventType.FILL_RECEIVED.value,
                intent=intent,
                broker_order=broker_order,
                stage_name="fill_received",
                latency_ms=None,
                payload={
                    "broker_fill_id": fill.broker_fill_id,
                    "price": str(fill.price),
                    "lot_qty": fill.lot_qty,
                },
            )
        return saved

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
        elif broker_order.broker_status == "partially_filled":
            self._orders.update_intent_status(intent, status="partially_filled")


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


def _tracking_id(headers: Mapping[str, object]) -> str | None:
    return _response_str(headers, "x_tracking_id") or _response_str(headers, "x-tracking-id")


def _elapsed_ms(started_at_monotonic: float) -> Decimal:
    return Decimal(str((perf_counter() - started_at_monotonic) * 1000)).quantize(
        Decimal("0.0001")
    )


def _decimal_from(
    payload: Mapping[object, object],
    key: str,
    *,
    default: Decimal | None,
) -> Decimal:
    value = payload.get(key)
    if value is None:
        if default is None:
            msg = f"Fill payload does not contain decimal field: {key}"
            raise ValueError(msg)
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _int_from(payload: Mapping[object, object], key: str, *, default: int) -> int:
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return int(str(value))


def _datetime_from(
    payload: Mapping[object, object],
    key: str,
    *,
    default: datetime,
) -> datetime:
    value = payload.get(key)
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return default


def _broker_event_type(status: str) -> str:
    if status in {"posted", "pseudo_posted"}:
        return DomainEventType.BROKER_ORDER_POSTED.value
    if status == "cancelled":
        return DomainEventType.BROKER_ORDER_CANCELLED.value
    return DomainEventType.BROKER_ORDER_UPDATED.value


def _log_order_event(
    *,
    event_type: str,
    intent: OrderIntent,
    broker_order: BrokerOrder,
    stage_name: str,
    latency_ms: Decimal | None,
    payload: Mapping[str, object],
) -> None:
    with bind_context(
        session_type=intent.session_type,
        exchange_phase=intent.session_phase,
        micro_session_id=intent.micro_session_id,
        instrument=intent.instrument_id,
        timeframe=intent.timeframe,
        strategy_id=intent.strategy_id,
        strategy_version=str(intent.strategy_version) if intent.strategy_version else None,
        candidate_id=str(intent.candidate_id) if intent.candidate_id else None,
        order_intent_id=str(intent.order_intent_id),
        request_order_id=str(intent.request_order_id),
        exchange_order_id=broker_order.exchange_order_id,
        tracking_id=broker_order.tracking_id or broker_order.broker_tracking_id,
    ):
        log_event(
            logger=LOGGER,
            event_type=event_type,
            component="reconciliation.service",
            stage_name=stage_name,
            latency_ms=latency_ms,
            details=dict(payload),
        )

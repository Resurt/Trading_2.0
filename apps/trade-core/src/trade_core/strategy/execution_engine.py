"""Execution engine with idempotent order intent lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    CancelOrderRequest,
    InstrumentRef,
    OrderPlacementRequest,
    RequestMetadata,
)
from trade_core.strategy.models import (
    CancelReasonCode,
    OrderAction,
    OrderIntentRequest,
    OrderLifecycleResult,
)
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import BrokerOrder, OrderIntent
from trading_common.db.repositories import OrderRepository
from trading_common.observability import DomainEventType


class DefaultExecutionEngine:
    """Creates idempotent intents and talks to BrokerGateway, not to HTTP/API."""

    def __init__(
        self,
        *,
        broker_gateway: BrokerGateway,
        orders: OrderRepository,
        launch_policy: LaunchModePolicy | None = None,
        clock: CallableClock | None = None,
    ) -> None:
        self._broker_gateway = broker_gateway
        self._orders = orders
        self._launch_policy = launch_policy or LaunchModePolicy.from_mode(
            RuntimeMode.HISTORICAL_REPLAY
        )
        self._clock = clock or _utcnow

    def create_order_intent(self, request: OrderIntentRequest) -> OrderIntent:
        micro_session_id = request.session_snapshot.micro_session_id or "unassigned"
        request_order_id = request.request_order_id or uuid4()
        candidate_ref = str(request.candidate.candidate_id or request.candidate.signal_fingerprint)
        idempotency_key = request.idempotency_key or _idempotency_key(
            strategy_id=request.candidate.strategy_id,
            strategy_version=request.candidate.strategy_version,
            micro_session_id=micro_session_id,
            candidate_ref=candidate_ref,
            order_action=request.order_action.value,
        )
        intent = OrderIntent(
            calendar_date=request.session_snapshot.calendar_date,
            trading_date=request.session_snapshot.trading_date,
            session_type=request.session_snapshot.session_type.value,
            session_phase=request.session_snapshot.session_phase.value,
            micro_session_id=micro_session_id,
            broker_trading_status=request.session_snapshot.broker_trading_status,
            candidate_id=request.candidate.candidate_id,
            instrument_id=request.candidate.instrument.instrument_id,
            strategy_id=request.candidate.strategy_id,
            side=request.candidate.side.value,
            order_action=request.order_action.value,
            order_type=request.candidate.order_type,
            lot_qty=request.candidate.lot_qty,
            intended_price=request.candidate.intended_price,
            time_in_force=request.candidate.time_in_force,
            request_order_id=request_order_id,
            idempotency_key=idempotency_key,
            execution_policy_version=request.execution_policy_version,
            status="created",
            cancel_reason_code=None,
            reject_reason_code=None,
            created_ts=request.created_at,
            submitted_ts=None,
            terminal_ts=None,
            intent_payload={
                "event_type": DomainEventType.ORDER_INTENT_CREATED.value,
                "account_id": request.account_id,
                "run_id": str(request.run_id) if request.run_id is not None else None,
                "instrument_uid": request.candidate.instrument.instrument_uid,
                "ticker": request.candidate.instrument.ticker,
                "class_code": request.candidate.instrument.class_code,
                "timeframe": request.candidate.timeframe.value,
                "signal_fingerprint": request.candidate.signal_fingerprint,
                "condition_payload": request.candidate.condition_payload,
                "launch_mode": self._launch_policy.mode.value,
                "order_submission_mode": self._launch_policy.order_submission_mode,
            },
        )
        return self._orders.create_intent_idempotent(intent)

    async def post_order(self, intent: OrderIntent) -> OrderLifecycleResult:
        if not self._launch_policy.allows_real_orders:
            return self._post_pseudo_order(intent)

        account_id = _required_payload_str(intent.intent_payload, "account_id")
        request = OrderPlacementRequest(
            account_id=account_id,
            instrument=_instrument_from_intent(intent),
            side=intent.side,
            order_type=intent.order_type,
            lot_qty=intent.lot_qty,
            price=intent.intended_price,
            time_in_force=intent.time_in_force,
            client_order_key=intent.idempotency_key,
            request_order_id=intent.request_order_id,
            payload={
                "order_intent_id": str(intent.order_intent_id),
                "execution_policy_version": intent.execution_policy_version,
            },
        )
        response = await self._broker_gateway.post_order(
            request,
            metadata=RequestMetadata(
                account_id=account_id,
                request_id=intent.request_order_id,
                correlation_id=str(intent.order_intent_id),
            ),
        )
        observed_at = self._clock()
        self._orders.update_intent_status(
            intent,
            status="submitted",
            submitted_ts=observed_at,
            payload_patch={"last_broker_method": response.method_name},
        )
        broker_order = self._upsert_broker_order(
            intent=intent,
            response=response,
            observed_at=observed_at,
            default_status="posted",
            lifecycle_seq_increment=1,
            posted_at=observed_at,
        )
        return _lifecycle_result(intent=intent, broker_order=broker_order, response=response)

    async def cancel_order(
        self,
        intent: OrderIntent,
        *,
        account_id: str,
        cancel_reason_code: CancelReasonCode,
        cancel_payload: Mapping[str, object],
        exchange_order_id: str | None = None,
    ) -> OrderLifecycleResult:
        if not cancel_payload:
            msg = "cancel_payload must explain the machine-readable cancellation context"
            raise ValueError(msg)

        if not self._launch_policy.allows_real_orders:
            return self._cancel_pseudo_order(
                intent,
                account_id=account_id,
                cancel_reason_code=cancel_reason_code,
                cancel_payload=cancel_payload,
                exchange_order_id=exchange_order_id,
            )

        requested_at = self._clock()
        self._orders.update_intent_status(
            intent,
            status="cancel_requested",
            cancel_reason_code=cancel_reason_code.value,
            payload_patch={
                "cancel_reason_code": cancel_reason_code.value,
                "cancel_payload": dict(cancel_payload),
            },
        )
        response = await self._broker_gateway.cancel_order(
            CancelOrderRequest(
                account_id=account_id,
                request_order_id=intent.request_order_id,
                exchange_order_id=exchange_order_id,
                payload={
                    "order_intent_id": str(intent.order_intent_id),
                    "cancel_reason_code": cancel_reason_code.value,
                    "cancel_payload": dict(cancel_payload),
                },
            ),
            metadata=RequestMetadata(
                account_id=account_id,
                request_id=intent.request_order_id,
                correlation_id=str(intent.order_intent_id),
            ),
        )
        observed_at = self._clock()
        self._orders.update_intent_status(
            intent,
            status="cancelled",
            terminal_ts=observed_at,
            payload_patch={"cancel_requested_at": requested_at.isoformat()},
        )
        broker_order = self._upsert_broker_order(
            intent=intent,
            response=response,
            observed_at=observed_at,
            default_status="cancelled",
            lifecycle_seq_increment=1,
            cancelled_at=observed_at,
            exchange_order_id=exchange_order_id,
        )
        return _lifecycle_result(intent=intent, broker_order=broker_order, response=response)

    async def replace_order(
        self,
        old_intent: OrderIntent,
        new_request: OrderIntentRequest,
        *,
        cancel_reason_code: CancelReasonCode,
        cancel_payload: Mapping[str, object],
        exchange_order_id: str | None = None,
    ) -> tuple[OrderLifecycleResult, OrderLifecycleResult]:
        cancel_result = await self.cancel_order(
            old_intent,
            account_id=new_request.account_id,
            cancel_reason_code=cancel_reason_code,
            cancel_payload=cancel_payload,
            exchange_order_id=exchange_order_id,
        )
        replacement_intent = self.create_order_intent(
            replace(new_request, order_action=OrderAction.REPLACE)
        )
        post_result = await self.post_order(replacement_intent)
        return cancel_result, post_result

    def _post_pseudo_order(self, intent: OrderIntent) -> OrderLifecycleResult:
        observed_at = self._clock()
        reason_code = self._launch_policy.real_order_block_reason_code
        self._orders.update_intent_status(
            intent,
            status="pseudo_submitted",
            submitted_ts=observed_at,
            payload_patch={
                "launch_mode": self._launch_policy.mode.value,
                "order_submission_mode": self._launch_policy.order_submission_mode,
                "real_broker_call": False,
                "reason_code": reason_code,
            },
        )
        response = BrokerUnaryResponse(
            method_name="PseudoPostOrder",
            data={
                "broker_status": "pseudo_posted",
                "real_broker_call": False,
                "launch_mode": self._launch_policy.mode.value,
                "reason_code": reason_code,
            },
            headers={},
        )
        broker_order = self._upsert_broker_order(
            intent=intent,
            response=response,
            observed_at=observed_at,
            default_status="pseudo_posted",
            lifecycle_seq_increment=1,
            posted_at=observed_at,
        )
        return _lifecycle_result(intent=intent, broker_order=broker_order, response=response)

    def _cancel_pseudo_order(
        self,
        intent: OrderIntent,
        *,
        account_id: str,
        cancel_reason_code: CancelReasonCode,
        cancel_payload: Mapping[str, object],
        exchange_order_id: str | None,
    ) -> OrderLifecycleResult:
        requested_at = self._clock()
        self._orders.update_intent_status(
            intent,
            status="cancel_requested",
            cancel_reason_code=cancel_reason_code.value,
            payload_patch={
                "cancel_reason_code": cancel_reason_code.value,
                "cancel_payload": dict(cancel_payload),
                "launch_mode": self._launch_policy.mode.value,
                "order_submission_mode": self._launch_policy.order_submission_mode,
                "real_broker_call": False,
                "account_id": account_id,
            },
        )
        observed_at = self._clock()
        self._orders.update_intent_status(
            intent,
            status="cancelled",
            terminal_ts=observed_at,
            payload_patch={"cancel_requested_at": requested_at.isoformat()},
        )
        response = BrokerUnaryResponse(
            method_name="PseudoCancelOrder",
            data={
                "exchange_order_id": exchange_order_id,
                "broker_status": "cancelled",
                "real_broker_call": False,
                "launch_mode": self._launch_policy.mode.value,
                "cancel_reason_code": cancel_reason_code.value,
            },
            headers={},
        )
        broker_order = self._upsert_broker_order(
            intent=intent,
            response=response,
            observed_at=observed_at,
            default_status="cancelled",
            lifecycle_seq_increment=1,
            cancelled_at=observed_at,
            exchange_order_id=exchange_order_id,
        )
        return _lifecycle_result(intent=intent, broker_order=broker_order, response=response)

    def _upsert_broker_order(
        self,
        *,
        intent: OrderIntent,
        response: BrokerUnaryResponse,
        observed_at: datetime,
        default_status: str,
        lifecycle_seq_increment: int,
        posted_at: datetime | None = None,
        cancelled_at: datetime | None = None,
        rejected_at: datetime | None = None,
        exchange_order_id: str | None = None,
    ) -> BrokerOrder:
        existing = self._orders.get_broker_order_by_request_order_id(intent.request_order_id)
        lifecycle_seq = (
            existing.lifecycle_seq if existing is not None else 0
        ) + lifecycle_seq_increment
        status = _response_str(response.data, "broker_status") or _response_str(
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
            exchange_order_id=exchange_order_id
            or _response_str(response.data, "exchange_order_id")
            or _response_str(response.data, "order_id"),
            broker_status=status or default_status,
            lifecycle_seq=lifecycle_seq,
            posted_at=posted_at or (existing.posted_at if existing is not None else None),
            cancelled_at=cancelled_at,
            rejected_at=rejected_at,
            reject_reason_code=_response_str(response.data, "reject_reason_code"),
            broker_tracking_id=_response_str(response.headers, "x-tracking-id"),
            last_observed_at=observed_at,
            broker_payload={
                "event_type": _broker_event_type(default_status),
                "method_name": response.method_name,
                "data": _json_payload(response.data),
                "headers": _json_payload(response.headers),
            },
        )
        return self._orders.upsert_broker_order_state(broker_order)


type CallableClock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _idempotency_key(
    *,
    strategy_id: str,
    strategy_version: int,
    micro_session_id: str,
    candidate_ref: str,
    order_action: str,
) -> str:
    raw = f"{strategy_id}:{strategy_version}:{micro_session_id}:{candidate_ref}:{order_action}"
    if len(raw) <= 160:
        return raw
    return f"{strategy_id}:{strategy_version}:{candidate_ref[:32]}:{order_action}"


def _required_payload_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        msg = f"Order intent payload does not contain required string field: {key}"
        raise ValueError(msg)
    return value


def _instrument_from_intent(intent: OrderIntent) -> InstrumentRef:
    return InstrumentRef(
        instrument_id=intent.instrument_id,
        instrument_uid=_optional_payload_str(intent.intent_payload, "instrument_uid"),
        class_code=_optional_payload_str(intent.intent_payload, "class_code"),
        ticker=_optional_payload_str(intent.intent_payload, "ticker"),
    )


def _optional_payload_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _response_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return str(value) if value is not None and str(value) else None


def _json_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return dict(payload)


def _lifecycle_result(
    *,
    intent: OrderIntent,
    broker_order: BrokerOrder,
    response: BrokerUnaryResponse,
) -> OrderLifecycleResult:
    return OrderLifecycleResult(
        order_intent_id=intent.order_intent_id,
        request_order_id=intent.request_order_id,
        status=intent.status,
        exchange_order_id=broker_order.exchange_order_id,
        broker_status=broker_order.broker_status,
        payload={
            "event_type": _broker_event_type(broker_order.broker_status),
            "broker_method": response.method_name,
            "broker_data": _json_payload(response.data),
            "broker_headers": _json_payload(response.headers),
        },
    )


def _broker_event_type(status: str) -> str:
    if status in {"posted", "pseudo_posted"}:
        return DomainEventType.BROKER_ORDER_POSTED.value
    if status == "cancelled":
        return DomainEventType.BROKER_ORDER_CANCELLED.value
    return DomainEventType.BROKER_ORDER_UPDATED.value

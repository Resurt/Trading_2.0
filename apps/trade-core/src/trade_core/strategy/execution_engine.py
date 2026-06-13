"""Execution engine with idempotent order intent lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
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
from trading_common.db.models import BrokerOrder, OrderIntent, OrderStateEvent
from trading_common.db.repositories import OrderRepository
from trading_common.observability import DomainEventType
from trading_common.telemetry import bind_context, get_logger, log_event

LOGGER = get_logger(__name__)


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
            timeframe=request.candidate.timeframe.value,
            strategy_id=request.candidate.strategy_id,
            strategy_version=request.candidate.strategy_version,
            side=request.candidate.side.value,
            order_action=request.order_action.value,
            order_type=request.candidate.order_type,
            lot_qty=request.candidate.lot_qty,
            intended_price=request.candidate.intended_price,
            time_in_force=request.candidate.time_in_force,
            request_order_id=request_order_id,
            tracking_id=None,
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
        persisted = self._orders.create_intent_idempotent(intent)
        _log_order_event(
            event_type=DomainEventType.ORDER_INTENT_CREATED.value,
            intent=persisted,
            stage_name="order_intent_creation",
            payload={"idempotency_key": persisted.idempotency_key},
        )
        return persisted

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
        started_at_monotonic = perf_counter()
        response = await self._broker_gateway.post_order(
            request,
            metadata=RequestMetadata(
                account_id=account_id,
                request_id=intent.request_order_id,
                correlation_id=str(intent.order_intent_id),
            ),
        )
        latency_ms = _elapsed_ms(started_at_monotonic)
        observed_at = self._clock()
        broker_status = _broker_status(response, "posted")
        reject_reason_code = _response_str(response.data, "reject_reason_code")
        terminal_ts = observed_at if broker_status == "rejected" else None
        self._orders.update_intent_status(
            intent,
            status="rejected" if broker_status == "rejected" else "submitted",
            submitted_ts=observed_at,
            terminal_ts=terminal_ts,
            reject_reason_code=reject_reason_code,
            payload_patch={
                "last_broker_method": response.method_name,
                "broker_status": broker_status,
                "broker_latency_ms": str(latency_ms),
                "tracking_id": _tracking_id(response.headers),
                "rate_limit": _rate_limit_payload(response.headers),
            },
        )
        broker_order = self._upsert_broker_order(
            intent=intent,
            response=response,
            observed_at=observed_at,
            default_status="posted",
            lifecycle_seq_increment=1,
            posted_at=observed_at if broker_status != "rejected" else None,
            rejected_at=observed_at if broker_status == "rejected" else None,
            latency_ms=latency_ms,
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
        started_at_monotonic = perf_counter()
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
        latency_ms = _elapsed_ms(started_at_monotonic)
        observed_at = self._clock()
        self._orders.update_intent_status(
            intent,
            status="cancelled",
            terminal_ts=observed_at,
            payload_patch={
                "cancel_requested_at": requested_at.isoformat(),
                "broker_latency_ms": str(latency_ms),
                "tracking_id": _tracking_id(response.headers),
                "rate_limit": _rate_limit_payload(response.headers),
            },
        )
        broker_order = self._upsert_broker_order(
            intent=intent,
            response=response,
            observed_at=observed_at,
            default_status="cancelled",
            lifecycle_seq_increment=1,
            cancelled_at=observed_at,
            exchange_order_id=exchange_order_id,
            latency_ms=latency_ms,
            cancel_reason_code=cancel_reason_code.value,
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
            latency_ms=Decimal("0"),
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
            latency_ms=Decimal("0"),
            cancel_reason_code=cancel_reason_code.value,
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
        latency_ms: Decimal | int | float | None = None,
        cancel_reason_code: str | None = None,
    ) -> BrokerOrder:
        existing = self._orders.get_broker_order_by_request_order_id(intent.request_order_id)
        previous_status = existing.broker_status if existing is not None else None
        lifecycle_seq = (
            existing.lifecycle_seq if existing is not None else 0
        ) + lifecycle_seq_increment
        status = _response_str(response.data, "broker_status") or _response_str(
            response.data,
            "status",
        )
        actual_status = status or default_status
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
            exchange_order_id=exchange_order_id
            or _response_str(response.data, "exchange_order_id")
            or _response_str(response.data, "order_id"),
            tracking_id=_tracking_id(response.headers),
            broker_status=actual_status,
            lifecycle_seq=lifecycle_seq,
            latency_ms=_decimal_or_none(latency_ms),
            posted_at=posted_at or (existing.posted_at if existing is not None else None),
            cancelled_at=cancelled_at,
            rejected_at=rejected_at,
            reject_reason_code=_response_str(response.data, "reject_reason_code"),
            broker_tracking_id=_tracking_id(response.headers),
            last_observed_at=observed_at,
            broker_payload={
                "event_type": _broker_event_type(actual_status),
                "method_name": response.method_name,
                "data": _json_payload(response.data),
                "headers": _json_payload(response.headers),
                "latency_ms": str(latency_ms) if latency_ms is not None else None,
                "rate_limit": _rate_limit_payload(response.headers),
            },
        )
        persisted = self._orders.upsert_broker_order_state(broker_order)
        self._record_order_state_event(
            intent=intent,
            broker_order=persisted,
            response=response,
            observed_at=observed_at,
            previous_state=previous_status,
            latency_ms=_decimal_or_none(latency_ms),
            cancel_reason_code=cancel_reason_code,
        )
        return persisted

    def _record_order_state_event(
        self,
        *,
        intent: OrderIntent,
        broker_order: BrokerOrder,
        response: BrokerUnaryResponse,
        observed_at: datetime,
        previous_state: str | None,
        latency_ms: Decimal | None,
        cancel_reason_code: str | None,
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
                reason_code=cancel_reason_code or broker_order.reject_reason_code,
                cancel_reason_code=cancel_reason_code,
                reject_reason_code=broker_order.reject_reason_code,
                latency_ms=latency_ms,
                state_payload={
                    "broker_method": response.method_name,
                    "broker_data": _json_payload(response.data),
                    "broker_headers": _json_payload(response.headers),
                    "rate_limit": _rate_limit_payload(response.headers),
                },
            )
        )
        _log_order_event(
            event_type=event_type,
            intent=intent,
            stage_name="broker_order_state",
            latency_ms=latency_ms,
            tracking_id=event.tracking_id,
            exchange_order_id=event.exchange_order_id,
            payload={
                "previous_state": previous_state,
                "new_state": broker_order.broker_status,
                "cancel_reason_code": cancel_reason_code,
                "reject_reason_code": broker_order.reject_reason_code,
                "rate_limit": _rate_limit_payload(response.headers),
            },
        )
        return event


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


def _broker_status(response: BrokerUnaryResponse, default_status: str) -> str:
    return (
        _response_str(response.data, "broker_status")
        or _response_str(response.data, "status")
        or default_status
    )


def _tracking_id(headers: Mapping[str, object]) -> str | None:
    return _response_str(headers, "x_tracking_id") or _response_str(headers, "x-tracking-id")


def _rate_limit_payload(headers: Mapping[str, object]) -> dict[str, object | None]:
    return {
        "limit": _response_str(headers, "x_ratelimit_limit")
        or _response_str(headers, "x-ratelimit-limit"),
        "remaining": _response_str(headers, "x_ratelimit_remaining")
        or _response_str(headers, "x-ratelimit-remaining"),
        "reset": _response_str(headers, "x_ratelimit_reset")
        or _response_str(headers, "x-ratelimit-reset"),
    }


def _elapsed_ms(started_at_monotonic: float) -> Decimal:
    return Decimal(str((perf_counter() - started_at_monotonic) * 1000)).quantize(
        Decimal("0.0001")
    )


def _decimal_or_none(value: Decimal | int | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


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


def _log_order_event(
    *,
    event_type: str,
    intent: OrderIntent,
    stage_name: str,
    payload: Mapping[str, object],
    latency_ms: Decimal | None = None,
    tracking_id: str | None = None,
    exchange_order_id: str | None = None,
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
        exchange_order_id=exchange_order_id,
        tracking_id=tracking_id,
    ):
        log_event(
            logger=LOGGER,
            event_type=event_type,
            component="execution.engine",
            stage_name=stage_name,
            latency_ms=latency_ms,
            details=dict(payload),
        )

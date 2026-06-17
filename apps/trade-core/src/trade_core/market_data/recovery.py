"""Stream gap recovery after market/order stream reconnects."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any
from uuid import UUID

from trade_core.broker_gateway import (
    BrokerGateway,
    CandleRequest,
    InstrumentRef,
    OrdersRequest,
    OrderStateRequest,
)
from trade_core.market_data.event_bus import MarketEventBus
from trade_core.market_data.events import MarketDataEvent, MarketEventType, Timeframe, ensure_utc
from trade_core.market_data.subscriptions import candle_from_mapping
from trading_common import TradingMetrics
from trading_common.observability import DomainEventType
from trading_common.telemetry import get_logger, log_event

RefreshPositionsHook = Callable[[str], Awaitable[object] | object]
AuditEventHook = Callable[[str, dict[str, object]], Awaitable[object] | object]
RecoveryFailureHook = Callable[[Exception], Awaitable[object] | object]
LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GapRecoveryRequest:
    """Inputs for one reconnect/gap recovery run."""

    instruments: tuple[InstrumentRef, ...]
    candle_timeframes: tuple[Timeframe, ...]
    from_ts_utc: datetime
    to_ts_utc: datetime
    account_id: str | None = None
    stream_name: str = "market_data"
    working_order_request_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class StreamGapRecoveryResult:
    """Machine-readable result of one gap recovery attempt."""

    recovered_candles: int
    open_orders_refreshed: bool
    order_states_refreshed: int
    positions_refreshed: bool
    duration_seconds: float


class StreamGapRecoveryService:
    """Backfill closed candles and reconcile account state after stream gaps."""

    def __init__(
        self,
        *,
        broker_gateway: BrokerGateway,
        event_bus: MarketEventBus,
        refresh_positions_hook: RefreshPositionsHook | None = None,
        metrics: TradingMetrics | None = None,
        audit_event_hook: AuditEventHook | None = None,
        on_failure: RecoveryFailureHook | None = None,
    ) -> None:
        self._broker_gateway = broker_gateway
        self._event_bus = event_bus
        self._refresh_positions_hook = refresh_positions_hook
        self._metrics = metrics
        self._audit_event_hook = audit_event_hook
        self._on_failure = on_failure
        self._last_good_event_ts: dict[tuple[str, str, str], datetime] = {}

    def record_good_event(
        self,
        *,
        stream_name: str,
        instrument_id: str | None,
        timeframe: str | Timeframe | None,
        ts_utc: datetime,
    ) -> None:
        """Track the latest trusted event timestamp by stream/instrument/timeframe."""

        key = _last_good_key(stream_name, instrument_id, timeframe)
        current = self._last_good_event_ts.get(key)
        normalized = ensure_utc(ts_utc)
        if current is None or normalized > current:
            self._last_good_event_ts[key] = normalized

    def last_good_event_ts(
        self,
        *,
        stream_name: str,
        instrument_id: str | None,
        timeframe: str | Timeframe | None,
    ) -> datetime | None:
        """Return the latest trusted event timestamp for one stream scope."""

        return self._last_good_event_ts.get(
            _last_good_key(stream_name, instrument_id, timeframe)
        )

    async def recover_after_reconnect(
        self,
        request: GapRecoveryRequest,
    ) -> StreamGapRecoveryResult:
        """Run candle backfill, order reconciliation and position refresh."""

        started_at = ensure_utc(datetime.now().astimezone())
        started_monotonic = perf_counter()
        if self._metrics is not None:
            self._metrics.inc_stream_reconnect(
                stream_type=request.stream_name,
                result="attempt",
            )
        log_event(
            logger=LOGGER,
            level="WARNING",
            event_type=DomainEventType.STREAM_GAP_RECOVERY_REQUESTED.value,
            component="market_data.recovery",
            stream_name=request.stream_name,
            instrument_count=len(request.instruments),
            timeframes=[timeframe.value for timeframe in request.candle_timeframes],
            from_ts_utc=request.from_ts_utc.isoformat(),
            to_ts_utc=request.to_ts_utc.isoformat(),
            account_id_present=request.account_id is not None,
        )
        await self._write_audit(
            DomainEventType.STREAM_GAP_RECOVERY_REQUESTED.value,
            _request_payload(request),
        )
        await self._event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.RECOVERY_REQUESTED,
                payload=request,
                ts_utc=started_at,
                instrument_id=None,
            )
        )

        try:
            recovered_candles = await self._backfill_candles(request)
            open_orders_refreshed = False
            order_states_refreshed = 0
            positions_refreshed = False
            if request.account_id is not None:
                await self._broker_gateway.reconcile_open_orders(OrdersRequest(request.account_id))
                open_orders_refreshed = True
                await self._write_audit(
                    DomainEventType.ORDER_RECONCILIATION_COMPLETED.value,
                    {
                        "account_id": request.account_id,
                        "reconciled_scope": "open_orders",
                    },
                )
                for request_order_id in request.working_order_request_ids:
                    await self._broker_gateway.reconcile_order_state(
                        OrderStateRequest(
                            account_id=request.account_id,
                            request_order_id=request_order_id,
                        )
                    )
                    order_states_refreshed += 1
                if order_states_refreshed:
                    await self._write_audit(
                        DomainEventType.ORDER_RECONCILIATION_COMPLETED.value,
                        {
                            "account_id": request.account_id,
                            "reconciled_scope": "working_orders",
                            "order_states_refreshed": order_states_refreshed,
                        },
                    )
                if self._refresh_positions_hook is not None:
                    result = self._refresh_positions_hook(request.account_id)
                    if inspect.isawaitable(result):
                        await result
                    positions_refreshed = True
                    await self._write_audit(
                        DomainEventType.POSITION_RECONCILIATION_COMPLETED.value,
                        {"account_id": request.account_id},
                    )
        except Exception as exc:
            duration = perf_counter() - started_monotonic
            if self._metrics is not None:
                self._metrics.inc_stream_reconnect(
                    stream_type=request.stream_name,
                    result="failed",
                )
                self._metrics.observe_gap_recovery_duration(
                    duration,
                    stream_type=request.stream_name,
                    status="failed",
                )
            log_event(
                logger=LOGGER,
                level="ERROR",
                event_type=DomainEventType.STREAM_GAP_RECOVERY_FAILED.value,
                component="market_data.recovery",
                stream_name=request.stream_name,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            await self._write_audit(
                DomainEventType.STREAM_GAP_RECOVERY_FAILED.value,
                {
                    **_request_payload(request),
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            if self._on_failure is not None:
                failure_result = self._on_failure(exc)
                if inspect.isawaitable(failure_result):
                    await failure_result
            raise

        completed_at = ensure_utc(datetime.now().astimezone())
        duration = perf_counter() - started_monotonic
        if self._metrics is not None:
            self._metrics.inc_stream_reconnect(stream_type=request.stream_name, result="success")
            self._metrics.observe_gap_recovery_duration(
                duration,
                stream_type=request.stream_name,
                status="success",
            )
        log_event(
            logger=LOGGER,
            event_type=DomainEventType.STREAM_GAP_RECOVERY_COMPLETED.value,
            component="market_data.recovery",
            stream_name=request.stream_name,
            recovered_candles=recovered_candles,
            open_orders_refreshed=open_orders_refreshed,
            order_states_refreshed=order_states_refreshed,
            positions_refreshed=positions_refreshed,
        )
        await self._event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.RECOVERY_COMPLETED,
                payload={
                    "recovered_candles": recovered_candles,
                    "open_orders_refreshed": open_orders_refreshed,
                    "order_states_refreshed": order_states_refreshed,
                    "positions_refreshed": positions_refreshed,
                },
                ts_utc=completed_at,
                instrument_id=None,
            )
        )
        return StreamGapRecoveryResult(
            recovered_candles=recovered_candles,
            open_orders_refreshed=open_orders_refreshed,
            order_states_refreshed=order_states_refreshed,
            positions_refreshed=positions_refreshed,
            duration_seconds=duration,
        )

    async def _backfill_candles(self, request: GapRecoveryRequest) -> int:
        await self._write_audit(
            DomainEventType.STREAM_GAP_BACKFILL_STARTED.value,
            _request_payload(request),
        )
        log_event(
            logger=LOGGER,
            event_type=DomainEventType.STREAM_GAP_BACKFILL_STARTED.value,
            component="market_data.recovery",
            stream_name=request.stream_name,
            instrument_count=len(request.instruments),
            timeframes=[timeframe.value for timeframe in request.candle_timeframes],
        )
        recovered_candles = 0
        for instrument in request.instruments:
            for timeframe in request.candle_timeframes:
                recovery_cursor = (
                    self.last_good_event_ts(
                        stream_name=request.stream_name,
                        instrument_id=instrument.instrument_id,
                        timeframe=timeframe,
                    )
                    or ensure_utc(request.from_ts_utc)
                )
                response = await self._broker_gateway.get_candles(
                    CandleRequest(
                        instrument=instrument,
                        interval=timeframe.value,
                        from_=recovery_cursor,
                        to=request.to_ts_utc,
                    )
                )
                for candle_payload in _iter_candle_payloads(response.data):
                    candle = candle_from_mapping(candle_payload, received_at=request.to_ts_utc)
                    if not candle.is_closed:
                        continue
                    candle_close_ts = ensure_utc(candle.close_ts_utc)
                    if candle_close_ts <= recovery_cursor:
                        if self._metrics is not None:
                            self._metrics.inc_recovered_candle(
                                instrument=candle.instrument_id,
                                timeframe=candle.timeframe.value,
                                status="duplicate",
                            )
                        continue
                    await self._event_bus.publish(
                        MarketDataEvent(
                            event_type=MarketEventType.CANDLE,
                            payload=candle,
                            ts_utc=request.to_ts_utc,
                            instrument_id=candle.instrument_id,
                        )
                    )
                    self.record_good_event(
                        stream_name=request.stream_name,
                        instrument_id=candle.instrument_id,
                        timeframe=candle.timeframe,
                        ts_utc=candle.close_ts_utc,
                    )
                    recovery_cursor = candle_close_ts
                    if self._metrics is not None:
                        self._metrics.inc_recovered_candle(
                            instrument=candle.instrument_id,
                            timeframe=candle.timeframe.value,
                            status="success",
                        )
                    recovered_candles += 1
        await self._write_audit(
            DomainEventType.STREAM_GAP_BACKFILL_COMPLETED.value,
            {
                **_request_payload(request),
                "recovered_candles": recovered_candles,
            },
        )
        log_event(
            logger=LOGGER,
            event_type=DomainEventType.STREAM_GAP_BACKFILL_COMPLETED.value,
            component="market_data.recovery",
            stream_name=request.stream_name,
            recovered_candles=recovered_candles,
        )
        return recovered_candles

    async def _write_audit(self, event_type: str, payload: dict[str, object]) -> None:
        if self._audit_event_hook is None:
            return
        result = self._audit_event_hook(event_type, payload)
        if inspect.isawaitable(result):
            await result


class GapRecoveryCoordinator(StreamGapRecoveryService):
    """Backward-compatible name for the stream gap recovery service."""


def _iter_candle_payloads(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    candles = data.get("candles", ())
    if not isinstance(candles, Iterable):
        return ()
    return (dict(candle) for candle in candles if isinstance(candle, dict))


def _last_good_key(
    stream_name: str,
    instrument_id: str | None,
    timeframe: str | Timeframe | None,
) -> tuple[str, str, str]:
    timeframe_value = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
    return (stream_name, instrument_id or "all", timeframe_value or "all")


def _request_payload(request: GapRecoveryRequest) -> dict[str, object]:
    return {
        "stream_name": request.stream_name,
        "instruments": [instrument.instrument_id for instrument in request.instruments],
        "timeframes": [timeframe.value for timeframe in request.candle_timeframes],
        "from_ts_utc": request.from_ts_utc.isoformat(),
        "to_ts_utc": request.to_ts_utc.isoformat(),
        "account_id_present": request.account_id is not None,
        "working_order_count": len(request.working_order_request_ids),
    }

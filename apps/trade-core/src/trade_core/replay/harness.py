"""Deterministic replay harness for controlled launch validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from trade_core.market_data import Bar, BarEngine, Candle
from trade_core.session import (
    HourlyMicroSessionManager,
    InMemorySessionStateStore,
    MicroSessionEvent,
    SessionSnapshot,
)

JsonPayload = dict[str, object]


class ReplayEventType(StrEnum):
    """Events accepted by the replay harness."""

    CANDLE = "candle"
    SESSION_SNAPSHOT = "session_snapshot"
    BLOCKER_TRIGGERED = "blocker_triggered"
    CANCELLED_ORDER = "cancelled_order"
    COUNTERFACTUAL_SOURCE = "counterfactual_source"


@dataclass(frozen=True, slots=True)
class ReplayCounterfactualCase:
    """Minimal blocked/cancelled source used by replay counterfactual checks."""

    source_event_type: str
    instrument_id: str
    strategy_id: str
    side: str
    event_ts: datetime
    entry_price: Decimal
    lot_qty: int
    candidate_id: UUID | None = None
    order_intent_id: UUID | None = None
    blocker_code: str | None = None
    cancel_reason_code: str | None = None

    def as_payload(self) -> JsonPayload:
        return {
            "source_event_type": self.source_event_type,
            "instrument_id": self.instrument_id,
            "strategy_id": self.strategy_id,
            "side": self.side,
            "event_ts": self.event_ts.isoformat(),
            "entry_price": str(self.entry_price),
            "lot_qty": self.lot_qty,
            "candidate_id": str(self.candidate_id) if self.candidate_id is not None else None,
            "order_intent_id": (
                str(self.order_intent_id) if self.order_intent_id is not None else None
            ),
            "blocker_code": self.blocker_code,
            "cancel_reason_code": self.cancel_reason_code,
        }


CounterfactualReplayCallback = Callable[
    [Sequence[ReplayCounterfactualCase], Sequence[Candle]],
    Sequence[Mapping[str, object]],
]


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """One replayable market/session/analytics event."""

    ts_utc: datetime
    event_type: ReplayEventType
    payload: object


@dataclass(frozen=True, slots=True)
class ReplayRunResult:
    """Summary produced by a replay run."""

    processed_events: int
    closed_bars: tuple[Bar, ...]
    closed_candles: tuple[Candle, ...]
    micro_session_events: tuple[MicroSessionEvent, ...]
    blocker_events: tuple[Mapping[str, object], ...]
    cancelled_orders: tuple[Mapping[str, object], ...]
    counterfactual_sources: tuple[ReplayCounterfactualCase, ...]
    counterfactual_results: tuple[Mapping[str, object], ...]

    @property
    def session_rollover_verified(self) -> bool:
        event_types = {event.event_type for event in self.micro_session_events}
        return {"session_run_closed", "report_requested"}.issubset(event_types)

    @property
    def blocker_pipeline_verified(self) -> bool:
        return bool(self.blocker_events)

    @property
    def counterfactual_pipeline_verified(self) -> bool:
        return bool(self.counterfactual_sources) and bool(self.counterfactual_results)

    def as_payload(self) -> JsonPayload:
        return {
            "processed_events": self.processed_events,
            "closed_bar_count": len(self.closed_bars),
            "closed_candle_count": len(self.closed_candles),
            "micro_session_event_types": [
                event.event_type for event in self.micro_session_events
            ],
            "blocker_count": len(self.blocker_events),
            "cancelled_order_count": len(self.cancelled_orders),
            "counterfactual_source_count": len(self.counterfactual_sources),
            "counterfactual_result_count": len(self.counterfactual_results),
            "session_rollover_verified": self.session_rollover_verified,
            "blocker_pipeline_verified": self.blocker_pipeline_verified,
            "counterfactual_pipeline_verified": self.counterfactual_pipeline_verified,
        }


@dataclass(slots=True)
class ReplayHarness:
    """Replay candles/events through launch-critical deterministic pipelines."""

    bar_engine: BarEngine = field(default_factory=BarEngine)
    micro_sessions: HourlyMicroSessionManager = field(
        default_factory=lambda: HourlyMicroSessionManager(store=InMemorySessionStateStore())
    )
    counterfactual_callback: CounterfactualReplayCallback | None = None

    def run(self, events: Sequence[ReplayEvent]) -> ReplayRunResult:
        closed_bars: list[Bar] = []
        closed_candles: list[Candle] = []
        micro_session_events: list[MicroSessionEvent] = []
        blockers: list[Mapping[str, object]] = []
        cancelled_orders: list[Mapping[str, object]] = []
        counterfactual_sources: list[ReplayCounterfactualCase] = []

        for event in sorted(events, key=lambda item: item.ts_utc):
            if event.event_type is ReplayEventType.CANDLE:
                candle = _require_payload(event, Candle)
                if candle.is_closed:
                    closed_candles.append(candle)
                closed_bars.extend(self.bar_engine.on_candle(candle))
            elif event.event_type is ReplayEventType.SESSION_SNAPSHOT:
                snapshot = _require_payload(event, SessionSnapshot)
                tick = self.micro_sessions.on_snapshot(snapshot)
                micro_session_events.extend(tick.events)
            elif event.event_type is ReplayEventType.BLOCKER_TRIGGERED:
                blockers.append(_require_mapping_payload(event))
            elif event.event_type is ReplayEventType.CANCELLED_ORDER:
                cancelled_orders.append(_require_mapping_payload(event))
            elif event.event_type is ReplayEventType.COUNTERFACTUAL_SOURCE:
                counterfactual_sources.append(_require_payload(event, ReplayCounterfactualCase))

        counterfactual_results: Sequence[Mapping[str, object]] = ()
        if self.counterfactual_callback is not None and counterfactual_sources:
            counterfactual_results = self.counterfactual_callback(
                tuple(counterfactual_sources),
                tuple(closed_candles),
            )

        return ReplayRunResult(
            processed_events=len(events),
            closed_bars=tuple(closed_bars),
            closed_candles=tuple(closed_candles),
            micro_session_events=tuple(micro_session_events),
            blocker_events=tuple(blockers),
            cancelled_orders=tuple(cancelled_orders),
            counterfactual_sources=tuple(counterfactual_sources),
            counterfactual_results=tuple(counterfactual_results),
        )


def _require_payload[PayloadT](event: ReplayEvent, expected_type: type[PayloadT]) -> PayloadT:
    if isinstance(event.payload, expected_type):
        return event.payload
    msg = f"{event.event_type.value} replay event expects {expected_type.__name__} payload"
    raise TypeError(msg)


def _require_mapping_payload(event: ReplayEvent) -> Mapping[str, object]:
    if isinstance(event.payload, Mapping):
        return event.payload
    msg = f"{event.event_type.value} replay event expects mapping payload"
    raise TypeError(msg)

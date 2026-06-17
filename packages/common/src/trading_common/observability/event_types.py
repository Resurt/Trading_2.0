"""Strict domain event type names used across logs, Postgres events, and reports."""

from enum import StrEnum


class DomainEventType(StrEnum):
    SIGNAL_CANDIDATE_CREATED = "signal_candidate_created"
    CANDIDATE_STAGE_RESULT_RECORDED = "candidate_stage_result_recorded"
    MARKET_CONTEXT_SNAPSHOT_WRITTEN = "market_context_snapshot_written"
    BLOCKER_TRIGGERED = "blocker_triggered"
    ORDER_INTENT_CREATED = "order_intent_created"
    BROKER_ORDER_POSTED = "broker_order_posted"
    BROKER_ORDER_UPDATED = "broker_order_updated"
    BROKER_ORDER_CANCELLED = "broker_order_cancelled"
    ORDER_STATE_CHANGED = "order_state_changed"
    FILL_RECEIVED = "fill_received"
    STRATEGY_STATE_CHANGED = "strategy_state_changed"
    RISK_EVENT_RECORDED = "risk_event_recorded"
    SESSION_SNAPSHOT_WRITTEN = "session_snapshot_written"
    MARKET_STATUS_CHANGED = "market_status_changed"
    BAR_CLOSED = "bar_closed"
    STREAM_GAP_RECOVERY_REQUESTED = "stream_gap_recovery_requested"
    STREAM_GAP_BACKFILL_STARTED = "stream_gap_backfill_started"
    STREAM_GAP_BACKFILL_COMPLETED = "stream_gap_backfill_completed"
    STREAM_GAP_RECOVERY_COMPLETED = "stream_gap_recovery_completed"
    STREAM_GAP_RECOVERY_FAILED = "stream_gap_recovery_failed"
    ORDER_RECONCILIATION_COMPLETED = "order_reconciliation_completed"
    POSITION_RECONCILIATION_COMPLETED = "position_reconciliation_completed"


STRICT_DOMAIN_EVENT_TYPES = tuple(event_type.value for event_type in DomainEventType)


def validate_domain_event_type(value: str) -> DomainEventType:
    """Parse a strict event type or raise a clear error."""

    return DomainEventType(value)

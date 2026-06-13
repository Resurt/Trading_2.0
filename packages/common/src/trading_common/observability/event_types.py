"""Strict domain event type names used across logs, Postgres events, and reports."""

from enum import StrEnum


class DomainEventType(StrEnum):
    SIGNAL_CANDIDATE_CREATED = "signal_candidate_created"
    BLOCKER_TRIGGERED = "blocker_triggered"
    ORDER_INTENT_CREATED = "order_intent_created"
    BROKER_ORDER_POSTED = "broker_order_posted"
    BROKER_ORDER_UPDATED = "broker_order_updated"
    BROKER_ORDER_CANCELLED = "broker_order_cancelled"
    FILL_RECEIVED = "fill_received"
    STRATEGY_STATE_CHANGED = "strategy_state_changed"
    RISK_EVENT_RECORDED = "risk_event_recorded"
    SESSION_SNAPSHOT_WRITTEN = "session_snapshot_written"


STRICT_DOMAIN_EVENT_TYPES = tuple(event_type.value for event_type in DomainEventType)


def validate_domain_event_type(value: str) -> DomainEventType:
    """Parse a strict event type or raise a clear error."""

    return DomainEventType(value)

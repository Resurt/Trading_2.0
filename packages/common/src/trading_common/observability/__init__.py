"""Observability helpers shared by backend services."""

from trading_common.observability.context import (
    CONTEXT_FIELDS,
    bind_context,
    clear_log_context,
    context_from_mapping,
    get_context,
    get_log_context,
    log_context,
    set_context,
    set_log_context,
    unbind_context,
)
from trading_common.observability.event_types import (
    STRICT_DOMAIN_EVENT_TYPES,
    DomainEventType,
    validate_domain_event_type,
)
from trading_common.observability.logging import (
    CANONICAL_LOG_FIELDS,
    JsonLogFormatter,
    LogContextFilter,
    RedactionFilter,
    TelemetryTextFormatter,
    build_logging_dict_config,
    configure_json_logging,
    configure_logging,
    get_logger,
    log_event,
)
from trading_common.observability.metrics import (
    BOUNDED_PROMETHEUS_LABELS,
    PROMETHEUS_METRIC_NAMES,
    TradingMetrics,
)

__all__ = [
    "BOUNDED_PROMETHEUS_LABELS",
    "CANONICAL_LOG_FIELDS",
    "CONTEXT_FIELDS",
    "DomainEventType",
    "JsonLogFormatter",
    "LogContextFilter",
    "PROMETHEUS_METRIC_NAMES",
    "RedactionFilter",
    "STRICT_DOMAIN_EVENT_TYPES",
    "TelemetryTextFormatter",
    "TradingMetrics",
    "bind_context",
    "build_logging_dict_config",
    "clear_log_context",
    "configure_json_logging",
    "configure_logging",
    "context_from_mapping",
    "get_context",
    "get_logger",
    "get_log_context",
    "log_event",
    "log_context",
    "set_context",
    "set_log_context",
    "unbind_context",
    "validate_domain_event_type",
]

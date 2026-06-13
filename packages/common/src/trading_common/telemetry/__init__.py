"""Telemetry foundation for structured Python logging."""

from trading_common.telemetry.context import (
    CANONICAL_CONTEXT_FIELDS,
    CONTEXT_FIELDS,
    bind_context,
    clear_context,
    context_from_mapping,
    get_context,
    set_context,
    unbind_context,
)
from trading_common.telemetry.logging import (
    CANONICAL_LOG_FIELDS,
    JsonLogFormatter,
    LogContextFilter,
    TelemetryTextFormatter,
    build_logging_dict_config,
    configure_json_logging,
    configure_logging,
    get_logger,
    log_event,
)
from trading_common.telemetry.redaction import REDACTED, RedactionFilter, redact_value

__all__ = [
    "CANONICAL_CONTEXT_FIELDS",
    "CANONICAL_LOG_FIELDS",
    "CONTEXT_FIELDS",
    "REDACTED",
    "JsonLogFormatter",
    "LogContextFilter",
    "RedactionFilter",
    "TelemetryTextFormatter",
    "bind_context",
    "build_logging_dict_config",
    "clear_context",
    "configure_json_logging",
    "configure_logging",
    "context_from_mapping",
    "get_context",
    "get_logger",
    "log_event",
    "redact_value",
    "set_context",
    "unbind_context",
]

"""Backward-compatible imports for telemetry logging."""

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
from trading_common.telemetry.redaction import RedactionFilter

__all__ = [
    "CANONICAL_LOG_FIELDS",
    "JsonLogFormatter",
    "LogContextFilter",
    "RedactionFilter",
    "TelemetryTextFormatter",
    "build_logging_dict_config",
    "configure_json_logging",
    "configure_logging",
    "get_logger",
    "log_event",
]

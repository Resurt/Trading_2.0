"""Backward-compatible observability context wrappers."""

from trading_common.telemetry.context import (
    CONTEXT_FIELDS,
    bind_context,
    clear_context,
    context_from_mapping,
    get_context,
    set_context,
    unbind_context,
)

get_log_context = get_context
set_log_context = set_context
clear_log_context = clear_context
log_context = bind_context

__all__ = [
    "CONTEXT_FIELDS",
    "bind_context",
    "clear_context",
    "clear_log_context",
    "context_from_mapping",
    "get_context",
    "get_log_context",
    "log_context",
    "set_context",
    "set_log_context",
    "unbind_context",
]

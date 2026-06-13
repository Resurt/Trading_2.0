"""Context propagation for structured logs and correlated metrics."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "session_type",
    "session_phase",
    "micro_session_id",
    "instrument_id",
    "timeframe",
    "strategy_id",
    "candidate_id",
    "blocker_id",
    "order_intent_id",
    "request_order_id",
    "exchange_order_id",
)

_log_context: ContextVar[dict[str, str] | None] = ContextVar(
    "trading_log_context",
    default=None,
)


def get_log_context() -> dict[str, str]:
    """Return a copy of the current contextvars-backed log context."""

    return dict(_log_context.get() or {})


def set_log_context(**values: object | None) -> None:
    """Replace or remove selected context keys for the current task."""

    current = get_log_context()
    for key, value in values.items():
        if key not in CONTEXT_FIELDS:
            msg = f"Unsupported observability context key: {key}"
            raise KeyError(msg)
        if value is None:
            current.pop(key, None)
        else:
            current[key] = str(value)
    _log_context.set(current)


def clear_log_context() -> None:
    """Clear all observability context for the current task."""

    _log_context.set({})


@contextmanager
def log_context(**values: object | None) -> Iterator[None]:
    """Temporarily add correlation fields to logs emitted in this context."""

    current = get_log_context()
    merged = _merge_context(current, values)
    token = _log_context.set(merged)
    try:
        yield
    finally:
        _log_context.reset(token)


def context_from_mapping(values: Mapping[str, object | None]) -> dict[str, str]:
    """Normalize a mapping to supported context keys only."""

    return _merge_context({}, values)


def _merge_context(
    current: Mapping[str, str],
    values: Mapping[str, object | None],
) -> dict[str, str]:
    merged = dict(current)
    for key, value in values.items():
        if key not in CONTEXT_FIELDS:
            msg = f"Unsupported observability context key: {key}"
            raise KeyError(msg)
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    return merged

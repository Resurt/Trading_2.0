"""Context propagation for telemetry logs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

CANONICAL_CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "session_type",
    "exchange_phase",
    "micro_session_id",
    "instrument",
    "timeframe",
    "strategy_id",
    "strategy_version",
    "candidate_id",
    "order_intent_id",
    "request_order_id",
    "exchange_order_id",
    "tracking_id",
)

COMPATIBILITY_CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "session_phase",
    "instrument_id",
    "blocker_id",
    "signal_id",
)

CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    *CANONICAL_CONTEXT_FIELDS,
    *COMPATIBILITY_CONTEXT_FIELDS,
)

_ALIASES: Final[dict[str, str]] = {
    "session_phase": "exchange_phase",
    "instrument_id": "instrument",
}
_REVERSE_ALIASES: Final[dict[str, str]] = {
    "exchange_phase": "session_phase",
    "instrument": "instrument_id",
}

_context: ContextVar[dict[str, str] | None] = ContextVar(
    "trading_telemetry_context",
    default=None,
)


def get_context() -> dict[str, str]:
    """Return a copy of the current contextvars-backed telemetry context."""

    return dict(_context.get() or {})


def set_context(**values: object | None) -> None:
    """Update selected context keys for the current task."""

    _context.set(_merge_context(get_context(), values))


def unbind_context(*keys: str) -> None:
    """Remove selected context keys for the current task."""

    current = get_context()
    for key in keys:
        _remove_context_key(current, key)
    _context.set(current)


def clear_context() -> None:
    """Clear all telemetry context for the current task."""

    _context.set({})


@contextmanager
def bind_context(**values: object | None) -> Iterator[None]:
    """Temporarily bind correlation fields to logs emitted in this context."""

    token = _context.set(_merge_context(get_context(), values))
    try:
        yield
    finally:
        _context.reset(token)


def context_from_mapping(values: Mapping[str, object | None]) -> dict[str, str]:
    """Normalize a mapping to supported context keys only."""

    return _merge_context({}, values)


def _merge_context(
    current: Mapping[str, str],
    values: Mapping[str, object | None],
) -> dict[str, str]:
    merged = dict(current)
    for key, value in values.items():
        _validate_context_key(key)
        if value is None:
            _remove_context_key(merged, key)
            continue
        text_value = str(value)
        merged[key] = text_value
        canonical_key = _ALIASES.get(key)
        if canonical_key is not None:
            merged[canonical_key] = text_value
        compatibility_key = _REVERSE_ALIASES.get(key)
        if compatibility_key is not None:
            merged[compatibility_key] = text_value
    return merged


def _remove_context_key(context: dict[str, str], key: str) -> None:
    _validate_context_key(key)
    context.pop(key, None)
    canonical_key = _ALIASES.get(key)
    if canonical_key is not None:
        context.pop(canonical_key, None)
    compatibility_key = _REVERSE_ALIASES.get(key)
    if compatibility_key is not None:
        context.pop(compatibility_key, None)


def _validate_context_key(key: str) -> None:
    if key not in CONTEXT_FIELDS:
        msg = f"Unsupported telemetry context key: {key}"
        raise KeyError(msg)

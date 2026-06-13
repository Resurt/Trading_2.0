"""Redaction helpers for structured logs."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

REDACTED: Final = "[REDACTED]"

_SENSITIVE_KEY_PATTERN: Final = re.compile(
    r"(authorization|password|passwd|pwd|secret|token|credential|credentials|api[_-]?key|"
    r"private[_-]?key|client[_-]?secret|access[_-]?key|refresh[_-]?token)",
    re.IGNORECASE,
)
_AUTH_VALUE_PATTERN: Final = re.compile(
    r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_TOKEN_VALUE_PATTERN: Final = re.compile(r"\bt\.[A-Za-z0-9_-]{20,}\b")
_ASSIGNMENT_PATTERN: Final = re.compile(
    r"(?i)\b(token|secret|password|authorization|api[_-]?key|client[_-]?secret)"
    r"\s*[:=]\s*[^,\s;]+"
)
_STANDARD_RECORD_FIELDS: Final = frozenset(logging.makeLogRecord({}).__dict__)


class RedactionFilter(logging.Filter):
    """Redact credential-like fields before records reach formatters."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_value(record.msg)
        if isinstance(record.args, Mapping):
            record.args = {
                str(item_key): redact_value(item_value, key=str(item_key))
                for item_key, item_value in record.args.items()
            }
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_value(item) for item in record.args)
        for key, value in tuple(record.__dict__.items()):
            if key in _STANDARD_RECORD_FIELDS:
                continue
            record.__dict__[key] = redact_value(value, key=key)
        return True


def redact_value(value: Any, *, key: str | None = None) -> object:
    """Return a JSON-safe-ish value with secrets removed."""

    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, bytes):
        return _redact_string(value.decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, set):
        return [redact_value(item) for item in sorted(value, key=str)]
    if _is_sequence_but_not_text(value):
        return [redact_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return bool(_SENSITIVE_KEY_PATTERN.search(normalized))


def _redact_string(value: str) -> str:
    redacted = _AUTH_VALUE_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED}", value)
    redacted = _TOKEN_VALUE_PATTERN.sub(REDACTED, redacted)
    return _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", redacted)


def _is_sequence_but_not_text(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)

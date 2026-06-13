"""Structured JSON logging on top of the standard Python logging package."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import TracebackType
from typing import Any

from trading_common.enums import ServiceName
from trading_common.observability.context import CONTEXT_FIELDS, get_log_context

CANONICAL_LOG_FIELDS: tuple[str, ...] = (
    "ts_utc",
    "level",
    "logger",
    "service",
    "event_type",
    "message",
    *CONTEXT_FIELDS,
    "tracking_id",
    "rate_limit_limit",
    "rate_limit_remaining",
    "rate_limit_reset",
    "latency_ms",
    "error_code",
    "error_message",
    "cancel_reason_code",
    "reject_reason_code",
)

_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


class LogContextFilter(logging.Filter):
    """Inject contextvars correlation fields into every log record."""

    def __init__(self, *, service: ServiceName | str) -> None:
        super().__init__()
        self._service = str(service)

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self._service
        for key, value in get_log_context().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonLogFormatter(logging.Formatter):
    """Render log records as one-line canonical JSON."""

    def __init__(self, *, service: ServiceName | str) -> None:
        super().__init__()
        self._service = str(service)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts_utc": _record_ts_utc(record),
            "level": record.levelname,
            "logger": record.name,
            "service": str(getattr(record, "service", self._service)),
            "event_type": getattr(record, "event_type", "technical_log"),
            "message": record.getMessage(),
        }

        for key in CANONICAL_LOG_FIELDS:
            if key in payload:
                continue
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = _json_safe(value)

        if record.exc_info is not None:
            exc_type, exc, tb = record.exc_info
            payload["exception"] = _format_exception(exc_type, exc, tb)

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key in payload or key == "message":
                continue
            payload[key] = _json_safe(value)

        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def configure_json_logging(
    *,
    service: ServiceName | str,
    level: int = logging.INFO,
    logger_name: str | None = None,
) -> logging.Logger:
    """Configure a logger with stdout JSON logs for Fluent Bit -> Loki."""

    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.addFilter(LogContextFilter(service=service))
    handler.setFormatter(JsonLogFormatter(service=service))
    logger.addHandler(handler)
    return logger


def _record_ts_utc(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat()


def _format_exception(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> dict[str, object]:
    return {
        "type": exc_type.__name__ if exc_type is not None else None,
        "message": str(exc) if exc is not None else None,
        "has_traceback": tb is not None,
    }


def _json_safe(value: Any) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)

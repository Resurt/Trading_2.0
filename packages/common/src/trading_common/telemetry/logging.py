"""Structured logging foundation built on the Python standard library."""

from __future__ import annotations

import json
import logging
import logging.config
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import TracebackType
from typing import Any, Final, Literal

from trading_common.enums import ServiceName
from trading_common.telemetry.context import CONTEXT_FIELDS, get_context
from trading_common.telemetry.redaction import redact_value

LogFormat = Literal["json", "text"]

CANONICAL_LOG_FIELDS: Final[tuple[str, ...]] = (
    "ts_utc",
    "exchange_ts",
    "level",
    "service",
    "component",
    "event_type",
    "event_version",
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
    "latency_ms",
    "error_code",
    "error_message",
    "payload",
)

COMPATIBILITY_OUTPUT_FIELDS: Final[tuple[str, ...]] = (
    "logger",
    "message",
    "run_id",
    "session_phase",
    "instrument_id",
    "blocker_id",
    "signal_id",
    "event_name",
    "stage_name",
    "cancel_reason_code",
    "reject_reason_code",
)

_STANDARD_RECORD_FIELDS: Final = frozenset(logging.makeLogRecord({}).__dict__)
_RESERVED_EXTRA_FIELDS: Final = frozenset(
    (
        *CANONICAL_LOG_FIELDS,
        *COMPATIBILITY_OUTPUT_FIELDS,
        *CONTEXT_FIELDS,
        "exc_info",
        "exc_text",
        "stack_info",
    )
)


class LogContextFilter(logging.Filter):
    """Inject service identity and contextvars correlation fields into records."""

    def __init__(self, *, service: ServiceName | str, component: str | None = None) -> None:
        super().__init__()
        self._service = str(service)
        self._component = component

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self._service
        if self._component is not None and not hasattr(record, "component"):
            record.component = self._component
        for key, value in get_context().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonLogFormatter(logging.Formatter):
    """Render log records as one-line canonical JSON."""

    def __init__(
        self,
        *,
        service: ServiceName | str = "unknown",
        component: str | None = None,
    ) -> None:
        super().__init__()
        self._service = str(service)
        self._component = component

    def format(self, record: logging.LogRecord) -> str:
        context = get_context()
        log_payload = self._base_payload(record, context)
        event_payload = self._event_payload(record)

        for key in COMPATIBILITY_OUTPUT_FIELDS:
            if key == "logger":
                value: object | None = record.name
            elif key == "message":
                value = record.getMessage()
            else:
                value = self._record_or_context_value(record, context, key)
            if value is not None:
                log_payload[key] = _json_safe(redact_value(value, key=key))

        for key, value in _extra_record_items(record):
            if key in _RESERVED_EXTRA_FIELDS:
                continue
            redacted = _json_safe(redact_value(value, key=key))
            event_payload[key] = redacted
            log_payload[key] = redacted

        if record.exc_info is not None:
            event_payload["exception"] = _format_exception(*record.exc_info)

        log_payload["payload"] = _json_safe(redact_value(event_payload, key="payload"))
        return json.dumps(
            log_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _base_payload(
        self,
        record: logging.LogRecord,
        context: Mapping[str, str],
    ) -> dict[str, object]:
        exchange_phase = self._record_or_context_value(record, context, "exchange_phase")
        if exchange_phase is None:
            exchange_phase = self._record_or_context_value(record, context, "session_phase")
        instrument = self._record_or_context_value(record, context, "instrument")
        if instrument is None:
            instrument = self._record_or_context_value(record, context, "instrument_id")

        return {
            "ts_utc": _record_ts_utc(record),
            "exchange_ts": _json_safe(
                self._record_or_context_value(record, context, "exchange_ts")
            ),
            "level": record.levelname,
            "service": str(
                self._record_or_context_value(record, context, "service") or self._service
            ),
            "component": str(
                self._record_or_context_value(record, context, "component")
                or self._component
                or record.name
            ),
            "event_type": str(
                self._record_or_context_value(record, context, "event_type") or "technical_log"
            ),
            "event_version": str(
                self._record_or_context_value(record, context, "event_version") or "1"
            ),
            "session_type": _json_safe(
                self._record_or_context_value(record, context, "session_type")
            ),
            "exchange_phase": _json_safe(exchange_phase),
            "micro_session_id": _json_safe(
                self._record_or_context_value(record, context, "micro_session_id")
            ),
            "instrument": _json_safe(instrument),
            "timeframe": _json_safe(self._record_or_context_value(record, context, "timeframe")),
            "strategy_id": _json_safe(
                self._record_or_context_value(record, context, "strategy_id")
            ),
            "strategy_version": _json_safe(
                self._record_or_context_value(record, context, "strategy_version")
            ),
            "candidate_id": _json_safe(
                self._record_or_context_value(record, context, "candidate_id")
            ),
            "order_intent_id": _json_safe(
                self._record_or_context_value(record, context, "order_intent_id")
            ),
            "request_order_id": _json_safe(
                self._record_or_context_value(record, context, "request_order_id")
            ),
            "exchange_order_id": _json_safe(
                self._record_or_context_value(record, context, "exchange_order_id")
            ),
            "tracking_id": _json_safe(
                self._record_or_context_value(record, context, "tracking_id")
            ),
            "latency_ms": _json_safe(
                self._record_or_context_value(record, context, "latency_ms")
            ),
            "error_code": _json_safe(
                self._record_or_context_value(record, context, "error_code")
            ),
            "error_message": _json_safe(
                self._record_or_context_value(record, context, "error_message")
                or _exception_message(record)
            ),
        }

    def _event_payload(self, record: logging.LogRecord) -> dict[str, object]:
        payload = getattr(record, "payload", None)
        if payload is None:
            return {}
        redacted_payload = redact_value(payload, key="payload")
        if isinstance(redacted_payload, Mapping):
            return {str(key): _json_safe(value) for key, value in redacted_payload.items()}
        return {"value": _json_safe(redacted_payload)}

    def _record_or_context_value(
        self,
        record: logging.LogRecord,
        context: Mapping[str, str],
        key: str,
    ) -> object | None:
        value: object | None = getattr(record, key, None)
        if value is not None:
            return value
        return context.get(key)


class TelemetryTextFormatter(logging.Formatter):
    """Friendly dev formatter; production containers should use JSON."""

    def format(self, record: logging.LogRecord) -> str:
        context = get_context()
        event_type = getattr(record, "event_type", "technical_log")
        component = getattr(record, "component", record.name)
        micro_session_id = getattr(record, "micro_session_id", context.get("micro_session_id"))
        prefix = f"{record.levelname} {component} {event_type}"
        if micro_session_id:
            prefix = f"{prefix} micro_session_id={micro_session_id}"
        return f"{prefix} - {redact_value(record.getMessage())}"


def build_logging_dict_config(
    *,
    service: ServiceName | str,
    level: int | str = logging.INFO,
    log_format: LogFormat = "json",
    logger_name: str | None = None,
    component: str | None = None,
) -> dict[str, object]:
    """Build a dictConfig-compatible logging configuration."""

    level_name = _level_name(level)
    formatter_name = "json" if log_format == "json" else "text"
    config: dict[str, object] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "telemetry_context": {
                "()": "trading_common.telemetry.logging.LogContextFilter",
                "service": str(service),
                "component": component,
            },
            "redaction": {
                "()": "trading_common.telemetry.redaction.RedactionFilter",
            },
        },
        "formatters": {
            "json": {
                "()": "trading_common.telemetry.logging.JsonLogFormatter",
                "service": str(service),
                "component": component,
            },
            "text": {
                "()": "trading_common.telemetry.logging.TelemetryTextFormatter",
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "level": level_name,
                "stream": "ext://sys.stdout",
                "filters": ["redaction", "telemetry_context"],
                "formatter": formatter_name,
            },
        },
    }
    if logger_name is None:
        config["root"] = {"level": level_name, "handlers": ["stdout"]}
    else:
        config["loggers"] = {
            logger_name: {
                "level": level_name,
                "handlers": ["stdout"],
                "propagate": False,
            }
        }
    return config


def configure_logging(
    *,
    service: ServiceName | str,
    level: int | str = logging.INFO,
    logger_name: str | None = None,
    log_format: LogFormat | None = None,
    component: str | None = None,
) -> logging.Logger:
    """Configure stdout logging via dictConfig and return the configured logger."""

    selected_format = log_format or _log_format_from_env()
    logging.config.dictConfig(
        build_logging_dict_config(
            service=service,
            level=level,
            log_format=selected_format,
            logger_name=logger_name,
            component=component,
        )
    )
    return logging.getLogger(logger_name)


def configure_json_logging(
    *,
    service: ServiceName | str,
    level: int | str = logging.INFO,
    logger_name: str | None = None,
) -> logging.Logger:
    """Configure production JSON logs to stdout for Fluent Bit -> Loki."""

    return configure_logging(
        service=service,
        level=level,
        logger_name=logger_name,
        log_format="json",
    )


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a standard-library logger."""

    return logging.getLogger(name)


def log_event(
    *,
    event_type: str | Enum,
    logger: logging.Logger | str | None = None,
    level: int | str = logging.INFO,
    event_version: str = "1",
    event_name: str | None = None,
    stage_name: str | None = None,
    component: str | None = None,
    exchange_ts: datetime | str | None = None,
    latency_ms: int | float | Decimal | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    **payload: object,
) -> None:
    """Log a structured event without using domain meaning as log severity."""

    target_logger = _resolve_logger(logger)
    event_type_value = event_type.value if isinstance(event_type, Enum) else str(event_type)
    event_payload = dict(payload)
    if stage_name is not None:
        event_payload["stage_name"] = stage_name

    extra: dict[str, object] = {
        "event_type": event_type_value,
        "event_version": event_version,
        "payload": event_payload,
    }
    if event_name is not None:
        extra["event_name"] = event_name
    if stage_name is not None:
        extra["stage_name"] = stage_name
    if component is not None:
        extra["component"] = component
    if exchange_ts is not None:
        extra["exchange_ts"] = exchange_ts
    if latency_ms is not None:
        extra["latency_ms"] = latency_ms
    if error_code is not None:
        extra["error_code"] = error_code
    if error_message is not None:
        extra["error_message"] = error_message
    for key, value in event_payload.items():
        if key in CANONICAL_LOG_FIELDS and key not in {"payload", "ts_utc", "level"}:
            extra[key] = value

    target_logger.log(_level_no(level), event_name or event_type_value, extra=extra)


def _record_ts_utc(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat().replace("+00:00", "Z")


def _format_exception(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> dict[str, object]:
    return {
        "type": exc_type.__name__ if exc_type is not None else None,
        "message": _json_safe(redact_value(str(exc) if exc is not None else None)),
        "has_traceback": tb is not None,
    }


def _exception_message(record: logging.LogRecord) -> str | None:
    if record.exc_info is None:
        return None
    _exc_type, exc, _tb = record.exc_info
    if exc is None:
        return None
    return str(redact_value(str(exc)))


def _json_safe(value: Any) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)


def _extra_record_items(record: logging.LogRecord) -> list[tuple[str, object]]:
    return [
        (key, value)
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_FIELDS and key != "message"
    ]


def _resolve_logger(logger: logging.Logger | str | None) -> logging.Logger:
    if isinstance(logger, logging.Logger):
        return logger
    return get_logger(logger)


def _level_no(level: int | str) -> int:
    if isinstance(level, int):
        return level
    value = logging.getLevelName(level.upper())
    if isinstance(value, int):
        return value
    msg = f"Unsupported logging level: {level}"
    raise ValueError(msg)


def _level_name(level: int | str) -> str:
    if isinstance(level, str):
        _level_no(level)
        return level.upper()
    return logging.getLevelName(level)


def _log_format_from_env() -> LogFormat:
    raw_value = os.getenv("TRADING_LOG_FORMAT", "json").lower()
    if raw_value == "text":
        return "text"
    return "json"

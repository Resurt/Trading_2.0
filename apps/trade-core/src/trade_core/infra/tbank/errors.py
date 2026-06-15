"""T-Bank error mapping to SDK-neutral broker exceptions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from trade_core.infra.tbank.headers import TBankResponseHeaders


class BrokerErrorKind(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    UNAUTHENTICATED = "unauthenticated"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    UNAVAILABLE = "unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERNAL = "internal"
    FAILED_PRECONDITION = "failed_precondition"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BrokerErrorInfo:
    kind: BrokerErrorKind
    retryable: bool
    reason_code: str


ERROR_CODE_KIND_PREFIXES: tuple[tuple[range, BrokerErrorKind, bool, str], ...] = (
    (range(30000, 39999), BrokerErrorKind.INVALID_ARGUMENT, False, "invalid_argument"),
    (range(40003, 40004), BrokerErrorKind.UNAUTHENTICATED, False, "unauthenticated"),
    (range(40000, 49999), BrokerErrorKind.PERMISSION_DENIED, False, "permission_denied"),
    (range(50000, 59999), BrokerErrorKind.NOT_FOUND, False, "not_found"),
    (range(70000, 79999), BrokerErrorKind.INTERNAL, True, "internal"),
    (range(80000, 89999), BrokerErrorKind.RESOURCE_EXHAUSTED, True, "rate_limit"),
    (range(90000, 99999), BrokerErrorKind.FAILED_PRECONDITION, False, "failed_precondition"),
)
STATUS_KIND_MAP: dict[str, BrokerErrorInfo] = {
    "INVALID_ARGUMENT": BrokerErrorInfo(
        BrokerErrorKind.INVALID_ARGUMENT,
        False,
        "invalid_argument",
    ),
    "UNAUTHENTICATED": BrokerErrorInfo(
        BrokerErrorKind.UNAUTHENTICATED,
        False,
        "unauthenticated",
    ),
    "PERMISSION_DENIED": BrokerErrorInfo(
        BrokerErrorKind.PERMISSION_DENIED,
        False,
        "permission_denied",
    ),
    "NOT_FOUND": BrokerErrorInfo(BrokerErrorKind.NOT_FOUND, False, "not_found"),
    "RESOURCE_EXHAUSTED": BrokerErrorInfo(
        BrokerErrorKind.RESOURCE_EXHAUSTED,
        True,
        "rate_limit",
    ),
    "UNAVAILABLE": BrokerErrorInfo(BrokerErrorKind.UNAVAILABLE, True, "unavailable"),
    "DEADLINE_EXCEEDED": BrokerErrorInfo(
        BrokerErrorKind.DEADLINE_EXCEEDED,
        True,
        "deadline_exceeded",
    ),
    "INTERNAL": BrokerErrorInfo(BrokerErrorKind.INTERNAL, True, "internal"),
    "FAILED_PRECONDITION": BrokerErrorInfo(
        BrokerErrorKind.FAILED_PRECONDITION,
        False,
        "failed_precondition",
    ),
}


class BrokerGatewayError(RuntimeError):
    """SDK-neutral broker adapter error."""

    def __init__(
        self,
        message: str,
        *,
        method_name: str,
        kind: BrokerErrorKind,
        retryable: bool,
        reason_code: str,
        headers: TBankResponseHeaders | None = None,
        original_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.method_name = method_name
        self.kind = kind
        self.retryable = retryable
        self.reason_code = reason_code
        self.headers = headers or TBankResponseHeaders()
        self.original_error = original_error


def map_error_info(status_code: str | None, error_code: int | None) -> BrokerErrorInfo:
    if error_code is not None:
        for code_range, kind, retryable, reason_code in ERROR_CODE_KIND_PREFIXES:
            if error_code in code_range:
                return BrokerErrorInfo(kind=kind, retryable=retryable, reason_code=reason_code)

    normalized_status = (status_code or "").upper()
    if normalized_status in STATUS_KIND_MAP:
        return STATUS_KIND_MAP[normalized_status]

    return BrokerErrorInfo(
        kind=BrokerErrorKind.UNKNOWN,
        retryable=False,
        reason_code="unknown_broker_error",
    )


def map_exception(
    exc: Exception,
    *,
    method_name: str,
    headers: TBankResponseHeaders | None = None,
) -> BrokerGatewayError:
    """Map SDK/gRPC-like exceptions without depending on a specific SDK class."""

    raw_status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    status_value = raw_status() if callable(raw_status) else raw_status
    status_code = None
    if status_value is not None:
        status_code = status_value.name if hasattr(status_value, "name") else str(status_value)

    raw_error_code = getattr(exc, "error_code", None)
    if raw_error_code is None:
        raw_error_code = _extract_numeric_error_code(getattr(exc, "details", None))
    error_code = int(raw_error_code) if raw_error_code is not None else None
    info = map_error_info(status_code, error_code)
    message = str(exc) or info.reason_code
    return BrokerGatewayError(
        message,
        method_name=method_name,
        kind=info.kind,
        retryable=info.retryable,
        reason_code=info.reason_code,
        headers=headers,
        original_error=exc,
    )


def _extract_numeric_error_code(details: object) -> int | None:
    if details is None:
        return None
    match = re.search(r"\b([3-9]\d{4})\b", str(details))
    if match is None:
        return None
    return int(match.group(1))

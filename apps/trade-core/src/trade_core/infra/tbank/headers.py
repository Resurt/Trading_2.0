"""Capture and normalize T-Invest service headers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

HEADER_TRACKING_ID = "x-tracking-id"
HEADER_APP_NAME = "x-app-name"
HEADER_RATELIMIT_LIMIT = "x-ratelimit-limit"
HEADER_RATELIMIT_REMAINING = "x-ratelimit-remaining"
HEADER_RATELIMIT_RESET = "x-ratelimit-reset"
HEADER_MESSAGE = "message"
CAPTURED_HEADER_NAMES = (
    HEADER_TRACKING_ID,
    HEADER_APP_NAME,
    HEADER_RATELIMIT_LIMIT,
    HEADER_RATELIMIT_REMAINING,
    HEADER_RATELIMIT_RESET,
    HEADER_MESSAGE,
)


@dataclass(frozen=True, slots=True)
class TBankResponseHeaders:
    """Headers used for support diagnostics, rate-limit handling, and logging."""

    tracking_id: str | None = None
    app_name: str | None = None
    ratelimit_limit: str | None = None
    ratelimit_remaining: str | None = None
    ratelimit_reset: str | None = None
    message: str | None = None

    def as_log_context(self) -> dict[str, str | None]:
        return {
            "x_tracking_id": self.tracking_id,
            "x_app_name": self.app_name,
            "x_ratelimit_limit": self.ratelimit_limit,
            "x_ratelimit_remaining": self.ratelimit_remaining,
            "x_ratelimit_reset": self.ratelimit_reset,
            "message": self.message,
        }


def _first_header_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list | tuple):
        if not value:
            return None
        return _first_header_value(value[0])
    return str(value)


def capture_response_headers(headers: Mapping[str, object] | None) -> TBankResponseHeaders:
    """Normalize response metadata to known T-Invest service headers."""

    if not headers:
        return TBankResponseHeaders()
    lowered = {key.lower(): value for key, value in headers.items()}
    return TBankResponseHeaders(
        tracking_id=_first_header_value(lowered.get(HEADER_TRACKING_ID)),
        app_name=_first_header_value(lowered.get(HEADER_APP_NAME)),
        ratelimit_limit=_first_header_value(lowered.get(HEADER_RATELIMIT_LIMIT)),
        ratelimit_remaining=_first_header_value(lowered.get(HEADER_RATELIMIT_REMAINING)),
        ratelimit_reset=_first_header_value(lowered.get(HEADER_RATELIMIT_RESET)),
        message=_first_header_value(lowered.get(HEADER_MESSAGE)),
    )


def auth_metadata(token: str, app_name: str) -> tuple[tuple[str, str], ...]:
    """Build outbound gRPC metadata without logging token values."""

    return (
        ("authorization", f"Bearer {token}"),
        (HEADER_APP_NAME, app_name),
    )

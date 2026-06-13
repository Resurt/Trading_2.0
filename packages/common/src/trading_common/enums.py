"""Canonical enums shared across backend services."""

from enum import StrEnum


class ServiceName(StrEnum):
    """Known backend service names."""

    TRADE_CORE = "trade-core"
    API = "api"
    REPORT_WORKER = "report-worker"


class RuntimeMode(StrEnum):
    """Supported runtime modes."""

    HISTORICAL_REPLAY = "historical_replay"
    SANDBOX = "sandbox"
    SHADOW = "shadow"
    PRODUCTION = "production"


class SessionType(StrEnum):
    """Canonical market session types."""

    WEEKDAY_MORNING = "weekday_morning"
    WEEKDAY_MAIN = "weekday_main"
    WEEKDAY_EVENING = "weekday_evening"
    WEEKEND = "weekend"


class SessionPhase(StrEnum):
    """Canonical market session phases."""

    OPENING_AUCTION = "opening_auction"
    CONTINUOUS_TRADING = "continuous_trading"
    CLOSING_AUCTION = "closing_auction"
    BREAK = "break"
    DEALER_MODE = "dealer_mode"
    CLOSED = "closed"

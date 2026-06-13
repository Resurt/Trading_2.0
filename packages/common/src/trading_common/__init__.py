"""Shared typed contracts for the trading robot monorepo."""

from trading_common.enums import RuntimeMode, ServiceName, SessionPhase, SessionType
from trading_common.launch_modes import (
    PRODUCTION_CONFIRM_ENV,
    PRODUCTION_CONFIRM_VALUE,
    TRADING_RUNTIME_MODE_ENV,
    LaunchModePolicy,
    parse_runtime_mode,
)
from trading_common.models import AppIdentity, HealthStatus, ServiceHealth, TradingContext
from trading_common.observability import (
    DomainEventType,
    TradingMetrics,
    bind_context,
    clear_log_context,
    configure_json_logging,
    configure_logging,
    get_logger,
    log_event,
)

__all__ = [
    "AppIdentity",
    "DomainEventType",
    "HealthStatus",
    "LaunchModePolicy",
    "PRODUCTION_CONFIRM_ENV",
    "PRODUCTION_CONFIRM_VALUE",
    "RuntimeMode",
    "ServiceHealth",
    "ServiceName",
    "SessionPhase",
    "SessionType",
    "TRADING_RUNTIME_MODE_ENV",
    "TradingMetrics",
    "TradingContext",
    "bind_context",
    "clear_log_context",
    "configure_json_logging",
    "configure_logging",
    "get_logger",
    "log_event",
    "parse_runtime_mode",
]

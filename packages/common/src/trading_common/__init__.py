"""Shared typed contracts for the trading robot monorepo."""

from trading_common.enums import RuntimeMode, ServiceName, SessionPhase, SessionType
from trading_common.models import AppIdentity, HealthStatus, ServiceHealth, TradingContext
from trading_common.observability import DomainEventType, TradingMetrics, configure_json_logging

__all__ = [
    "AppIdentity",
    "DomainEventType",
    "HealthStatus",
    "RuntimeMode",
    "ServiceHealth",
    "ServiceName",
    "SessionPhase",
    "SessionType",
    "TradingMetrics",
    "TradingContext",
    "configure_json_logging",
]

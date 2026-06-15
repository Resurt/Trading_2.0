"""Long-lived trade-core service skeleton."""

from trade_core.app import create_identity, health
from trade_core.broker_gateway import BrokerGateway
from trade_core.market_data import MarketDataPipeline, MarketEventBus
from trade_core.runtime import TradeCoreRuntime
from trade_core.session import HourlyMicroSessionManager, SessionManager
from trade_core.strategy import (
    DefaultExecutionEngine,
    DefaultReconciliationService,
    DefaultRiskEngine,
    StrategyEngine,
)

__all__ = [
    "BrokerGateway",
    "DefaultExecutionEngine",
    "DefaultReconciliationService",
    "DefaultRiskEngine",
    "HourlyMicroSessionManager",
    "MarketDataPipeline",
    "MarketEventBus",
    "SessionManager",
    "StrategyEngine",
    "TradeCoreRuntime",
    "create_identity",
    "health",
]

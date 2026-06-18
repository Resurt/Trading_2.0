"""Corporate action and special-day services for historical calibration."""

from trade_core.corporate_actions.dividend_sync import (
    DividendSyncConfig,
    DividendSyncInstrumentResult,
    DividendSyncResult,
    DividendSyncService,
    dividend_sync_window,
)
from trade_core.corporate_actions.service import (
    CorporateActionEvent,
    CorporateActionImportConfig,
    CorporateActionService,
    MarketSpecialDayClassifier,
    MarketSpecialDayResult,
    SpecialDayFlags,
)

__all__ = [
    "CorporateActionEvent",
    "CorporateActionImportConfig",
    "CorporateActionService",
    "DividendSyncConfig",
    "DividendSyncInstrumentResult",
    "DividendSyncResult",
    "DividendSyncService",
    "MarketSpecialDayClassifier",
    "MarketSpecialDayResult",
    "SpecialDayFlags",
    "dividend_sync_window",
]

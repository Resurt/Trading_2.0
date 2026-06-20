"""Analytics public API for report-worker."""

from report_worker.analytics.calculations import (
    analyze_counterfactual,
    classify_day_regimes,
    classify_day_trend,
    state_time_distribution,
)
from report_worker.analytics.calibration_observatory import (
    CalibrationDiagnosticService,
    IntradayAnalyticsService,
    MarketRegimeDiagnosticService,
    RollingPerformanceCubeService,
    StrategyConfigProposalService,
)
from report_worker.analytics.models import (
    AnalyticsAssumptions,
    AnalyticsFilters,
    CounterfactualAnalysis,
    CounterfactualSource,
    FunnelMetrics,
    PricePathPoint,
    TrendClassification,
    WindowOutcome,
)
from report_worker.analytics.service import ReportAnalyticsService

__all__ = [
    "AnalyticsAssumptions",
    "AnalyticsFilters",
    "CalibrationDiagnosticService",
    "CounterfactualAnalysis",
    "CounterfactualSource",
    "FunnelMetrics",
    "IntradayAnalyticsService",
    "MarketRegimeDiagnosticService",
    "PricePathPoint",
    "ReportAnalyticsService",
    "RollingPerformanceCubeService",
    "StrategyConfigProposalService",
    "TrendClassification",
    "WindowOutcome",
    "analyze_counterfactual",
    "classify_day_regimes",
    "classify_day_trend",
    "state_time_distribution",
]

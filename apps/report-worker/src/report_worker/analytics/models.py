"""Typed analytics models for report-worker calculations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

JsonPayload = dict[str, object]


@dataclass(frozen=True, slots=True)
class AnalyticsAssumptions:
    """Fee/slippage/TP/SL assumptions used by deterministic analytics."""

    commission_bps_per_side: Decimal = Decimal("5.0")
    fee_bps: Decimal = Decimal("10.0")
    slippage_bps: Decimal = Decimal("2.0")
    take_profit_bps: Decimal = Decimal("30.0")
    stop_loss_bps: Decimal = Decimal("20.0")

    @property
    def total_cost_bps(self) -> Decimal:
        return self.fee_bps + self.slippage_bps

    def as_payload(self) -> JsonPayload:
        return {
            "commission_bps_per_side": str(self.commission_bps_per_side),
            "fee_bps": str(self.fee_bps),
            "slippage_bps": str(self.slippage_bps),
            "total_cost_bps": str(self.total_cost_bps),
            "take_profit_bps": str(self.take_profit_bps),
            "stop_loss_bps": str(self.stop_loss_bps),
            "commission_rule": "stock_default_0_05_percent_per_side",
        }


@dataclass(frozen=True, slots=True)
class AnalyticsFilters:
    """Common report filters shared by Celery tasks and CLI scripts."""

    trading_date: date
    strategy_id: str
    instrument_id: str | None = None
    timeframe: str | None = None
    session_type: str | None = None
    strategy_version: int | None = None

    def as_payload(self) -> JsonPayload:
        return {
            "trading_date": self.trading_date.isoformat(),
            "strategy_id": self.strategy_id,
            "instrument": self.instrument_id,
            "timeframe": self.timeframe,
            "session_type": self.session_type,
            "strategy_version": self.strategy_version,
        }


@dataclass(frozen=True, slots=True)
class PricePathPoint:
    ts_utc: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal


@dataclass(frozen=True, slots=True)
class CounterfactualSource:
    candidate_id: UUID | None
    order_intent_id: UUID | None
    source_event_type: str
    instrument_id: str
    strategy_id: str
    side: str
    event_ts: datetime
    entry_price: Decimal
    lot_qty: int
    blocker_code: str | None
    cancel_reason_code: str | None
    timeframe: str | None = None
    strategy_version: int | None = None


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    window_minutes: int
    mfe_bps: Decimal | None
    mae_bps: Decimal | None
    close_return_bps: Decimal | None
    gross_pnl_bps: Decimal | None
    gross_pnl_rub: Decimal | None
    net_pnl_bps: Decimal | None
    net_pnl_rub: Decimal | None
    theoretical_pnl_bps: Decimal | None
    theoretical_pnl_rub: Decimal | None
    would_profit: bool | None
    tp_hit: bool | None
    sl_hit: bool | None

    def as_payload(self) -> JsonPayload:
        return {
            "window_minutes": self.window_minutes,
            "mfe_bps": _optional_decimal(self.mfe_bps),
            "mae_bps": _optional_decimal(self.mae_bps),
            "close_return_bps": _optional_decimal(self.close_return_bps),
            "gross_pnl_bps": _optional_decimal(self.gross_pnl_bps),
            "gross_pnl_rub": _optional_decimal(self.gross_pnl_rub),
            "net_pnl_bps": _optional_decimal(self.net_pnl_bps),
            "net_pnl_rub": _optional_decimal(self.net_pnl_rub),
            "theoretical_pnl_bps": _optional_decimal(self.theoretical_pnl_bps),
            "theoretical_pnl_rub": _optional_decimal(self.theoretical_pnl_rub),
            "would_profit": self.would_profit,
            "tp_hit": self.tp_hit,
            "sl_hit": self.sl_hit,
        }


@dataclass(frozen=True, slots=True)
class CounterfactualAnalysis:
    source: CounterfactualSource
    windows: dict[int, WindowOutcome]
    assumptions: AnalyticsAssumptions
    scenarios: dict[str, dict[int, WindowOutcome]] = field(default_factory=dict)

    def as_payload(self) -> JsonPayload:
        return {
            "source_event_type": self.source.source_event_type,
            "side": self.source.side,
            "entry_price": str(self.source.entry_price),
            "lot_qty": self.source.lot_qty,
            "timeframe": self.source.timeframe,
            "assumptions": self.assumptions.as_payload(),
            "windows": {
                str(window): outcome.as_payload()
                for window, outcome in sorted(self.windows.items())
            },
            "scenarios": {
                scenario: {
                    str(window): outcome.as_payload()
                    for window, outcome in sorted(outcomes.items())
                }
                for scenario, outcomes in sorted(self.scenarios.items())
            },
            "algorithm": "counterfactual_mfe_mae_realistic_scenarios_v2",
            "explainability": {
                "horizons_minutes": [5, 10, 15],
                "gross_pnl_bps": "directional close return before costs",
                "net_pnl_bps": "gross_pnl_bps - fee_bps - slippage_bps",
                "default_stock_commission": "0.05% per side, stored as 10 bps round trip",
            },
        }


@dataclass(frozen=True, slots=True)
class FunnelMetrics:
    created: int = 0
    passed_gates: int = 0
    blockers: int = 0
    order_intent: int = 0
    posted: int = 0
    filled: int = 0
    exited: int = 0
    profitable: int = 0

    def as_payload(self) -> JsonPayload:
        return {
            "created": self.created,
            "candidates": self.created,
            "passed_gates": self.passed_gates,
            "blockers": self.blockers,
            "blocked": self.blockers,
            "approved": self.passed_gates,
            "order_intent": self.order_intent,
            "posted": self.posted,
            "filled": self.filled,
            "exited": self.exited,
            "profitable": self.profitable,
        }


@dataclass(frozen=True, slots=True)
class TrendClassification:
    market_regime: str
    average_return_bps: Decimal
    instrument_returns_bps: dict[str, Decimal] = field(default_factory=dict)
    scope_returns_bps: dict[str, Decimal] = field(default_factory=dict)
    scope_range_bps: dict[str, Decimal] = field(default_factory=dict)
    regime_by_scope: dict[str, str] = field(default_factory=dict)

    def as_payload(self) -> JsonPayload:
        return {
            "market_regime": self.market_regime,
            "average_return_bps": str(self.average_return_bps),
            "instrument_returns_bps": {
                instrument_id: str(value)
                for instrument_id, value in sorted(self.instrument_returns_bps.items())
            },
            "scope_returns_bps": {
                scope: str(value) for scope, value in sorted(self.scope_returns_bps.items())
            },
            "scope_range_bps": {
                scope: str(value) for scope, value in sorted(self.scope_range_bps.items())
            },
            "regime_by_instrument_timeframe": dict(sorted(self.regime_by_scope.items())),
            "allowed_regimes": ["trend_up", "trend_down", "flat", "choppy"],
            "algorithm": "daily_scope_return_and_range_v2",
        }


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None

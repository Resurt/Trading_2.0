"""Typed analytics models for report-worker calculations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

JsonPayload = dict[str, object]


@dataclass(frozen=True, slots=True)
class AnalyticsAssumptions:
    """Fee/slippage/TP/SL assumptions used by deterministic analytics."""

    fee_bps: Decimal = Decimal("2.0")
    slippage_bps: Decimal = Decimal("2.0")
    take_profit_bps: Decimal = Decimal("30.0")
    stop_loss_bps: Decimal = Decimal("20.0")

    @property
    def total_cost_bps(self) -> Decimal:
        return self.fee_bps + self.slippage_bps

    def as_payload(self) -> JsonPayload:
        return {
            "fee_bps": str(self.fee_bps),
            "slippage_bps": str(self.slippage_bps),
            "take_profit_bps": str(self.take_profit_bps),
            "stop_loss_bps": str(self.stop_loss_bps),
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


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    window_minutes: int
    mfe_bps: Decimal | None
    mae_bps: Decimal | None
    close_return_bps: Decimal | None
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

    def as_payload(self) -> JsonPayload:
        return {
            "source_event_type": self.source.source_event_type,
            "side": self.source.side,
            "entry_price": str(self.source.entry_price),
            "lot_qty": self.source.lot_qty,
            "assumptions": self.assumptions.as_payload(),
            "windows": {
                str(window): outcome.as_payload()
                for window, outcome in sorted(self.windows.items())
            },
            "algorithm": "mfe_mae_directional_close_after_fees_slippage_v1",
        }


@dataclass(frozen=True, slots=True)
class FunnelMetrics:
    candidates: int = 0
    blockers: int = 0
    approved: int = 0
    posted: int = 0
    filled: int = 0
    profitable: int = 0

    def as_payload(self) -> JsonPayload:
        return {
            "candidates": self.candidates,
            "blockers": self.blockers,
            "approved": self.approved,
            "posted": self.posted,
            "filled": self.filled,
            "profitable": self.profitable,
        }


@dataclass(frozen=True, slots=True)
class TrendClassification:
    market_regime: str
    average_return_bps: Decimal
    instrument_returns_bps: dict[str, Decimal] = field(default_factory=dict)

    def as_payload(self) -> JsonPayload:
        return {
            "market_regime": self.market_regime,
            "average_return_bps": str(self.average_return_bps),
            "instrument_returns_bps": {
                instrument_id: str(value)
                for instrument_id, value in sorted(self.instrument_returns_bps.items())
            },
            "algorithm": "daily_first_open_to_last_close_equal_weight_v1",
        }


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None

"""Market source and quality helpers for operator read models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

JsonPayload = dict[str, Any]

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class SpreadMetrics:
    spread_abs: Decimal | None
    mid_price: Decimal | None
    spread_bps: Decimal | None


def calculate_spread_metrics(
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> SpreadMetrics:
    """Return spread in RUB and bps without mixing display units."""

    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        return SpreadMetrics(spread_abs=None, mid_price=None, spread_bps=None)
    spread_abs = best_ask - best_bid
    mid_price = (best_ask + best_bid) / Decimal("2")
    if mid_price <= 0:
        return SpreadMetrics(
            spread_abs=spread_abs,
            mid_price=mid_price,
            spread_bps=None,
        )
    return SpreadMetrics(
        spread_abs=spread_abs,
        mid_price=mid_price,
        spread_bps=spread_abs / mid_price * Decimal("10000"),
    )


def calculate_market_quality(
    *,
    spread_bps: Decimal | None,
    bid_depth_lots: Decimal | None,
    ask_depth_lots: Decimal | None,
    best_bid_qty_lots: Decimal | None = None,
    best_ask_qty_lots: Decimal | None = None,
    book_imbalance: Decimal | None,
    order_book_age_ms: int | None,
    order_book_stale: bool,
    venue_type: str,
    official_exchange_open: bool,
    trades_count: int,
) -> JsonPayload:
    """Initial transparent heuristic for display and calibration quality.

    This is intentionally componentized.  The weights are conservative until
    instrument-specific thresholds are calibrated on official exchange data.
    """

    reason_codes: list[str] = []
    spread_score = _spread_score(spread_bps)
    depth_score = _depth_score(bid_depth_lots, ask_depth_lots)
    touch_depth_score = _touch_depth_score(best_bid_qty_lots, best_ask_qty_lots)
    depth_concentration_score = _depth_concentration_score(
        bid_depth_lots=bid_depth_lots,
        ask_depth_lots=ask_depth_lots,
        best_bid_qty_lots=best_bid_qty_lots,
        best_ask_qty_lots=best_ask_qty_lots,
    )
    imbalance_score = _imbalance_score(book_imbalance)
    freshness_score = _freshness_score(order_book_age_ms, order_book_stale)
    venue_score = _venue_score(venue_type, official_exchange_open)
    trade_tape_score = Decimal("1.00") if trades_count > 0 else Decimal("0.85")
    if trades_count == 0:
        reason_codes.append("no_market_trades_samples")
    if not official_exchange_open or venue_type != "official_exchange":
        reason_codes.append("not_for_calibration")
    if order_book_stale:
        reason_codes.append("order_book_stale")
    if spread_bps is None:
        reason_codes.append("spread_unavailable")

    final_display_score = (
        spread_score * Decimal("0.22")
        + depth_score * Decimal("0.17")
        + touch_depth_score * Decimal("0.13")
        + depth_concentration_score * Decimal("0.08")
        + imbalance_score * Decimal("0.13")
        + freshness_score * Decimal("0.12")
        + venue_score * Decimal("0.10")
        + trade_tape_score * Decimal("0.05")
    )
    calibration_allowed = official_exchange_open and venue_type == "official_exchange"
    final_calibration_score = final_display_score if calibration_allowed else Decimal("0")
    label = _quality_label(
        display_score=final_display_score,
        calibration_allowed=calibration_allowed,
        spread_bps=spread_bps,
        order_book_stale=order_book_stale,
    )
    return {
        "spread_score": _q(spread_score),
        "depth_score": _q(depth_score),
        "touch_depth_score": _q(touch_depth_score),
        "depth_concentration_score": _q(depth_concentration_score),
        "imbalance_score": _q(imbalance_score),
        "freshness_score": _q(freshness_score),
        "venue_score": _q(venue_score),
        "trade_tape_score": _q(trade_tape_score),
        "final_display_score": _q(final_display_score),
        "final_calibration_score": _q(final_calibration_score),
        "display_market_quality_score": _q(final_display_score),
        "calibration_market_quality_score": _q(final_calibration_score),
        "market_quality_label": label,
        "reason_codes": reason_codes,
    }


def _spread_score(spread_bps: Decimal | None) -> Decimal:
    if spread_bps is None:
        return Decimal("0.20")
    if spread_bps <= Decimal("2"):
        return Decimal("1.00")
    if spread_bps <= Decimal("5"):
        return Decimal("0.85")
    if spread_bps <= Decimal("10"):
        return Decimal("0.65")
    if spread_bps <= Decimal("15"):
        return Decimal("0.50")
    if spread_bps <= Decimal("25"):
        return Decimal("0.35")
    return Decimal("0.20")


def _depth_score(
    bid_depth_lots: Decimal | None,
    ask_depth_lots: Decimal | None,
) -> Decimal:
    if bid_depth_lots is None or ask_depth_lots is None:
        return Decimal("0.35")
    total = max(Decimal("0"), bid_depth_lots) + max(Decimal("0"), ask_depth_lots)
    if total >= Decimal("5000"):
        return Decimal("0.85")
    if total >= Decimal("1000"):
        return Decimal("0.65")
    if total >= Decimal("300"):
        return Decimal("0.50")
    if total > 0:
        return Decimal("0.35")
    return Decimal("0.20")


def _touch_depth_score(
    best_bid_qty_lots: Decimal | None,
    best_ask_qty_lots: Decimal | None,
) -> Decimal:
    if best_bid_qty_lots is None or best_ask_qty_lots is None:
        return Decimal("0.40")
    touch = max(Decimal("0"), best_bid_qty_lots) + max(Decimal("0"), best_ask_qty_lots)
    if touch >= Decimal("1000"):
        return Decimal("0.85")
    if touch >= Decimal("300"):
        return Decimal("0.65")
    if touch >= Decimal("50"):
        return Decimal("0.50")
    if touch > 0:
        return Decimal("0.35")
    return Decimal("0.20")


def _depth_concentration_score(
    *,
    bid_depth_lots: Decimal | None,
    ask_depth_lots: Decimal | None,
    best_bid_qty_lots: Decimal | None,
    best_ask_qty_lots: Decimal | None,
) -> Decimal:
    if None in {bid_depth_lots, ask_depth_lots, best_bid_qty_lots, best_ask_qty_lots}:
        return Decimal("0.65")
    total = max(Decimal("0"), bid_depth_lots or Decimal("0")) + max(
        Decimal("0"), ask_depth_lots or Decimal("0")
    )
    touch = max(Decimal("0"), best_bid_qty_lots or Decimal("0")) + max(
        Decimal("0"), best_ask_qty_lots or Decimal("0")
    )
    if total <= 0:
        return Decimal("0.25")
    ratio = touch / total
    if ratio <= Decimal("0.35"):
        return Decimal("0.85")
    if ratio <= Decimal("0.60"):
        return Decimal("0.70")
    if ratio <= Decimal("0.80"):
        return Decimal("0.50")
    return Decimal("0.35")


def _imbalance_score(book_imbalance: Decimal | None) -> Decimal:
    if book_imbalance is None:
        return Decimal("0.50")
    value = abs(book_imbalance)
    if value <= Decimal("0.20"):
        return Decimal("0.90")
    if value <= Decimal("0.50"):
        return Decimal("0.70")
    if value <= Decimal("0.70"):
        return Decimal("0.45")
    return Decimal("0.25")


def _freshness_score(order_book_age_ms: int | None, order_book_stale: bool) -> Decimal:
    if order_book_age_ms is None:
        return Decimal("0.30")
    if order_book_stale:
        return Decimal("0.25")
    if order_book_age_ms <= 1_000:
        return Decimal("1.00")
    if order_book_age_ms <= 5_000:
        return Decimal("0.80")
    if order_book_age_ms <= 30_000:
        return Decimal("0.55")
    return Decimal("0.25")


def _venue_score(venue_type: str, official_exchange_open: bool) -> Decimal:
    if official_exchange_open and venue_type == "official_exchange":
        return Decimal("1.00")
    if venue_type == "broker_otc":
        return Decimal("0.45")
    if venue_type == "broker_indicative":
        return Decimal("0.35")
    if venue_type == "stale_local":
        return Decimal("0.20")
    return Decimal("0.25")


def _quality_label(
    *,
    display_score: Decimal,
    calibration_allowed: bool,
    spread_bps: Decimal | None,
    order_book_stale: bool,
) -> str:
    if not calibration_allowed:
        return "not_for_calibration"
    if order_book_stale:
        return "poor"
    if spread_bps is None:
        return "unknown"
    if display_score >= Decimal("0.80"):
        return "good"
    if display_score >= Decimal("0.65"):
        return "ok"
    if display_score >= Decimal("0.45"):
        return "weak"
    return "poor"


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"))

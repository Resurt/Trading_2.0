from __future__ import annotations

from decimal import Decimal

from trading_api.market_quality import calculate_market_quality, calculate_spread_metrics


def test_spread_abs_and_bps_are_separate_units() -> None:
    metrics = calculate_spread_metrics(Decimal("312.99"), Decimal("313.34"))

    assert metrics.spread_abs == Decimal("0.35")
    assert metrics.mid_price == Decimal("313.165")
    assert metrics.spread_bps is not None
    assert metrics.spread_bps.quantize(Decimal("0.01")) == Decimal("11.18")


def test_official_closed_otc_quality_not_for_calibration() -> None:
    quality = calculate_market_quality(
        spread_bps=Decimal("11.18"),
        bid_depth_lots=Decimal("212"),
        ask_depth_lots=Decimal("121"),
        best_bid_qty_lots=Decimal("2"),
        best_ask_qty_lots=Decimal("8"),
        book_imbalance=Decimal("0.65"),
        order_book_age_ms=900,
        order_book_stale=False,
        venue_type="broker_otc",
        official_exchange_open=False,
        trades_count=0,
    )

    assert quality["display_market_quality_score"] <= Decimal("0.600")
    assert quality["calibration_market_quality_score"] == Decimal("0.000")
    assert quality["market_quality_label"] == "not_for_calibration"
    assert "not_for_calibration" in quality["reason_codes"]
    assert "no_market_trades_samples" in quality["reason_codes"]

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from scripts.run_data_shadow_summary_report import summarize

from trading_common.db.models import MarketMicrostructureSnapshot


def make_snapshot(
    ts: datetime,
    *,
    instrument_id: str = "MOEX:SBER",
    session_type: str = "weekend",
    spread_bps: Decimal = Decimal("5"),
) -> MarketMicrostructureSnapshot:
    return MarketMicrostructureSnapshot(
        calendar_date=ts.date(),
        trading_date=ts.date(),
        session_type=session_type,
        session_phase="continuous_trading",
        micro_session_id=f"{ts.date()}:{session_type}:test",
        broker_trading_status="normal_trading",
        snapshot_id=uuid4(),
        ts_utc=ts,
        exchange_ts=ts,
        received_ts=ts,
        instrument_id=instrument_id,
        best_bid=Decimal("100"),
        best_ask=Decimal("100.05"),
        mid_price=Decimal("100.025"),
        spread_abs=Decimal("0.05"),
        spread_bps=spread_bps,
        bid_depth_lots=Decimal("40"),
        ask_depth_lots=Decimal("60"),
        book_imbalance=Decimal("0.20"),
        market_quality_score=Decimal("0.90"),
        feed_freshness_age_ms=100,
        is_stale=False,
        source="data_only_shadow",
        snapshot_payload={
            "source": "data_only_shadow",
            "venue_type": "official_exchange",
            "include_in_calibration": True,
            "calibration_allowed": True,
        },
    )


def test_data_shadow_summary_reports_calibration_rejections_and_gaps() -> None:
    valid = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)
    late = datetime(2026, 6, 28, 17, 0, tzinfo=UTC)
    rows = [
        make_snapshot(valid),
        make_snapshot(valid + timedelta(seconds=90)),
        make_snapshot(late, spread_bps=Decimal("100")),
    ]

    payload = summarize(rows, lookback_hours=24)

    assert payload["snapshots_count"] == 3
    assert payload["calibration_eligible_count"] == 2
    assert payload["calibration_rejected_count"] == 1
    assert payload["calibration_rejection_reasons"] == {"late_after_session_close": 1}
    assert payload["stream_gap_count"] == 2
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    assert "late_after_session_close_rows_excluded_from_calibration" in warnings

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
    session_phase: str = "continuous_trading",
    spread_bps: Decimal = Decimal("5"),
    is_stale: bool = False,
    strict_dual_freshness_eligible: bool = True,
) -> MarketMicrostructureSnapshot:
    return MarketMicrostructureSnapshot(
        calendar_date=ts.date(),
        trading_date=ts.date(),
        session_type=session_type,
        session_phase=session_phase,
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
        stale_by_exchange_time=is_stale,
        stale_by_received_time=False,
        freshness_basis="exchange_ts",
        strict_dual_freshness_eligible=strict_dual_freshness_eligible,
        is_stale=is_stale,
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
        make_snapshot(valid, instrument_id="uid-sber"),
        make_snapshot(valid + timedelta(seconds=90), instrument_id="uid-sber"),
        make_snapshot(late, instrument_id="uid-sber", spread_bps=Decimal("100")),
    ]

    payload = summarize(rows, lookback_hours=24)

    assert payload["snapshots_count"] == 3
    assert payload["calibration_eligible_count"] == 2
    assert payload["calibration_rejected_count"] == 1
    assert payload["calibration_rejection_reasons"] == {"late_after_session_close": 1}
    assert payload["stream_gap_count"] == 2
    assert payload["stream_gap_warning_count"] == 2
    assert payload["stream_gap_classification_counts"] == {"real_stream_gap": 2}
    assert payload["strict_timestamp_eligible_count"] == 3
    assert payload["diagnostic_eligible_count"] == 2
    assert payload["strict_timestamp_eligible_but_calibration_rejected_count"] == 1
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    assert "late_after_session_close_rows_excluded_from_calibration" in warnings
    assert "stream_gaps_detected" in warnings


def test_data_shadow_summary_classifies_session_boundary_gap_as_info() -> None:
    first = datetime(2026, 6, 28, 6, 59, tzinfo=UTC)
    rows = [
        make_snapshot(first, session_type="weekday_morning"),
        make_snapshot(first + timedelta(seconds=90), session_type="weekday_main"),
    ]

    payload = summarize(rows, lookback_hours=24)

    assert payload["stream_gap_count"] == 1
    assert payload["stream_gap_warning_count"] == 0
    assert payload["stream_gap_info_count"] == 1
    assert payload["stream_gap_classification_counts"] == {"session_boundary_gap": 1}
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    assert "stream_gaps_detected" not in warnings


def test_data_shadow_summary_splits_strict_timestamp_and_calibration_eligibility() -> None:
    ts = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)
    rows = [make_snapshot(ts, is_stale=True, strict_dual_freshness_eligible=True)]

    payload = summarize(rows, lookback_hours=24)

    assert payload["strict_timestamp_eligible_count"] == 1
    assert payload["diagnostic_eligible_count"] == 1
    assert payload["calibration_eligible_count"] == 0
    assert payload["calibration_rejection_reasons"] == {"stale": 1}
    assert payload["strict_timestamp_eligible_but_calibration_rejected_count"] == 1

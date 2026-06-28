from __future__ import annotations

from argparse import Namespace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from scripts.run_purge_invalid_data_shadow_rows import run_purge
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from trading_common.db.base import Base
from trading_common.db.models import (
    AuditEvent,
    IntradaySessionAnalytics,
    MarketMicrostructureSnapshot,
    OrderBookSummary,
)


def test_purge_invalid_data_shadow_rows_removes_late_rows_only(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'purge.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        valid_ts = datetime(2026, 6, 28, 15, 59, tzinfo=UTC)
        late_ts = datetime(2026, 6, 28, 16, 1, tzinfo=UTC)
        session.add(_snapshot(valid_ts))
        session.add(_snapshot(late_ts))
        session.add(_order_book(valid_ts))
        session.add(_order_book(late_ts))
        session.add(_intraday_row())
        session.commit()

    dry_run = cast(
        dict[str, Any],
        run_purge(
            Namespace(
                date="2026-06-28",
                reason="late_after_session_close_bug",
                database_url=database_url,
                manifest_path=None,
                max_rows=50_000,
                dry_run=True,
                apply=False,
                json_output=True,
            )
        )
    )

    assert dry_run["before"]["market_microstructure_snapshot"]["late_rows"] == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketMicrostructureSnapshot)) == 2

    applied = cast(
        dict[str, Any],
        run_purge(
            Namespace(
                date="2026-06-28",
                reason="late_after_session_close_bug",
                database_url=database_url,
                manifest_path=".local/test-manifest.json",
                max_rows=50_000,
                dry_run=False,
                apply=True,
                json_output=True,
            )
        )
    )

    assert applied["deleted"]["market_microstructure_snapshot"] == 1
    assert applied["deleted"]["order_book_summary"] == 1
    assert applied["deleted"]["intraday_session_analytics"] == 1
    assert applied["after"]["market_microstructure_snapshot"]["late_rows"] == 0
    with Session(engine) as session:
        remaining_snapshot = session.execute(
            select(MarketMicrostructureSnapshot)
        ).scalar_one()
        assert remaining_snapshot.ts_utc.replace(tzinfo=UTC) == valid_ts
        remaining_book = session.execute(select(OrderBookSummary)).scalar_one()
        assert remaining_book.ts_utc.replace(tzinfo=UTC) == valid_ts
        audit = session.execute(
            select(AuditEvent).where(AuditEvent.action == "data_only_invalid_rows_purged")
        ).scalar_one()
        audit_payload = cast(dict[str, Any], audit.audit_payload)
        deleted_payload = cast(dict[str, Any], audit_payload["deleted"])
        assert deleted_payload["market_microstructure_snapshot"] == 1
        assert audit_payload["audit_events_deleted"] == 0

    engine.dispose()


def _session_context(ts: datetime) -> dict[str, object]:
    return {
        "calendar_date": date(2026, 6, 28),
        "trading_date": date(2026, 6, 28),
        "session_type": "weekend",
        "session_phase": "continuous_trading",
        "micro_session_id": "2026-06-28:weekend:test",
        "broker_trading_status": "normal_trading",
        "ts_utc": ts,
        "exchange_ts": ts,
        "received_ts": ts,
    }


def _snapshot(ts: datetime) -> MarketMicrostructureSnapshot:
    return MarketMicrostructureSnapshot(
        snapshot_id=uuid4(),
        **_session_context(ts),
        instrument_id="MOEX:SBER",
        best_bid=Decimal("100"),
        best_ask=Decimal("100.05"),
        mid_price=Decimal("100.025"),
        spread_abs=Decimal("0.05"),
        spread_bps=Decimal("4.9988"),
        bid_depth_lots=Decimal("10"),
        ask_depth_lots=Decimal("12"),
        book_imbalance=Decimal("-0.0909"),
        market_quality_score=Decimal("0.90"),
        feed_freshness_age_ms=100,
        is_stale=False,
        source="data_only_shadow",
        snapshot_payload={
            "source": "data_only_shadow",
            "include_in_calibration": True,
            "calibration_allowed": True,
        },
    )


def _order_book(ts: datetime) -> OrderBookSummary:
    return OrderBookSummary(
        order_book_summary_id=uuid4(),
        **_session_context(ts),
        instrument_id="MOEX:SBER",
        depth_levels=10,
        best_bid_price=Decimal("100"),
        best_bid_qty_lots=Decimal("10"),
        best_ask_price=Decimal("100.05"),
        best_ask_qty_lots=Decimal("12"),
        mid_price=Decimal("100.025"),
        spread_abs=Decimal("0.05"),
        spread_bps=Decimal("4.9988"),
        bid_depth_lots=Decimal("10"),
        ask_depth_lots=Decimal("12"),
        book_imbalance=Decimal("-0.0909"),
        market_quality_score=Decimal("0.90"),
        summary_payload={"source": "data_only_shadow"},
    )


def _intraday_row() -> IntradaySessionAnalytics:
    return IntradaySessionAnalytics(
        intraday_analytics_id=uuid4(),
        generated_at=datetime(2026, 6, 28, 18, 0, tzinfo=UTC),
        trading_date=date(2026, 6, 28),
        calendar_date=date(2026, 6, 28),
        session_type="weekend",
        session_phase="continuous_trading",
        micro_session_id=None,
        hour_bucket=None,
        instrument_id=None,
        timeframe=None,
        side="all",
        mode="data_shadow",
        market_bias="unknown",
        market_activity="high",
        trend_strength=None,
        candidate_count=0,
        pseudo_order_count=0,
        real_order_count=0,
        blocked_count=0,
        near_miss_count=0,
        avg_spread_bps=Decimal("5"),
        p95_spread_bps=Decimal("5"),
        avg_depth=Decimal("11"),
        avg_imbalance=Decimal("-0.0909"),
        avg_market_quality=Decimal("0.90"),
        stale_incidents=0,
        candle_lag_p95_seconds=None,
        gross_pnl_proxy=Decimal("0"),
        net_pnl_proxy=Decimal("0"),
        analytics_payload={"source": "test"},
    )

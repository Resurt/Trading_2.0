"""Backfill exchange/received timestamp freshness metadata for data-only rows."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from sys import path
from typing import Any

from sqlalchemy import case, func, select, update

ROOT = Path(__file__).resolve().parents[1]
for src in (ROOT / "packages" / "common" / "src",):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import (
    AuditEvent,
    MarketMicrostructureSnapshot,
    OrderBookSummary,
)
from trading_common.db.service import DatabaseService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist metadata updates.")
    parser.add_argument("--dry-run", action="store_true", help="Preview updates only.")
    parser.add_argument("--json-output", action="store_true")
    args = parser.parse_args()

    apply = bool(args.apply)
    result = run_backfill(apply=apply)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    print(text if args.json_output else text)
    return 0 if result["status"] in {"dry_run", "applied"} else 1


def run_backfill(*, apply: bool) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    database = DatabaseService(build_database_url_from_env())
    try:
        with database.session_scope() as session:
            summary_stats = _summary_stats(session)
            micro_stats_before = _micro_stats(session)
            micro_join_count = _micro_join_exchange_ts_count(session)

            result: dict[str, Any] = {
                "status": "applied" if apply else "dry_run",
                "dry_run": not apply,
                "does_not_fabricate_exchange_ts": True,
                "source_for_microstructure_exchange_ts": "matching_order_book_summary_same_event",
                "order_book_summary": summary_stats,
                "market_microstructure_snapshot_before": micro_stats_before,
                "market_microstructure_exchange_ts_backfillable_from_order_book_summary": (
                    micro_join_count
                ),
                "audit_event_written": False,
            }

            if not apply:
                session.rollback()
                return result

            _apply_order_book_summary_metadata(session)
            _apply_microstructure_exchange_ts_from_order_book_summary(session)
            _apply_microstructure_metadata(session)
            _write_audit_event(session, now=now, payload=result)
            session.flush()

            result["market_microstructure_snapshot_after"] = _micro_stats(session)
            result["audit_event_written"] = True
            return result
    finally:
        database.engine.dispose()


def _summary_stats(session: Any) -> dict[str, int]:
    row = session.execute(
        select(
            func.count().label("total"),
            func.count().filter(OrderBookSummary.exchange_ts.is_not(None)).label("exchange_present"),
            func.count().filter(OrderBookSummary.exchange_ts.is_(None)).label("exchange_missing"),
            func.count()
            .filter(OrderBookSummary.strict_dual_freshness_eligible.is_(True))
            .label("strict_eligible"),
        )
    ).one()
    return {key: int(getattr(row, key) or 0) for key in row._fields}


def _micro_stats(session: Any) -> dict[str, int]:
    row = session.execute(
        select(
            func.count().label("total"),
            func.count()
            .filter(MarketMicrostructureSnapshot.exchange_ts.is_not(None))
            .label("exchange_present"),
            func.count()
            .filter(MarketMicrostructureSnapshot.exchange_ts.is_(None))
            .label("exchange_missing"),
            func.count()
            .filter(MarketMicrostructureSnapshot.strict_dual_freshness_eligible.is_(True))
            .label("strict_eligible"),
            func.count()
            .filter(MarketMicrostructureSnapshot.freshness_basis == "received_ts_only")
            .label("received_ts_only"),
        )
    ).one()
    return {key: int(getattr(row, key) or 0) for key in row._fields}


def _micro_join_exchange_ts_count(session: Any) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(MarketMicrostructureSnapshot)
            .join(
                OrderBookSummary,
                (OrderBookSummary.trading_date == MarketMicrostructureSnapshot.trading_date)
                & (OrderBookSummary.instrument_id == MarketMicrostructureSnapshot.instrument_id)
                & (OrderBookSummary.ts_utc == MarketMicrostructureSnapshot.ts_utc)
                & (OrderBookSummary.received_ts == MarketMicrostructureSnapshot.received_ts),
            )
            .where(
                MarketMicrostructureSnapshot.exchange_ts.is_(None),
                OrderBookSummary.exchange_ts.is_not(None),
            )
        ).scalar()
        or 0
    )


def _apply_order_book_summary_metadata(session: Any) -> None:
    session.execute(
        update(OrderBookSummary).values(
            exchange_age_ms=OrderBookSummary.summary_payload["feed_freshness"][
                "exchange_age_ms"
            ].as_integer(),
            received_age_ms=OrderBookSummary.summary_payload["feed_freshness"][
                "received_age_ms"
            ].as_integer(),
            stale_by_exchange_time=func.coalesce(
                OrderBookSummary.summary_payload["feed_freshness"][
                    "stale_by_exchange_time"
                ].as_boolean(),
                False,
            ),
            stale_by_received_time=func.coalesce(
                OrderBookSummary.summary_payload["feed_freshness"][
                    "stale_by_received_time"
                ].as_boolean(),
                False,
            ),
            freshness_basis=func.coalesce(
                OrderBookSummary.summary_payload["freshness_basis"].as_string(),
                "exchange_ts",
            ),
            exchange_ts_missing_reason=None,
            strict_dual_freshness_eligible=OrderBookSummary.exchange_ts.is_not(None),
        )
    )


def _apply_microstructure_exchange_ts_from_order_book_summary(session: Any) -> None:
    subq = (
        select(OrderBookSummary.exchange_ts)
        .where(
            OrderBookSummary.trading_date == MarketMicrostructureSnapshot.trading_date,
            OrderBookSummary.instrument_id == MarketMicrostructureSnapshot.instrument_id,
            OrderBookSummary.ts_utc == MarketMicrostructureSnapshot.ts_utc,
            OrderBookSummary.received_ts == MarketMicrostructureSnapshot.received_ts,
            OrderBookSummary.exchange_ts.is_not(None),
        )
        .limit(1)
        .scalar_subquery()
    )
    session.execute(
        update(MarketMicrostructureSnapshot)
        .where(MarketMicrostructureSnapshot.exchange_ts.is_(None))
        .values(exchange_ts=subq)
    )


def _apply_microstructure_metadata(session: Any) -> None:
    freshness = MarketMicrostructureSnapshot.snapshot_payload["feed_freshness"]
    session.execute(
        update(MarketMicrostructureSnapshot).values(
            exchange_age_ms=freshness["exchange_age_ms"].as_integer(),
            received_age_ms=freshness["received_age_ms"].as_integer(),
            stale_by_exchange_time=func.coalesce(
                freshness["stale_by_exchange_time"].as_boolean(),
                False,
            ),
            stale_by_received_time=func.coalesce(
                freshness["stale_by_received_time"].as_boolean(),
                False,
            ),
            freshness_basis=func.coalesce(
                MarketMicrostructureSnapshot.snapshot_payload["freshness_basis"].as_string(),
                case(
                    (MarketMicrostructureSnapshot.exchange_ts.is_not(None), "exchange_ts"),
                    else_="received_ts_only",
                ),
            ),
            exchange_ts_missing_reason=case(
                (
                    MarketMicrostructureSnapshot.exchange_ts.is_(None),
                    func.coalesce(
                        MarketMicrostructureSnapshot.snapshot_payload[
                            "exchange_ts_missing_reason"
                        ].as_string(),
                        "source_payload_missing_exchange_ts",
                    ),
                ),
                else_=None,
            ),
            strict_dual_freshness_eligible=MarketMicrostructureSnapshot.exchange_ts.is_not(None),
        )
    )


def _write_audit_event(session: Any, *, now: datetime, payload: dict[str, Any]) -> None:
    event = AuditEvent(
        ts_utc=now,
        exchange_ts=None,
        received_ts=now,
        calendar_date=now.date(),
        trading_date=now.date(),
        session_type="weekend",
        session_phase="closed",
        micro_session_id=f"maintenance_{now:%Y%m%d_%H}",
        broker_trading_status="closed",
        service="trade_core",
        actor="system",
        action="data_only_exchange_ts_metadata_backfilled",
        entity_type="market_microstructure_snapshot",
        entity_id="exchange_ts_metadata_backfill",
        severity="info",
        correlation_id=None,
        audit_payload=payload,
    )
    session.add(event)


if __name__ == "__main__":
    raise SystemExit(main())

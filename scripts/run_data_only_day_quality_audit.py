"""Audit one data-only shadow collection day after quality remediation."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from sys import path
from zoneinfo import ZoneInfo

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
for src in (ROOT / "packages" / "common" / "src",):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import AuditEvent, MarketMicrostructureSnapshot, OrderBookSummary
from trading_common.db.service import DatabaseService

MSK = ZoneInfo("Europe/Moscow")


def main() -> None:
    args = parse_args()
    payload = run_audit(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    raise SystemExit(0 if payload["passed"] else 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Collection date, YYYY-MM-DD.")
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def run_audit(args: argparse.Namespace) -> dict[str, object]:
    collection_date = date.fromisoformat(args.date)
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            micro_rows = list(
                session.execute(
                    select(MarketMicrostructureSnapshot).where(
                        MarketMicrostructureSnapshot.trading_date == collection_date,
                        MarketMicrostructureSnapshot.source == "data_only_shadow",
                    )
                ).scalars()
            )
            order_book_rows = list(
                session.execute(
                    select(OrderBookSummary).where(
                        OrderBookSummary.trading_date == collection_date
                    )
                ).scalars()
            )
            session_counts = Counter(row.session_type for row in micro_rows)
            instrument_sessions: dict[str, Counter[str]] = {}
            for row in micro_rows:
                instrument_sessions.setdefault(row.instrument_id, Counter())[row.session_type] += 1
            invalid_micro = [row for row in micro_rows if _invalid_micro(row)]
            invalid_order_books = [row for row in order_book_rows if _invalid_order_book(row)]
            wrong_micro = [row for row in micro_rows if _wrong_context(row)]
            wrong_order_books = [row for row in order_book_rows if _wrong_context(row)]
            audit_events = list(
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.trading_date == collection_date,
                        AuditEvent.action.in_(
                            {
                                "data_only_quality_rows_repaired",
                                "data_only_invalid_rows_purged",
                            }
                        ),
                    )
                ).scalars()
            )
            main_evening_count = sum(
                1
                for counts in instrument_sessions.values()
                if counts.get("weekday_main", 0) > 0
                and counts.get("weekday_evening", 0) > 0
            )
            morning_missing = session_counts.get("weekday_morning", 0) == 0
            passed = (
                len(invalid_micro) == 0
                and len(invalid_order_books) == 0
                and len(wrong_micro) == 0
                and len(wrong_order_books) == 0
                and main_evening_count == 8
                and len(audit_events) >= 2
            )
            return {
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "collection_date": collection_date.isoformat(),
                "passed": passed,
                "dataset_classification": (
                    "partial_main_evening_clean"
                    if morning_missing
                    else "full_or_morning_present_clean"
                ),
                "weekday_morning_missing_classification": (
                    "operational_missing_start" if morning_missing else "not_missing"
                ),
                "main_rows_present": session_counts.get("weekday_main", 0) > 0,
                "evening_rows_present": session_counts.get("weekday_evening", 0) > 0,
                "main_evening_instrument_count": main_evening_count,
                "invalid_formula_rows": len(invalid_micro),
                "invalid_order_book_formula_rows": len(invalid_order_books),
                "wrong_micro_session_rows": len(wrong_micro),
                "wrong_order_book_micro_session_rows": len(wrong_order_books),
                "purge_policy_valid": len(invalid_micro) == 0 and len(invalid_order_books) == 0,
                "audit_event_remediation_count": len(audit_events),
                "valid_rows_retained": len(micro_rows),
                "session_counts": dict(sorted(session_counts.items())),
                "warnings": [
                    "weekday_morning rows missing because the robot was not started in morning"
                ]
                if morning_missing
                else [],
            }
    finally:
        database.engine.dispose()


def _invalid_micro(row: MarketMicrostructureSnapshot) -> bool:
    return (
        row.best_bid is None
        or row.best_ask is None
        or row.best_ask < row.best_bid
        or row.mid_price is None
        or row.mid_price <= 0
        or row.spread_abs is None
        or row.spread_abs < 0
        or row.spread_bps is None
        or row.spread_bps < 0
        or row.bid_depth_lots is None
        or row.bid_depth_lots < 0
        or row.ask_depth_lots is None
        or row.ask_depth_lots < 0
        or row.book_imbalance is None
        or row.book_imbalance < Decimal("-1")
        or row.book_imbalance > Decimal("1")
    )


def _invalid_order_book(row: OrderBookSummary) -> bool:
    return (
        row.best_bid_price is None
        or row.best_ask_price is None
        or row.best_ask_price < row.best_bid_price
        or row.mid_price is None
        or row.mid_price <= 0
        or row.spread_abs is None
        or row.spread_abs < 0
        or row.spread_bps is None
        or row.spread_bps < 0
        or row.bid_depth_lots < 0
        or row.ask_depth_lots < 0
        or row.book_imbalance is None
        or row.book_imbalance < Decimal("-1")
        or row.book_imbalance > Decimal("1")
    )


def _wrong_context(row: object) -> bool:
    ts = _row_ts_msk(row)
    session_type = _session_type_for(ts)
    if session_type == "closed":
        return True
    micro_session_id = (
        f"{ts.date().isoformat()}:{session_type}:"
        f"{ts.replace(minute=0, second=0, microsecond=0):%Y%m%dT%H%M}"
    )
    return row.session_type != session_type or row.micro_session_id != micro_session_id


def _row_ts_msk(row: object) -> datetime:
    value = getattr(row, "received_ts", None)
    if value is None:
        value = row.ts_utc  # type: ignore[attr-defined]
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK)


def _session_type_for(moment: datetime) -> str:
    local_time = moment.timetz().replace(tzinfo=None)
    if time(7, 0) <= local_time < time(10, 0):
        return "weekday_morning"
    if time(10, 0) <= local_time < time(19, 0):
        return "weekday_main"
    if time(19, 0) <= local_time < time(23, 50):
        return "weekday_evening"
    return "closed"


if __name__ == "__main__":
    main()

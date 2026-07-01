"""Repair deterministic data-only shadow quality issues for one collection date.

The script is deliberately narrow:
- invalid market values are purged from primary data-only rows;
- deterministic session/hour metadata is repaired when market values are valid;
- audit_event and robot_command are never deleted or modified.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from sys import path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
for src in (ROOT / "packages" / "common" / "src",):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import AuditEvent, MarketMicrostructureSnapshot, OrderBookSummary
from trading_common.db.service import DatabaseService

MSK = ZoneInfo("Europe/Moscow")
MAINTENANCE_ACTOR = "run_repair_data_shadow_quality_issues"


@dataclass(frozen=True, slots=True)
class ExpectedContext:
    session_type: str
    session_phase: str
    micro_session_id: str
    calendar_date: date
    trading_date: date


def main() -> None:
    args = parse_args()
    payload = run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def run(args: argparse.Namespace) -> dict[str, object]:
    collection_date = date.fromisoformat(args.date)
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            before = inventory(session, collection_date)
            invalid_micro = _invalid_micro_rows(session, collection_date)
            invalid_order_books = _matching_invalid_order_books(
                session,
                collection_date,
                invalid_micro,
            )
            repair_micro = _wrong_context_micro_rows(session, collection_date)
            invalid_micro_ids = {row.snapshot_id for row in invalid_micro}
            repair_micro = [row for row in repair_micro if row.snapshot_id not in invalid_micro_ids]
            repair_order_books = _wrong_context_order_book_rows(session, collection_date)
            invalid_order_book_ids = {row.order_book_summary_id for row in invalid_order_books}
            repair_order_books = [
                row
                for row in repair_order_books
                if row.order_book_summary_id not in invalid_order_book_ids
                and not _invalid_order_book(row)
            ]

            purge_count = len(invalid_micro) + len(invalid_order_books)
            repair_count = len(repair_micro) + len(repair_order_books)
            if args.apply and purge_count > args.max_purge_rows:
                raise SystemExit(
                    f"Refusing to purge {purge_count} rows; raise --max-purge-rows to proceed"
                )
            if args.apply and repair_count > args.max_repair_rows:
                raise SystemExit(
                    f"Refusing to repair {repair_count} rows; raise --max-repair-rows to proceed"
                )

            deleted = {"market_microstructure_snapshot": 0, "order_book_summary": 0}
            repaired = {"market_microstructure_snapshot": 0, "order_book_summary": 0}
            audit_event_ids: list[str] = []
            if args.apply:
                repaired = _repair_rows(session, repair_micro, repair_order_books)
                deleted = _delete_rows(session, invalid_micro, invalid_order_books)
                if repaired["market_microstructure_snapshot"] or repaired["order_book_summary"]:
                    event = _write_audit_event(
                        session,
                        collection_date=collection_date,
                        action="data_only_quality_rows_repaired",
                        severity="info",
                        payload={
                            "reason": "deterministic_micro_session_metadata_repair",
                            "repaired": repaired,
                            "sample_snapshot_ids": [
                                str(row.snapshot_id) for row in repair_micro[:20]
                            ],
                            "sample_order_book_summary_ids": [
                                str(row.order_book_summary_id)
                                for row in repair_order_books[:20]
                            ],
                            "audit_events_deleted": 0,
                        },
                    )
                    audit_event_ids.append(str(event.audit_event_id))
                if deleted["market_microstructure_snapshot"] or deleted["order_book_summary"]:
                    event = _write_audit_event(
                        session,
                        collection_date=collection_date,
                        action="data_only_invalid_rows_purged",
                        severity="warning",
                        payload={
                            "reason": "crossed_book_or_invalid_formula",
                            "deleted": deleted,
                            "sample_snapshot_ids": [
                                str(row.snapshot_id) for row in invalid_micro[:20]
                            ],
                            "sample_order_book_summary_ids": [
                                str(row.order_book_summary_id)
                                for row in invalid_order_books[:20]
                            ],
                            "audit_events_deleted": 0,
                        },
                    )
                    audit_event_ids.append(str(event.audit_event_id))

            after = inventory(session, collection_date)
            return {
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "mode": "apply" if args.apply else "dry_run",
                "dry_run": not args.apply,
                "applied": bool(args.apply),
                "collection_date": collection_date.isoformat(),
                "before": before,
                "planned": {
                    "invalid_formula_rows_to_purge": {
                        "market_microstructure_snapshot": len(invalid_micro),
                        "order_book_summary": len(invalid_order_books),
                        "sample_snapshot_ids": [str(row.snapshot_id) for row in invalid_micro[:20]],
                        "sample_order_book_summary_ids": [
                            str(row.order_book_summary_id) for row in invalid_order_books[:20]
                        ],
                    },
                    "micro_session_rows_to_repair": {
                        "market_microstructure_snapshot": len(repair_micro),
                        "order_book_summary": len(repair_order_books),
                        "sample_snapshot_ids": [str(row.snapshot_id) for row in repair_micro[:20]],
                        "sample_order_book_summary_ids": [
                            str(row.order_book_summary_id) for row in repair_order_books[:20]
                        ],
                    },
                    "micro_session_rows_to_purge_if_any": 0,
                },
                "deleted": deleted,
                "repaired": repaired,
                "after": after,
                "audit_event_ids": audit_event_ids,
                "safety": {
                    "requires_explicit_apply": True,
                    "max_purge_rows": args.max_purge_rows,
                    "max_repair_rows": args.max_repair_rows,
                    "audit_events_deleted": 0,
                    "robot_commands_touched": 0,
                    "market_candle_touched": 0,
                },
            }
    finally:
        database.engine.dispose()


def inventory(session: Session, collection_date: date) -> dict[str, object]:
    micro = session.execute(
        select(MarketMicrostructureSnapshot).where(
            MarketMicrostructureSnapshot.trading_date == collection_date,
            MarketMicrostructureSnapshot.source == "data_only_shadow",
        )
    ).scalars()
    order_books = session.execute(
        select(OrderBookSummary).where(OrderBookSummary.trading_date == collection_date)
    ).scalars()
    micro_rows = list(micro)
    order_book_rows = list(order_books)
    wrong_micro = [row for row in micro_rows if _wrong_context(row)]
    wrong_order_book = [row for row in order_book_rows if _wrong_context(row)]
    invalid_micro = [row for row in micro_rows if _invalid_micro(row)]
    invalid_order_books = [row for row in order_book_rows if _invalid_order_book(row)]
    session_counts = Counter(row.session_type for row in micro_rows)
    instruments = Counter(row.instrument_id for row in micro_rows)
    return {
        "market_microstructure_snapshot_rows": len(micro_rows),
        "order_book_summary_rows": len(order_book_rows),
        "invalid_formula_micro_rows": len(invalid_micro),
        "invalid_formula_order_book_rows": len(invalid_order_books),
        "wrong_micro_session_micro_rows": len(wrong_micro),
        "wrong_micro_session_order_book_rows": len(wrong_order_book),
        "session_counts": dict(sorted(session_counts.items())),
        "instrument_counts": dict(sorted(instruments.items())),
        "sample_invalid_snapshot_ids": [str(row.snapshot_id) for row in invalid_micro[:20]],
        "sample_wrong_snapshot_ids": [str(row.snapshot_id) for row in wrong_micro[:20]],
    }


def _invalid_micro_rows(
    session: Session,
    collection_date: date,
) -> list[MarketMicrostructureSnapshot]:
    return [
        row
        for row in session.execute(
            select(MarketMicrostructureSnapshot).where(
                MarketMicrostructureSnapshot.trading_date == collection_date,
                MarketMicrostructureSnapshot.source == "data_only_shadow",
            )
        ).scalars()
        if _invalid_micro(row)
    ]


def _matching_invalid_order_books(
    session: Session,
    collection_date: date,
    invalid_micro: list[MarketMicrostructureSnapshot],
) -> list[OrderBookSummary]:
    order_books = list(
        session.execute(
            select(OrderBookSummary).where(OrderBookSummary.trading_date == collection_date)
        ).scalars()
    )
    invalid_keys = {
        (row.instrument_id, row.ts_utc, row.received_ts, row.micro_session_id)
        for row in invalid_micro
    }
    return [
        row
        for row in order_books
        if _invalid_order_book(row)
        or (row.instrument_id, row.ts_utc, row.received_ts, row.micro_session_id) in invalid_keys
    ]


def _wrong_context_micro_rows(
    session: Session,
    collection_date: date,
) -> list[MarketMicrostructureSnapshot]:
    return [
        row
        for row in session.execute(
            select(MarketMicrostructureSnapshot).where(
                MarketMicrostructureSnapshot.trading_date == collection_date,
                MarketMicrostructureSnapshot.source == "data_only_shadow",
            )
        ).scalars()
        if _wrong_context(row)
    ]


def _wrong_context_order_book_rows(
    session: Session,
    collection_date: date,
) -> list[OrderBookSummary]:
    return [
        row
        for row in session.execute(
            select(OrderBookSummary).where(OrderBookSummary.trading_date == collection_date)
        ).scalars()
        if _wrong_context(row)
    ]


def _delete_rows(
    session: Session,
    micro_rows: list[MarketMicrostructureSnapshot],
    order_book_rows: list[OrderBookSummary],
) -> dict[str, int]:
    micro_ids = [row.snapshot_id for row in micro_rows]
    order_book_ids = [row.order_book_summary_id for row in order_book_rows]
    deleted_micro = 0
    deleted_order_books = 0
    if micro_ids:
        deleted_micro = int(
            session.execute(
                delete(MarketMicrostructureSnapshot).where(
                    MarketMicrostructureSnapshot.snapshot_id.in_(micro_ids)
                )
            ).rowcount
            or 0
        )
    if order_book_ids:
        deleted_order_books = int(
            session.execute(
                delete(OrderBookSummary).where(
                    OrderBookSummary.order_book_summary_id.in_(order_book_ids)
                )
            ).rowcount
            or 0
        )
    return {
        "market_microstructure_snapshot": deleted_micro,
        "order_book_summary": deleted_order_books,
    }


def _repair_rows(
    session: Session,
    micro_rows: list[MarketMicrostructureSnapshot],
    order_book_rows: list[OrderBookSummary],
) -> dict[str, int]:
    repaired_micro = 0
    repaired_order_books = 0
    for row in micro_rows:
        context = _expected_context(row)
        _apply_context(row, context)
        repaired_micro += 1
    for row in order_book_rows:
        context = _expected_context(row)
        _apply_context(row, context)
        repaired_order_books += 1
    session.flush()
    return {
        "market_microstructure_snapshot": repaired_micro,
        "order_book_summary": repaired_order_books,
    }


def _apply_context(row: object, context: ExpectedContext) -> None:
    row.calendar_date = context.calendar_date
    row.trading_date = context.trading_date
    row.session_type = context.session_type
    row.session_phase = context.session_phase
    row.micro_session_id = context.micro_session_id


def _wrong_context(row: object) -> bool:
    context = _expected_context(row)
    if context.session_type == "closed":
        return True
    return (
        row.calendar_date != context.calendar_date
        or row.trading_date != context.trading_date
        or row.session_type != context.session_type
        or row.session_phase != context.session_phase
        or row.micro_session_id != context.micro_session_id
    )


def _expected_context(row: object) -> ExpectedContext:
    ts = _row_event_ts_msk(row)
    session_type = _session_type_for(ts)
    micro_session_id = (
        f"{ts.date().isoformat()}:{session_type}:"
        f"{ts.replace(minute=0, second=0, microsecond=0):%Y%m%dT%H%M}"
    )
    session_phase = "continuous_trading" if session_type != "closed" else "closed"
    return ExpectedContext(
        session_type=session_type,
        session_phase=session_phase,
        micro_session_id=micro_session_id,
        calendar_date=ts.date(),
        trading_date=ts.date(),
    )


def _row_event_ts_msk(row: object) -> datetime:
    # received_ts is the durable collection timestamp used by data-only context.
    value = getattr(row, "received_ts", None)
    if value is None:
        value = row.ts_utc  # type: ignore[attr-defined]
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK)


def _session_type_for(moment_msk: datetime) -> str:
    local_time = moment_msk.timetz().replace(tzinfo=None)
    if time(7, 0) <= local_time < time(10, 0):
        return "weekday_morning"
    if time(10, 0) <= local_time < time(19, 0):
        return "weekday_main"
    if time(19, 0) <= local_time < time(23, 50):
        return "weekday_evening"
    return "closed"


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
        or _expected_context(row).session_type == "closed"
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
        or _expected_context(row).session_type == "closed"
    )


def _write_audit_event(
    session: Session,
    *,
    collection_date: date,
    action: str,
    severity: str,
    payload: dict[str, object],
) -> AuditEvent:
    now = datetime.now(tz=UTC)
    event = AuditEvent(
        audit_event_id=uuid4(),
        calendar_date=collection_date,
        trading_date=collection_date,
        session_type="weekday_main",
        session_phase="closed",
        micro_session_id=f"{collection_date.isoformat()}:maintenance:quality_repair",
        broker_trading_status="unknown",
        ts_utc=now,
        exchange_ts=None,
        received_ts=now,
        service="maintenance",
        actor=MAINTENANCE_ACTOR,
        action=action,
        entity_type="data_shadow_collection",
        entity_id=collection_date.isoformat(),
        severity=severity,
        correlation_id=str(uuid4()),
        audit_payload={
            "collection_date": collection_date.isoformat(),
            **payload,
        },
    )
    session.add(event)
    session.flush()
    return event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Collection date, YYYY-MM-DD.")
    parser.add_argument("--database-url")
    parser.add_argument("--max-purge-rows", type=int, default=100)
    parser.add_argument("--max-repair-rows", type=int, default=1000)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        args.dry_run = True
    return args


if __name__ == "__main__":
    main()

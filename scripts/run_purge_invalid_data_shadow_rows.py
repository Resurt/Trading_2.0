"""Purge known-invalid data-only shadow rows after a session close bug."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from sys import path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import (
    AuditEvent,
    IntradaySessionAnalytics,
    MarketMicrostructureSnapshot,
    OrderBookSummary,
    RollingPerformanceCube,
)
from trading_common.db.service import DatabaseService

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
VALID_REASONS = {"late_after_session_close_bug"}
CountsPayload = dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class PurgeWindow:
    collection_date: date
    session_type: str
    window_start_msk: datetime
    cutoff_msk: datetime
    window_start_utc: datetime
    cutoff_utc: datetime
    day_end_utc: datetime


def main() -> None:
    args = parse_args()
    payload = run_purge(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def run_purge(args: argparse.Namespace) -> dict[str, object]:
    if args.reason not in VALID_REASONS:
        msg = f"Unsupported purge reason: {args.reason}"
        raise SystemExit(msg)
    if bool(args.apply) == bool(args.dry_run):
        msg = "Specify exactly one of --dry-run or --apply"
        raise SystemExit(msg)

    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            collection_date = _parse_collection_date(args.date, session)
            window = _purge_window(session, collection_date)
            before = _counts(session, window)
            total_primary_rows = (
                _count_from(before, "market_microstructure_snapshot", "late_rows")
                + _count_from(before, "order_book_summary", "late_rows")
            )
            if args.apply and total_primary_rows > args.max_rows:
                msg = (
                    f"Refusing to purge {total_primary_rows} primary rows; "
                    f"raise --max-rows above that value to proceed"
                )
                raise SystemExit(msg)

            deleted = {
                "market_microstructure_snapshot": 0,
                "order_book_summary": 0,
                "intraday_session_analytics": 0,
                "rolling_performance_cube": 0,
            }
            audit_event_id: str | None = None
            if args.apply:
                deleted = _delete_rows(session, window)
                audit_event = _audit_event(
                    window=window,
                    reason=args.reason,
                    before=before,
                    deleted=deleted,
                    manifest_path=args.manifest_path,
                )
                session.add(audit_event)
                session.flush()
                audit_event_id = str(audit_event.audit_event_id)

            after = _counts(session, window)
            return {
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "mode": "apply" if args.apply else "dry_run",
                "dry_run": bool(args.dry_run),
                "applied": bool(args.apply),
                "purge_reason": args.reason,
                "collection_date": window.collection_date.isoformat(),
                "session_type": window.session_type,
                "window_start_msk": window.window_start_msk.isoformat(),
                "cutoff_msk": window.cutoff_msk.isoformat(),
                "cutoff_utc": window.cutoff_utc.isoformat(),
                "before": before,
                "deleted": deleted,
                "after": after,
                "audit_event_id": audit_event_id,
                "manifest_path": args.manifest_path,
                "safety": {
                    "audit_events_deleted": 0,
                    "requires_explicit_apply": True,
                    "max_rows": args.max_rows,
                    "database_url_configured": bool(args.database_url),
                },
            }
    finally:
        database.engine.dispose()


def _parse_collection_date(value: str, session: Session) -> date:
    if value.upper() == "TODAY":
        return datetime.now(tz=MOSCOW_TZ).date()
    if value.lower() == "latest":
        latest = session.execute(
            select(func.max(MarketMicrostructureSnapshot.trading_date)).where(
                MarketMicrostructureSnapshot.source == "data_only_shadow"
            )
        ).scalar_one_or_none()
        if latest is None:
            msg = "No data-only shadow rows found for --date latest"
            raise SystemExit(msg)
        return latest
    return date.fromisoformat(value)


def _count_from(payload: CountsPayload, table: str, key: str) -> int:
    value = payload.get(table, {}).get(key, 0)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _purge_window(session: Session, collection_date: date) -> PurgeWindow:
    session_types = {
        value
        for value in session.execute(
            select(MarketMicrostructureSnapshot.session_type)
            .where(
                MarketMicrostructureSnapshot.trading_date == collection_date,
                MarketMicrostructureSnapshot.source == "data_only_shadow",
            )
            .distinct()
        ).scalars()
        if value
    }
    if not session_types:
        msg = f"No data-only shadow rows found for {collection_date.isoformat()}"
        raise SystemExit(msg)
    if session_types != {"weekend"}:
        msg = f"Refusing automatic purge for mixed/unsupported session types: {session_types}"
        raise SystemExit(msg)

    window_start_msk = datetime.combine(collection_date, time(10, 0), tzinfo=MOSCOW_TZ)
    cutoff_msk = datetime.combine(collection_date, time(19, 0), tzinfo=MOSCOW_TZ)
    day_end_msk = datetime.combine(
        collection_date + timedelta(days=1),
        time(0, 0),
        tzinfo=MOSCOW_TZ,
    )
    return PurgeWindow(
        collection_date=collection_date,
        session_type="weekend",
        window_start_msk=window_start_msk,
        cutoff_msk=cutoff_msk,
        window_start_utc=window_start_msk.astimezone(UTC),
        cutoff_utc=cutoff_msk.astimezone(UTC),
        day_end_utc=day_end_msk.astimezone(UTC),
    )


def _counts(session: Session, window: PurgeWindow) -> CountsPayload:
    late_rows = list(
        session.execute(
            select(MarketMicrostructureSnapshot).where(_invalid_microstructure_predicate(window))
        ).scalars()
    )
    by_instrument = Counter(str(row.instrument_id) for row in late_rows)
    calibration_allowed_true = sum(
        1 for row in late_rows if _payload_bool(row.snapshot_payload, "calibration_allowed") is True
    )
    include_true = sum(
        1
        for row in late_rows
        if _payload_bool(row.snapshot_payload, "include_in_calibration") is True
    )
    valid_rows = session.execute(
        select(func.count()).select_from(MarketMicrostructureSnapshot).where(
            MarketMicrostructureSnapshot.trading_date == window.collection_date,
            MarketMicrostructureSnapshot.source == "data_only_shadow",
            MarketMicrostructureSnapshot.ts_utc >= window.window_start_utc,
            MarketMicrostructureSnapshot.ts_utc < window.cutoff_utc,
        )
    ).scalar_one()
    order_book_late = session.execute(
        select(func.count()).select_from(OrderBookSummary).where(
            _invalid_order_book_predicate(window)
        )
    ).scalar_one()
    analytics_rows = session.execute(
        select(func.count()).select_from(IntradaySessionAnalytics).where(
            IntradaySessionAnalytics.trading_date == window.collection_date,
            IntradaySessionAnalytics.mode == "data_shadow",
        )
    ).scalar_one()
    rolling_rows = session.execute(
        select(func.count()).select_from(RollingPerformanceCube).where(
            RollingPerformanceCube.mode == "data_shadow",
            RollingPerformanceCube.window_start < window.day_end_utc,
            RollingPerformanceCube.window_end >= window.window_start_utc,
        )
    ).scalar_one()
    min_late_ts = min((row.ts_utc for row in late_rows), default=None)
    max_late_ts = max((row.ts_utc for row in late_rows), default=None)
    return {
        "market_microstructure_snapshot": {
            "valid_rows_before_cutoff": int(valid_rows),
            "late_rows": len(late_rows),
            "late_rows_by_instrument": dict(by_instrument),
            "late_rows_with_calibration_allowed_true": calibration_allowed_true,
            "late_rows_with_include_in_calibration_true": include_true,
            "sample_snapshot_ids": [str(row.snapshot_id) for row in late_rows[:10]],
            "min_late_ts": min_late_ts.isoformat() if min_late_ts is not None else None,
            "max_late_ts": max_late_ts.isoformat() if max_late_ts is not None else None,
        },
        "order_book_summary": {"late_rows": int(order_book_late)},
        "intraday_session_analytics": {"rows_for_rebuild": int(analytics_rows)},
        "rolling_performance_cube": {"rows_for_rebuild": int(rolling_rows)},
    }


def _delete_rows(session: Session, window: PurgeWindow) -> dict[str, int]:
    micro = _rowcount(
        session.execute(
            delete(MarketMicrostructureSnapshot).where(
                _invalid_microstructure_predicate(window)
            )
        )
    )
    order_books = _rowcount(
        session.execute(delete(OrderBookSummary).where(_invalid_order_book_predicate(window)))
    )
    intraday = _rowcount(
        session.execute(
            delete(IntradaySessionAnalytics).where(
                IntradaySessionAnalytics.trading_date == window.collection_date,
                IntradaySessionAnalytics.mode == "data_shadow",
            )
        )
    )
    rolling = _rowcount(
        session.execute(
            delete(RollingPerformanceCube).where(
                RollingPerformanceCube.mode == "data_shadow",
                RollingPerformanceCube.window_start < window.day_end_utc,
                RollingPerformanceCube.window_end >= window.window_start_utc,
            )
        )
    )
    return {
        "market_microstructure_snapshot": micro,
        "order_book_summary": order_books,
        "intraday_session_analytics": intraday,
        "rolling_performance_cube": rolling,
    }


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _invalid_microstructure_predicate(window: PurgeWindow) -> ColumnElement[bool]:
    return (
        (MarketMicrostructureSnapshot.trading_date == window.collection_date)
        & (MarketMicrostructureSnapshot.source == "data_only_shadow")
        & (MarketMicrostructureSnapshot.session_type == window.session_type)
        & (MarketMicrostructureSnapshot.session_phase == "continuous_trading")
        & (MarketMicrostructureSnapshot.ts_utc >= window.cutoff_utc)
        & (MarketMicrostructureSnapshot.ts_utc < window.day_end_utc)
    )


def _invalid_order_book_predicate(window: PurgeWindow) -> ColumnElement[bool]:
    return (
        (OrderBookSummary.trading_date == window.collection_date)
        & (OrderBookSummary.session_type == window.session_type)
        & (OrderBookSummary.session_phase == "continuous_trading")
        & (OrderBookSummary.ts_utc >= window.cutoff_utc)
        & (OrderBookSummary.ts_utc < window.day_end_utc)
    )


def _audit_event(
    *,
    window: PurgeWindow,
    reason: str,
    before: CountsPayload,
    deleted: dict[str, int],
    manifest_path: str | None,
) -> AuditEvent:
    now = datetime.now(tz=UTC)
    return AuditEvent(
        audit_event_id=uuid4(),
        calendar_date=window.collection_date,
        trading_date=window.collection_date,
        session_type=window.session_type,
        session_phase="closed",
        micro_session_id=f"{window.collection_date}:{window.session_type}:purge",
        broker_trading_status="unknown",
        ts_utc=now,
        exchange_ts=None,
        received_ts=now,
        service="maintenance",
        actor="run_purge_invalid_data_shadow_rows",
        action="data_only_invalid_rows_purged",
        entity_type="data_shadow_collection",
        entity_id=f"{window.collection_date}:{window.session_type}",
        severity="warning",
        correlation_id=str(uuid4()),
        audit_payload={
            "purge_reason": reason,
            "manifest_path": manifest_path,
            "collection_date": window.collection_date.isoformat(),
            "session_type": window.session_type,
            "cutoff_msk": window.cutoff_msk.isoformat(),
            "cutoff_utc": window.cutoff_utc.isoformat(),
            "before": before,
            "deleted": deleted,
            "audit_events_deleted": 0,
        },
    )


def _payload_bool(payload: object, key: str) -> bool | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    for nested in payload.values():
        if isinstance(nested, dict):
            nested_value = nested.get(key)
            if isinstance(nested_value, bool):
                return nested_value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD, TODAY, or latest")
    parser.add_argument("--reason", required=True, choices=sorted(VALID_REASONS))
    parser.add_argument("--database-url")
    parser.add_argument("--manifest-path")
    parser.add_argument("--max-rows", type=int, default=50_000)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()

"""Validate Dashboard Live Feed without starting data-only collection."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from sys import path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "api" / "src",
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService

CORE_TICKERS = ("SBER", "GAZP", "LKOH", "YDEX", "TATN", "GMKN", "OZON", "VTBR")
COUNT_TABLES = (
    "market_microstructure_snapshot",
    "signal_candidate",
    "order_intent",
    "broker_order",
    "order_state_event",
)


def main() -> None:
    args = parse_args()
    payload = run_acceptance(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    raise SystemExit(0 if payload["passed"] else 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default="http://localhost:8000")
    parser.add_argument("--frontend-url", default="http://localhost:5173")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--skip-frontend", action="store_true")
    return parser.parse_args()


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    api_base = args.api_base_url.rstrip("/")
    errors: list[str] = []
    warnings: list[str] = []
    database = DatabaseService(args.database_url or build_database_url_from_env())
    before_counts = table_counts(database)

    health = get_json(f"{api_base}/health", timeout_seconds=args.timeout_seconds)
    if health.get("status") not in {"ok", "OK", None} and not health.get("identity"):
        errors.append("api health did not return service identity")

    data_shadow = get_json(
        f"{api_base}/runtime/data-shadow/status",
        timeout_seconds=args.timeout_seconds,
    )
    if data_shadow.get("collector_state") == "collecting":
        warnings.append("data-only collector was already running before dashboard feed check")

    status = get_json(
        f"{api_base}/dashboard/market-feed/status",
        timeout_seconds=args.timeout_seconds,
    )
    snapshot = get_json(
        f"{api_base}/dashboard/market-feed/snapshot?{urllib.parse.urlencode({
            'selected_instrument': 'MOEX:SBER',
            'include_order_book': 'true',
            'include_trades': 'true',
        })}",
        timeout_seconds=args.timeout_seconds,
    )
    rows = snapshot.get("quote_rows")
    if not isinstance(rows, list):
        rows = []
        errors.append("dashboard feed quote_rows is not a list")
    tickers = {
        str(row.get("ticker") or str(row.get("instrument_id", "")).split(":")[-1])
        for row in rows
        if isinstance(row, dict)
    }
    missing = [ticker for ticker in CORE_TICKERS if ticker not in tickers]
    if len(rows) != 8:
        errors.append(f"dashboard feed returned {len(rows)} quote rows, expected 8")
    if missing:
        errors.append(f"dashboard feed missing tickers: {missing}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not row.get("quote_source"):
            errors.append(f"{row.get('instrument_id')} missing quote_source")
        if not row.get("quote_status"):
            errors.append(f"{row.get('instrument_id')} missing quote_status")

    selected = snapshot.get("selected_details")
    if not isinstance(selected, dict):
        selected = {}
        errors.append("selected_details is missing")
    if selected.get("instrument_id") != "MOEX:SBER":
        errors.append("selected_details did not default to MOEX:SBER")
    if not (selected.get("last_price") or selected.get("reason_code")):
        errors.append("selected SBER has neither price nor explicit reason")
    if not selected.get("market_trades_source"):
        errors.append("selected SBER missing explicit trade tape status")
    if not selected.get("order_book_source") and selected.get("display_market_quality_score"):
        errors.append("fake display quality score returned without order book")

    session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
    market_open = bool(session.get("market_open"))
    second_snapshot: dict[str, Any] | None = None
    if market_open:
        if not selected.get("last_price"):
            errors.append("market is open but selected SBER has no live/display price")
        if not selected.get("order_book_source"):
            errors.append("market is open but selected SBER has no order book source")
        if selected.get("order_book_stale"):
            errors.append("market is open but selected SBER order book is stale")
        time.sleep(5)
        second_snapshot = get_json(
            f"{api_base}/dashboard/market-feed/snapshot?{urllib.parse.urlencode({
                'selected_instrument': 'MOEX:SBER',
                'include_order_book': 'true',
                'include_trades': 'true',
            })}",
            timeout_seconds=args.timeout_seconds,
        )
        second_selected = (
            second_snapshot.get("selected_details")
            if isinstance(second_snapshot.get("selected_details"), dict)
            else {}
        )
        if second_selected.get("order_book_ts") == selected.get("order_book_ts"):
            errors.append("selected order book did not refresh within 10 seconds")
        age_ms = second_selected.get("order_book_age_ms")
        if isinstance(age_ms, int) and age_ms > 5_000:
            errors.append(f"selected order book age too high: {age_ms}ms")
    else:
        labelled_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and (row.get("last_price") or row.get("reason_code") or row.get("quote_payload"))
        ]
        if len(labelled_rows) != 8:
            errors.append("closed-market display mode lacks labels or fallback rows")

    frontend = (
        {"ok": True, "skipped": True}
        if args.skip_frontend
        else get_text(args.frontend_url.rstrip("/"), timeout_seconds=10)
    )
    if not frontend.get("ok"):
        warnings.append("frontend route is not reachable")

    after_counts = table_counts(database)
    deltas = {
        table: after_counts.get(table, 0) - before_counts.get(table, 0)
        for table in COUNT_TABLES
    }
    if deltas.get("market_microstructure_snapshot", 0) != 0:
        errors.append("dashboard feed wrote market_microstructure_snapshot")
    for table in ("signal_candidate", "order_intent", "broker_order", "order_state_event"):
        if deltas.get(table, 0) != 0:
            errors.append(f"dashboard feed changed {table}")

    passed = not errors
    return {
        "passed": passed,
        "market_open": market_open,
        "dashboard_feed_status": status,
        "quote_rows": len(rows),
        "selected_instrument": selected.get("instrument_id"),
        "selected_price_present": bool(selected.get("last_price")),
        "order_book_source": selected.get("order_book_source"),
        "trade_tape_status": selected.get("market_trades_source"),
        "data_only_collector_state": data_shadow.get("collector_state"),
        "db_deltas": deltas,
        "post_order_calls": 0,
        "cancel_order_calls": 0,
        "second_snapshot_checked": second_snapshot is not None,
        "frontend_ok": bool(frontend.get("ok")),
        "errors": errors,
        "warnings": warnings,
    }


def table_counts(database: DatabaseService) -> dict[str, int]:
    counts: dict[str, int] = {}
    with database.session_scope() as session:
        for table in COUNT_TABLES:
            try:
                counts[table] = int(
                    session.execute(text(f"select count(*) from {table}")).scalar() or 0
                )
            except Exception:
                counts[table] = 0
    return counts


def get_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    request.add_header("X-API-Role", "observer")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"error": f"{exc.code} {exc.reason}", "body": body}
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def get_text(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    try:
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response.read(512)
            return {"ok": 200 <= response.status < 500, "status": response.status}
    except (OSError, TimeoutError) as exc:
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


if __name__ == "__main__":
    main()

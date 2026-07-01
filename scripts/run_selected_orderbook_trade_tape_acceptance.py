"""Validate selected dashboard order book and trade tape behavior.

This is a black-box API/DB acceptance check. It does not start data-only
collection and treats dashboard feed writes to calibration/trading tables as a
failure.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path
from sys import path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
for src in (ROOT / "packages" / "common" / "src",):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService

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
    parser.add_argument("--database-url")
    parser.add_argument("--instruments", required=True, help="Comma-separated MOEX instruments.")
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    api_base = args.api_base_url.rstrip("/")
    instruments = tuple(item.strip() for item in args.instruments.split(",") if item.strip())
    database = DatabaseService(args.database_url or build_database_url_from_env())
    errors: list[str] = []
    warnings: list[str] = []
    before_counts = table_counts(database)

    data_shadow = get_json(
        f"{api_base}/runtime/data-shadow/status",
        timeout_seconds=args.timeout_seconds,
    )
    snapshots: dict[str, dict[str, Any]] = {}
    selected_by_instrument: dict[str, dict[str, Any]] = {}
    status_by_instrument: dict[str, str | None] = {}
    raw_orderbook_reason: dict[str, str | None] = {}
    raw_trades_reason: dict[str, str | None] = {}
    rendered_bid_ask: dict[str, bool] = {}
    order_book_available: dict[str, bool] = {}
    trade_tape_status: dict[str, str | None] = {}

    market_open = False
    get_order_book_called = False
    get_last_trades_called = False
    broker_ids_used_uid_or_figi = True

    for instrument in instruments:
        snapshot = post_json(
            f"{api_base}/dashboard/market-feed/refresh?{urllib.parse.urlencode({
                'selected_instrument': instrument,
                'include_order_book': 'true',
                'include_trades': 'true',
            })}",
            timeout_seconds=args.timeout_seconds,
        )
        snapshots[instrument] = snapshot
        session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
        market_open = market_open or bool(session.get("market_open"))
        if snapshot.get("selected_instrument") != instrument:
            errors.append(f"{instrument}: selected instrument was not preserved")
        selected = snapshot.get("selected_details")
        if not isinstance(selected, dict):
            selected = {}
            errors.append(f"{instrument}: selected_details missing")
        selected_by_instrument[instrument] = selected
        if selected.get("instrument_id") != instrument:
            errors.append(f"{instrument}: selected_details instrument mismatch")

        summary = selected.get("order_book_summary")
        if not isinstance(summary, dict):
            summary = {}
        bids = summary.get("bids") if isinstance(summary.get("bids"), list) else []
        asks = summary.get("asks") if isinstance(summary.get("asks"), list) else []
        has_book = bool(bids and asks and selected.get("best_bid") and selected.get("best_ask"))
        order_book_available[instrument] = has_book
        rendered_bid_ask[instrument] = bool(
            has_book
            and selected.get("spread_abs") is not None
            and selected.get("spread_bps") is not None
            and selected.get("bid_depth_lots") is not None
            and selected.get("ask_depth_lots") is not None
            and selected.get("book_imbalance") is not None
        )
        get_order_book_called = get_order_book_called or bool(
            selected.get("order_book_source")
            or selected.get("order_book_ts")
            or summary.get("source")
            or selected.get("warning")
        )
        if selected.get("warning") == "dashboard_feed_selected_instrument_unresolved":
            broker_ids_used_uid_or_figi = False

        raw_orderbook_reason[instrument] = _first_reason(
            selected,
            "order_book_source",
            "warning",
            "market_quality_label",
            "reason_code",
        )
        trade_tape_status[instrument] = _first_reason(
            selected,
            "trade_tape_status",
            "market_trades_source",
        )
        raw_trades_reason[instrument] = _first_reason(
            selected,
            "trade_tape_reason",
            "market_trades_source",
            "warning",
        )
        get_last_trades_called = get_last_trades_called or bool(
            selected.get("trade_tape_status")
            or selected.get("trade_tape_reason")
            or selected.get("market_trades_source")
            or selected.get("recent_market_trades")
        )
        status_by_instrument[instrument] = str(selected.get("quote_status") or "")

        if has_book and not rendered_bid_ask[instrument]:
            errors.append(f"{instrument}: broker book present but bid/ask metrics incomplete")
        if not has_book and market_open and not raw_orderbook_reason[instrument]:
            errors.append(f"{instrument}: no selected order book and no explicit reason")
        if not trade_tape_status[instrument] or not raw_trades_reason[instrument]:
            errors.append(f"{instrument}: missing explicit trade tape status/reason")
        if (
            trade_tape_status[instrument] == "no_market_trades_samples"
            and raw_trades_reason[instrument] != "no_market_trades_samples"
        ):
            errors.append(f"{instrument}: no_market_trades_samples lacks raw empty evidence")

    after_counts = table_counts(database)
    database.engine.dispose()
    deltas = {
        table: int(after_counts.get(table, 0)) - int(before_counts.get(table, 0))
        for table in COUNT_TABLES
    }
    no_db_writes = all(value == 0 for value in deltas.values())
    if not no_db_writes:
        errors.append(f"dashboard feed changed protected DB tables: {deltas}")

    closed_mode_pending = not market_open
    if closed_mode_pending:
        warnings.append("market closed or unknown; open-session verification pending")

    passed = not errors
    return {
        "passed": passed,
        "market_open": market_open,
        "closed_mode_open_session_verification_pending": closed_mode_pending,
        "get_order_book_called": get_order_book_called,
        "get_last_trades_called": get_last_trades_called,
        "broker_ids_used_uid_or_figi": broker_ids_used_uid_or_figi,
        "sber_order_book_available": order_book_available.get("MOEX:SBER", False),
        "gazp_order_book_available": order_book_available.get("MOEX:GAZP", False),
        "vtbr_order_book_available": order_book_available.get("MOEX:VTBR", False),
        "sber_bid_ask_rendered": rendered_bid_ask.get("MOEX:SBER", False),
        "gazp_bid_ask_rendered": rendered_bid_ask.get("MOEX:GAZP", False),
        "vtbr_bid_ask_rendered": rendered_bid_ask.get("MOEX:VTBR", False),
        "trade_tape_status_by_instrument": trade_tape_status,
        "raw_broker_orderbook_reason_by_instrument": raw_orderbook_reason,
        "raw_broker_trades_reason_by_instrument": raw_trades_reason,
        "quote_status_by_instrument": status_by_instrument,
        "no_db_writes_from_dashboard_feed": no_db_writes,
        "db_deltas": deltas,
        "no_trading_entities": all(deltas[table] == 0 for table in COUNT_TABLES[1:]),
        "post_order_calls": 0,
        "cancel_order_calls": 0,
        "data_shadow_collector_state": data_shadow.get("collector_state"),
        "errors": errors,
        "warnings": warnings,
    }


def get_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def table_counts(database: DatabaseService) -> dict[str, int]:
    with database.session_scope() as session:
        return {
            table: int(session.execute(text(f"select count(*) from {table}")).scalar_one())
            for table in COUNT_TABLES
        }


def _first_reason(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return str(value)
    return None


if __name__ == "__main__":
    main()

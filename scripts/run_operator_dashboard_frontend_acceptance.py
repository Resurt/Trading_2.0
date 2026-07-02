"""Validate operator dashboard read-model endpoints without starting collection."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

CORE_TICKERS = ("SBER", "GAZP", "LKOH", "YDEX", "TATN", "GMKN", "OZON", "VTBR", "T")
CORE_CSV = ",".join(CORE_TICKERS)

REASON_LABELS = {
    "market_open": "рынок открыт",
    "no_trading_window": "нет торгового окна",
    "broker_status_unavailable": "статус инструмента недоступен",
    "market_closed_expected": "рынок закрыт по расписанию",
    "official_exchange_closed": "биржа закрыта",
    "data_only_collection_stopped": "data-only сбор остановлен",
    "no_market_trades_samples": "лента сделок не пришла",
    "instrument_unavailable": "инструмент недоступен",
    "broker_quote_not_for_calibration": "брокерская котировка только для отображения",
    "stale_order_book": "стакан устарел",
    "no_order_book_samples": "нет samples стакана",
    "preflight_unavailable": "preflight недоступен",
    "data_only_collection_allowed": "data-only сбор разрешён",
    "data_only_collection_blocked": "data-only сбор заблокирован",
}


def main() -> int:
    args = parse_args()
    result = run_acceptance(args)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json_output else None))
    return 0 if result["passed"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default="http://localhost:8000")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    return parser.parse_args()


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.api_base_url.rstrip("/")
    errors: list[str] = []
    warnings: list[str] = []

    overview = get_json(
        f"{base_url}/market/overview?{urllib.parse.urlencode({'include_details': 'false'})}",
        timeout_seconds=args.timeout_seconds,
    )
    rows = overview.get("instruments", []) if isinstance(overview, dict) else []
    if not isinstance(rows, list):
        rows = []
        errors.append("market overview instruments is not a list")

    tickers = {
        str(row.get("ticker") or str(row.get("instrument_id", "")).split(":")[-1]) for row in rows
    }
    missing_tickers = [ticker for ticker in CORE_TICKERS if ticker not in tickers]
    if len(rows) != len(CORE_TICKERS):
        errors.append(f"market overview returned {len(rows)} rows, expected {len(CORE_TICKERS)}")
    if missing_tickers:
        errors.append(f"market overview missing core tickers: {missing_tickers}")

    sber_row = next((row for row in rows if row.get("instrument_id") == "MOEX:SBER"), None)
    if not isinstance(sber_row, dict):
        errors.append("SBER row is not selectable")
        sber_row = {}
    validate_quote_row(sber_row, errors)

    details = get_json(
        f"{base_url}/market/instruments/{urllib.parse.quote('MOEX:SBER', safe='')}/details",
        timeout_seconds=args.timeout_seconds,
    )
    if details.get("instrument_id") != "MOEX:SBER":
        errors.append("selected instrument details did not return MOEX:SBER")
    validate_details(details, errors, warnings)

    preflight_query = urllib.parse.urlencode(
        {"instruments": CORE_CSV, "mode": "data_shadow", "cache": "false"}
    )
    preflight = get_json(
        f"{base_url}/session/preflight?{preflight_query}",
        timeout_seconds=args.timeout_seconds,
    )
    human_state = preflight_human_state(preflight)
    if not human_state.get("session_type"):
        errors.append("preflight missing session_type")
    if not human_state.get("session_phase"):
        errors.append("preflight missing session_phase")
    if human_state.get("reason_label") == human_state.get("reason_code"):
        warnings.append(f"unmapped preflight reason: {human_state.get('reason_code')}")

    passed = not errors
    return {
        "passed": passed,
        "overview_rows": len(rows),
        "sber_selectable": bool(sber_row),
        "details_loaded": details.get("instrument_id") == "MOEX:SBER",
        "quote_sources": sorted(
            {str(row.get("quote_source")) for row in rows if isinstance(row, dict)}
        ),
        "freshness_states": sorted(
            {str(row.get("quote_status")) for row in rows if isinstance(row, dict)}
        ),
        "preflight_human_state": human_state,
        "trade_tape_source": details.get("market_trades_source"),
        "errors": errors,
        "warnings": warnings,
    }


def validate_quote_row(row: dict[str, Any], errors: list[str]) -> None:
    if not row:
        return
    if not row.get("quote_source"):
        errors.append("SBER row missing quote_source")
    if not row.get("quote_status"):
        errors.append("SBER row missing quote_status/freshness")
    if row.get("last_price") in (None, "") and not (
        row.get("reason_code") or row.get("quote_payload", {}).get("reason_code")
    ):
        errors.append("SBER row has no price and no unavailable reason")
    if row.get("official_exchange_closed") is True and row.get("quote_source") in {
        "live_order_book_mid",
        "live_exchange_order_book",
        "live_exchange_last_price",
    }:
        errors.append("closed market quote is labelled as live exchange")


def validate_details(
    details: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    if not details:
        errors.append("selected instrument details response is empty")
        return
    for field in ("quote_source", "quote_status", "order_book_stale", "market_trades_source"):
        if field not in details:
            errors.append(f"selected details missing {field}")
    no_trades = not details.get("recent_market_trades")
    no_samples_marker = details.get("market_trades_source") == "no_market_trades_samples"
    if no_trades and not no_samples_marker:
        warnings.append("selected details has no trades without no_market_trades_samples marker")
    has_quality_without_book = not details.get("order_book_source") and details.get(
        "display_market_quality_score"
    ) not in (None, "")
    if has_quality_without_book:
        errors.append("details exposes display quality score without a real order book")


def preflight_human_state(preflight: dict[str, Any]) -> dict[str, Any]:
    reason_code = str(preflight.get("reason_code") or "preflight_unavailable")
    return {
        "market_open": preflight.get("market_open"),
        "data_only_collection_allowed": preflight.get("data_only_collection_allowed"),
        "session_type": preflight.get("session_type"),
        "session_phase": preflight.get("session_phase"),
        "reason_code": reason_code,
        "reason_label": REASON_LABELS.get(reason_code, reason_code),
        "next_session_at": preflight.get("next_session_at"),
    }


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


if __name__ == "__main__":
    raise SystemExit(main())

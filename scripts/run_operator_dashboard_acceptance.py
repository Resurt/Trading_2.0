"""Capture and validate Live Dashboard API truth for operator acceptance."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / ".local" / "collection_reports" / "operator_dashboard_acceptance"
CORE_TICKERS = ("SBER", "GAZP", "LKOH", "YDEX", "TATN", "GMKN", "OZON", "VTBR", "T")


@dataclass(frozen=True)
class Endpoint:
    filename: str
    method: str
    path: str
    role: str = "observer"


def request_json(base_url: str, endpoint: Endpoint, timeout_seconds: float) -> dict[str, Any]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", endpoint.path.lstrip("/"))
    request = urllib.request.Request(url, method=endpoint.method)
    request.add_header("X-API-Role", endpoint.role)
    if endpoint.method == "POST":
        request.add_header("Content-Type", "application/json")
        request.data = b"{}"
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"error": f"{exc.code} {exc.reason}", "body": body}
    except Exception as exc:  # noqa: BLE001 - command-line diagnostic should capture all failures.
        return {"error": type(exc).__name__, "message": str(exc)}


def write_snapshot(out_dir: Path, endpoint: Endpoint, payload: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / endpoint.filename).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def has_error(payload: dict[str, Any]) -> bool:
    return "error" in payload


def contains_exact_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(contains_exact_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_exact_key(item, key) for item in value)
    return False


def validate(
    snapshots: dict[str, dict[str, Any]],
) -> tuple[bool, list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []

    portfolio = snapshots["10_portfolio_summary_after.json"]
    balance = portfolio.get("balance", {}) if isinstance(portfolio.get("balance"), dict) else {}
    if balance.get("balance_degraded") is True:
        if not balance.get("balance_degraded_reason_code"):
            errors.append("portfolio degraded without explicit reason")
    elif not (balance.get("total_portfolio_value_rub") or balance.get("available_cash_rub")):
        errors.append("portfolio has neither total value nor available cash")
    if contains_exact_key(portfolio, "account_id"):
        errors.append("portfolio payload exposes unmasked account_id field")

    market = snapshots["11_market_overview_after.json"]
    rows = market.get("instruments", [])
    if not isinstance(rows, list):
        errors.append("market overview instruments is not a list")
        rows = []
    tickers = {
        str(row.get("ticker") or row.get("instrument_id", "").split(":")[-1]) for row in rows
    }
    if tuple(sorted(tickers & set(CORE_TICKERS))) != tuple(sorted(CORE_TICKERS)):
        errors.append(f"market overview does not include all core tickers: {sorted(tickers)}")
    if len(rows) != len(CORE_TICKERS):
        errors.append(f"market overview returned {len(rows)} rows, expected {len(CORE_TICKERS)}")

    live_rows = 0
    broker_display_rows = 0
    stale_rows = 0
    for row in rows:
        instrument_id = row.get("instrument_id")
        source = row.get("quote_source") or row.get("last_price_source")
        stale = row.get("is_price_stale")
        price = row.get("last_price")
        if not price and not row.get("quote_payload", {}).get("reason_code"):
            errors.append(f"{instrument_id}: no price and no unavailable reason")
        if row.get("official_exchange_closed") is True:
            if source in {
                "live_order_book_mid",
                "live_exchange_order_book",
                "live_exchange_last_price",
            }:
                errors.append(f"{instrument_id}: official closed row is labelled live exchange")
            if row.get("quote_allowed_for_data_collection") is True:
                errors.append(f"{instrument_id}: official closed row allowed for data collection")
        if source in {"live_exchange_order_book", "live_exchange_last_price"}:
            live_rows += 1
            if stale is not False:
                errors.append(f"{instrument_id}: live exchange quote is stale")
            for field in ("best_bid", "best_ask", "mid_price", "spread_bps"):
                if row.get(field) is None:
                    errors.append(f"{instrument_id}: live row missing {field}")
        if source in {
            "broker_quote_exchange_closed",
            "broker_otc_order_book",
            "broker_indicative_quote",
        }:
            broker_display_rows += 1
            if row.get("quote_allowed_for_data_collection") is True:
                errors.append(f"{instrument_id}: broker/indicative row allowed for calibration")
        if source == "latest_market_candle_close":
            stale_rows += 1
            if stale is not True:
                errors.append(f"{instrument_id}: latest candle fallback is not marked stale")
    if not live_rows:
        warnings.append(
            "no live quote rows after explicit refresh; dashboard should show stale/source badges"
        )

    status = snapshots["12_data_shadow_status_after.json"]
    if status.get("real_orders_disabled") is not True:
        errors.append("data-shadow status does not confirm real_orders_disabled=true")
    if status.get("strategy_trading_disabled") is not True:
        errors.append("data-shadow status does not confirm strategy_trading_disabled=true")
    if not status.get("collector_state"):
        errors.append("data-shadow status missing collector_state")

    for filename, payload in snapshots.items():
        if has_error(payload):
            errors.append(f"{filename}: {payload.get('error')}")

    summary = {
        "market_rows": len(rows),
        "live_rows": live_rows,
        "broker_display_rows": broker_display_rows,
        "stale_rows": stale_rows,
        "balance_degraded": balance.get("balance_degraded"),
        "collector_state": status.get("collector_state"),
        "warnings": warnings,
    }
    return not errors, errors, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--json-output", action="store_true")
    args = parser.parse_args()

    endpoints = (
        Endpoint("01_robot_status.json", "GET", "/robot/status"),
        Endpoint("02_portfolio_summary_before.json", "GET", "/portfolio/summary"),
        Endpoint(
            "03_session_preflight.json",
            "GET",
            "/session/preflight?instruments=SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T&mode=data_shadow",
        ),
        Endpoint("04_data_shadow_status_before.json", "GET", "/runtime/data-shadow/status"),
        Endpoint("05_market_overview_before.json", "GET", "/market/overview"),
        Endpoint("06_microstructure_latest.json", "GET", "/market/microstructure/latest?limit=20"),
        Endpoint(
            "07_microstructure_summary.json",
            "GET",
            "/market/microstructure/summary?lookback_minutes=60",
        ),
        Endpoint("08_portfolio_refresh.json", "POST", "/portfolio/refresh", role="operator"),
        Endpoint("09_quotes_refresh.json", "POST", "/market/quotes/refresh"),
        Endpoint("10_portfolio_summary_after.json", "GET", "/portfolio/summary"),
        Endpoint("11_market_overview_after.json", "GET", "/market/overview"),
        Endpoint("12_data_shadow_status_after.json", "GET", "/runtime/data-shadow/status"),
    )
    snapshots: dict[str, dict[str, Any]] = {}
    for endpoint in endpoints:
        payload = request_json(args.base_url, endpoint, args.timeout_seconds)
        snapshots[endpoint.filename] = payload
        write_snapshot(args.out_dir, endpoint, payload)

    passed, errors, summary = validate(snapshots)
    result = {
        "passed": passed,
        "out_dir": str(args.out_dir),
        "errors": errors,
        **summary,
    }
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Operator dashboard acceptance:", "passed" if passed else "failed")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

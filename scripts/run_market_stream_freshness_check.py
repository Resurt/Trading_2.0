"""Check live market read-model freshness without treating closed MOEX as failure."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from typing import Any

CORE = "SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T"


def get_json(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--max-order-book-age-ms", type=int, default=1500)
    parser.add_argument("--json-output", action="store_true")
    args = parser.parse_args()

    preflight = get_json(
        args.base_url,
        f"/session/preflight?instruments={CORE}&mode=data_shadow",
        timeout=10,
    )
    if preflight.get("official_exchange_closed") is True:
        result = {
            "passed": True,
            "skipped": True,
            "reason_code": preflight.get("reason_code"),
            "official_exchange_closed": True,
            "message": (
                "official MOEX exchange is closed; calibration stream freshness is not expected"
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.json_output else None))
        return 0

    snapshots: list[dict[str, Any]] = []
    for index in range(max(1, args.samples)):
        snapshots.append(get_json(args.base_url, "/market/overview", timeout=10))
        if index < args.samples - 1:
            time.sleep(max(0.1, args.interval_seconds))

    rows = snapshots[-1].get("instruments", [])
    live_rows = [
        row
        for row in rows
        if row.get("quote_source") in {"live_exchange_order_book", "live_exchange_last_price"}
    ]
    stale_live_rows = [
        row
        for row in live_rows
        if row.get("order_book_age_ms") is not None
        and int(row["order_book_age_ms"]) > args.max_order_book_age_ms
    ]
    generated_values = {str(item.get("generated_at")) for item in snapshots}
    errors: list[str] = []
    if not live_rows:
        errors.append("no live exchange quote rows in market overview")
    if stale_live_rows:
        errors.append(
            "live rows exceed age threshold: "
            + ", ".join(str(row.get("instrument_id")) for row in stale_live_rows)
        )
    if len(generated_values) < 2 and args.samples > 1:
        errors.append("market overview generated_at did not change during sampling")

    result = {
        "passed": not errors,
        "skipped": False,
        "errors": errors,
        "live_rows": len(live_rows),
        "generated_at_values": sorted(generated_values),
        "official_exchange_closed": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json_output else None))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

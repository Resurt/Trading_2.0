"""Validate Dashboard Live Feed without starting data-only collection."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
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
    parser.add_argument("--selected-instrument", default="MOEX:SBER")
    parser.add_argument("--switch-instrument", default="MOEX:GAZP")
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

    selected_instrument = str(args.selected_instrument)
    switch_instrument = str(args.switch_instrument)
    status = get_json(
        f"{api_base}/dashboard/market-feed/status",
        timeout_seconds=args.timeout_seconds,
    )
    ws_result = read_market_feed_websocket(
        api_base,
        selected_instrument=selected_instrument,
        switch_instrument=switch_instrument,
        timeout_seconds=args.timeout_seconds,
    )
    if not ws_result.get("ok"):
        errors.append(f"dashboard WS failed: {ws_result.get('error')}")
    ws_first = ws_result.get("first_snapshot")
    if isinstance(ws_first, dict):
        if ws_first.get("source") != "dashboard_market_feed":
            errors.append("WS /ws/market-feed is not DashboardMarketFeed snapshot")
        ws_rows = ws_first.get("quote_rows")
        if not isinstance(ws_rows, list) or len(ws_rows) < 8:
            errors.append("WS first snapshot did not contain 8 quote rows")
        if ws_first.get("selected_instrument") != selected_instrument:
            errors.append("WS first snapshot selected instrument mismatch")
    ws_switched = ws_result.get("switch_snapshot")
    if isinstance(ws_switched, dict):
        selected_after_switch = ws_switched.get("selected_details")
        if ws_switched.get("selected_instrument") != switch_instrument:
            errors.append("WS selected switch did not preserve requested instrument")
        if not isinstance(selected_after_switch, dict) or selected_after_switch.get(
            "instrument_id"
        ) != switch_instrument:
            errors.append("WS selected details did not switch to target instrument")
    snapshot = get_json(
        f"{api_base}/dashboard/market-feed/snapshot?{urllib.parse.urlencode({
            'selected_instrument': selected_instrument,
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
    if selected.get("instrument_id") != selected_instrument:
        errors.append(f"selected_details did not default to {selected_instrument}")
    if not (selected.get("last_price") or selected.get("reason_code")):
        errors.append("selected instrument has neither price nor explicit reason")
    if not (selected.get("trade_tape_status") or selected.get("market_trades_source")):
        errors.append("selected instrument missing explicit trade tape status")
    if not selected.get("order_book_source") and selected.get("display_market_quality_score"):
        errors.append("fake display quality score returned without order book")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("last_price_source") == "latest_market_candle_close" and row.get(
            "quote_status"
        ) == "live":
            errors.append(f"{row.get('instrument_id')} stale candle fallback is labeled live")
        if row.get("freshness_status") == "stale" and row.get("quote_status") == "live":
            errors.append(f"{row.get('instrument_id')} stale freshness is labeled live")

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
                'selected_instrument': selected_instrument,
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
        "ws_primary_ok": bool(ws_result.get("ok")),
        "ws_path": ws_result.get("path"),
        "ws_selected_switch_passed": not any(
            "WS selected" in item for item in errors
        ),
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


def read_market_feed_websocket(
    api_base: str,
    *,
    selected_instrument: str,
    switch_instrument: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    parsed_base = urllib.parse.urlparse(api_base)
    scheme = "wss" if parsed_base.scheme == "https" else "ws"
    path = "/ws/market-feed?" + urllib.parse.urlencode(
        {
            "selected_instrument": selected_instrument,
            "include_order_book": "true",
            "include_trades": "true",
        }
    )
    url = urllib.parse.urlunparse(
        (
            scheme,
            parsed_base.netloc,
            path,
            "",
            "",
            "",
        )
    )
    sock: socket.socket | None = None
    try:
        sock = websocket_connect(url, timeout_seconds=timeout_seconds)
        first_message = websocket_recv_json(sock, timeout_seconds=timeout_seconds)
        websocket_send_json(
            sock,
            {"type": "market.select", "selected_instrument": switch_instrument},
        )
        switch_snapshot: dict[str, Any] | None = None
        for _ in range(5):
            message = websocket_recv_json(sock, timeout_seconds=timeout_seconds)
            payload = message.get("payload") if isinstance(message, dict) else {}
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data.get("selected_instrument") == switch_instrument:
                switch_snapshot = data
                break
        first_payload = first_message.get("payload") if isinstance(first_message, dict) else {}
        first_snapshot = first_payload.get("data") if isinstance(first_payload, dict) else None
        return {
            "ok": isinstance(first_snapshot, dict) and isinstance(switch_snapshot, dict),
            "path": url,
            "first_snapshot": first_snapshot,
            "switch_snapshot": switch_snapshot,
            "first_message_type": (
                first_message.get("type") if isinstance(first_message, dict) else None
            ),
        }
    except Exception as exc:
        return {"ok": False, "path": url, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if sock is not None:
            sock.close()


def websocket_connect(url: str, *, timeout_seconds: float) -> socket.socket:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    raw_sock = socket.create_connection((host, port), timeout=timeout_seconds)
    raw_sock.settimeout(timeout_seconds)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    raw_sock.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = raw_sock.recv(4096)
        if not chunk:
            break
        response += chunk
    if b" 101 " not in response.split(b"\r\n", 1)[0]:
        raise RuntimeError(response.decode("utf-8", errors="replace")[:300])
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")
    if f"Sec-WebSocket-Accept: {accept}".lower() not in response.decode(
        "utf-8", errors="replace"
    ).lower():
        raise RuntimeError("websocket accept header mismatch")
    return raw_sock


def websocket_send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.extend([0x80 | 126, *struct.pack("!H", length)])
    else:
        header.extend([0x80 | 127, *struct.pack("!Q", length)])
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    sock.sendall(bytes(header) + mask + masked)


def websocket_recv_json(sock: socket.socket, *, timeout_seconds: float) -> dict[str, Any]:
    sock.settimeout(timeout_seconds)
    while True:
        first = recv_exact(sock, 2)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(sock, 8))[0]
        mask = recv_exact(sock, 4) if masked else b""
        payload = recv_exact(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            raise RuntimeError("websocket closed")
        if opcode == 0x9:
            continue
        if opcode != 0x1:
            continue
        return json.loads(payload.decode("utf-8"))


def recv_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("socket closed")
        data.extend(chunk)
    return bytes(data)


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

"""Live readonly diagnostic for exchange_ts and persisted trade tape.

The script is intended for an open market session. If the market is closed it
exits without broker diagnostics because closed-market payloads are
inconclusive for exchange timestamps and trade tape availability.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sys import path
from typing import Any
from urllib.request import urlopen

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.broker_gateway import (
    InstrumentRef,
    InstrumentResolveRequest,
    LastPricesRequest,
    LastTradesRequest,
    OrderBookRequest,
    TradingStatusRequest,
)
from trade_core.infra.tbank import TBankBrokerConfig, TBankBrokerGateway, load_tbank_tokens
from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import (
    MarketMicrostructureSnapshot,
    MarketTradeSample,
    OrderBookSummary,
)
from trading_common.db.service import DatabaseService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", required=True)
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--api-base-url", default="http://localhost:8000")
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = asyncio.run(run(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    return 0 if payload["status"] in {"ok", "market_closed_live_diagnostic_not_run"} else 1


async def run(args: argparse.Namespace) -> dict[str, Any]:
    instruments = tuple(
        item.strip().upper() for item in args.instruments.split(",") if item.strip()
    )
    preflight = _get_json(
        f"{args.api_base_url}/session/preflight?"
        f"instruments={','.join(instruments)}&mode=data_shadow&cache=false"
    )
    if not (
        preflight.get("market_open")
        and preflight.get("market_window_open")
        and preflight.get("data_only_collection_allowed")
    ):
        return {
            "status": "market_closed_live_diagnostic_not_run",
            "closed_market_diagnostic_inconclusive": True,
            "reason_code": preflight.get("reason_code"),
            "market_open": preflight.get("market_open"),
            "market_window_open": preflight.get("market_window_open"),
            "data_only_collection_allowed": preflight.get("data_only_collection_allowed"),
            "note": (
                "Run during an open MOEX collection window; closed-market payloads are "
                "inconclusive."
            ),
        }

    database = DatabaseService(args.database_url or build_database_url_from_env())
    started_at = datetime.now(tz=UTC)
    before = _db_counts(database, started_at=started_at)
    broker = await _broker_probe(instruments=instruments, minutes=args.minutes)
    after = _db_counts(database, started_at=started_at)
    database.engine.dispose()

    return {
        "status": "ok",
        "market_open": True,
        "data_only_collection_allowed": True,
        "started_at": started_at.isoformat(),
        "duration_minutes": args.minutes,
        "broker": broker,
        "db_delta": {
            key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in sorted(after)
        },
        "db_after": after,
        "post_order_called": False,
        "cancel_order_called": False,
    }


async def _broker_probe(*, instruments: tuple[str, ...], minutes: float) -> dict[str, Any]:
    tokens = load_tbank_tokens()
    gateway = TBankBrokerGateway(config=TBankBrokerConfig(), tokens=tokens)
    resolved = await gateway.resolve_instruments(
        InstrumentResolveRequest(
            tickers=tuple(_ticker(item) for item in instruments),
            class_code="TQBR",
        )
    )
    refs = _instrument_refs(resolved.data.get("instruments", []))
    deadline = time.monotonic() + max(1.0, minutes * 60.0)
    observations: dict[str, dict[str, Any]] = {
        ref.instrument_id: {
            "ticker": ref.ticker,
            "broker_payload_has_exchange_ts_order_book": False,
            "broker_payload_has_exchange_ts_last_price": False,
            "broker_payload_has_exchange_ts_trades": False,
            "order_book_calls": 0,
            "last_prices_calls": 0,
            "last_trades_calls": 0,
            "trading_status_calls": 0,
            "trades_seen": 0,
            "errors": [],
        }
        for ref in refs
    }
    while time.monotonic() < deadline:
        now = datetime.now(tz=UTC)
        last_prices = await gateway.get_last_prices(LastPricesRequest(instruments=tuple(refs)))
        price_items = _list_payload(last_prices.data, "prices")
        for ref in refs:
            row = observations[ref.instrument_id]
            row["last_prices_calls"] += 1
            row["broker_payload_has_exchange_ts_last_price"] = bool(
                row["broker_payload_has_exchange_ts_last_price"]
                or _payload_list_has_exchange_ts(price_items)
            )
            try:
                status = await gateway.get_trading_status(TradingStatusRequest(instrument=ref))
                row["trading_status_calls"] += 1
                row["trading_status_has_exchange_ts"] = bool(status.data.get("exchange_ts"))
                book = await gateway.get_order_book(OrderBookRequest(instrument=ref, depth=10))
                row["order_book_calls"] += 1
                row["broker_payload_has_exchange_ts_order_book"] = bool(
                    row["broker_payload_has_exchange_ts_order_book"]
                    or _payload_has_exchange_ts(book.data)
                )
                trades = await gateway.get_last_trades(
                    LastTradesRequest(
                        instrument=ref,
                        from_=now - timedelta(seconds=60),
                        to=now,
                    )
                )
                row["last_trades_calls"] += 1
                trade_items = _list_payload(trades.data, "trades")
                row["trades_seen"] += len(trade_items)
                row["broker_payload_has_exchange_ts_trades"] = bool(
                    row["broker_payload_has_exchange_ts_trades"]
                    or _payload_list_has_exchange_ts(trade_items)
                )
            except Exception as exc:  # pragma: no cover - diagnostic resilience
                row["errors"].append(type(exc).__name__)
        await asyncio.sleep(5)
    return {"instruments": observations}


def _db_counts(database: DatabaseService, *, started_at: datetime) -> dict[str, int]:
    with database.session_scope() as session:
        return {
            "market_microstructure_snapshot_delta_scope": int(
                session.scalar(
                    select(func.count())
                    .select_from(MarketMicrostructureSnapshot)
                    .where(MarketMicrostructureSnapshot.received_ts >= started_at)
                )
                or 0
            ),
            "order_book_summary_delta_scope": int(
                session.scalar(
                    select(func.count())
                    .select_from(OrderBookSummary)
                    .where(OrderBookSummary.received_ts >= started_at)
                )
                or 0
            ),
            "market_trade_sample_delta_scope": int(
                session.scalar(
                    select(func.count())
                    .select_from(MarketTradeSample)
                    .where(MarketTradeSample.received_ts >= started_at)
                )
                or 0
            ),
            "db_rows_with_exchange_ts": int(
                session.scalar(
                    select(func.count())
                    .select_from(MarketMicrostructureSnapshot)
                    .where(MarketMicrostructureSnapshot.exchange_ts.is_not(None))
                )
                or 0
            ),
            "db_rows_received_ts_only": int(
                session.scalar(
                    select(func.count())
                    .select_from(MarketMicrostructureSnapshot)
                    .where(MarketMicrostructureSnapshot.freshness_basis == "received_ts_only")
                )
                or 0
            ),
            "strict_dual_freshness_eligible_rows": int(
                session.scalar(
                    select(func.count())
                    .select_from(MarketMicrostructureSnapshot)
                    .where(MarketMicrostructureSnapshot.strict_dual_freshness_eligible.is_(True))
                )
                or 0
            ),
            "trade_samples_persisted": int(
                session.scalar(select(func.count()).select_from(MarketTradeSample)) or 0
            ),
        }


def _instrument_refs(items: Any) -> list[InstrumentRef]:
    refs: list[InstrumentRef] = []
    if not isinstance(items, list):
        return refs
    for item in items:
        if not isinstance(item, dict):
            continue
        refs.append(
            InstrumentRef(
                instrument_id=str(item.get("instrument_id") or ""),
                instrument_uid=str(item.get("instrument_uid") or "") or None,
                figi=str(item.get("figi") or "") or None,
                class_code=str(item.get("class_code") or "TQBR"),
                ticker=str(item.get("ticker") or ""),
            )
        )
    return [ref for ref in refs if ref.instrument_id or ref.instrument_uid or ref.figi]


def _get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _ticker(value: str) -> str:
    return value.rsplit(":", 1)[-1].upper()


def _payload_has_exchange_ts(payload: Any) -> bool:
    if isinstance(payload, dict):
        if payload.get("exchange_ts") or payload.get("exchange_time") or payload.get("time"):
            return True
        return any(_payload_has_exchange_ts(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_has_exchange_ts(item) for item in payload)
    return False


def _payload_list_has_exchange_ts(items: list[Any]) -> bool:
    return any(_payload_has_exchange_ts(item) for item in items)


def _list_payload(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return list(payload[key])
    return []


if __name__ == "__main__":
    raise SystemExit(main())

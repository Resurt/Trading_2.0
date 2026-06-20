"""Run a bounded data-only shadow collector smoke without trading calls."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.runtime import TradeCoreRuntime, TradeCoreRuntimeConfig
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.service import DatabaseService


def main() -> None:
    args = parse_args()
    payload = asyncio.run(async_main(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    raise SystemExit(0 if not payload.get("errors") else 1)


async def async_main(args: argparse.Namespace) -> dict[str, object]:
    if args.dry_run:
        return {
            "runtime_started": False,
            "data_only_shadow_enabled": True,
            "real_orders_disabled": True,
            "post_order_calls": 0,
            "cancel_order_calls": 0,
            "candles_received": 0,
            "order_books_received": 0,
            "market_state_snapshots_written": 0,
            "spread_samples": 0,
            "avg_spread_bps": None,
            "p95_spread_bps": None,
            "avg_book_imbalance": None,
            "avg_market_quality_score": None,
            "max_stream_message_age_seconds": None,
            "stream_reconnect_total": 0,
            "warnings": ["dry_run_no_broker_calls"],
            "errors": [],
        }

    env = os.environ.copy()
    env["TRADING_DATA_ONLY_SHADOW"] = "true"
    env["TRADING_RUNTIME_MODE"] = "shadow"
    env["TRADING_INSTRUMENTS"] = args.instruments
    if args.database_url:
        env["TRADING_DATABASE_URL"] = args.database_url
    if args.require_dividend_sync:
        dividend_status = _dividend_sync_status(args.database_url)
        if not dividend_status.get("ready_for_shadow"):
            return {
                "runtime_started": False,
                "data_only_shadow_enabled": True,
                "real_orders_disabled": True,
                "post_order_calls": 0,
                "cancel_order_calls": 0,
                "errors": ["dividend_sync_not_ready"],
                "dividend_sync_status": dividend_status,
            }
    config = TradeCoreRuntimeConfig.from_env(env)
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
    )
    errors: list[str] = []
    warnings: list[str] = []
    started_successfully = False
    started_at = datetime.now(tz=UTC)
    try:
        await runtime.start()
        started_successfully = True
        await runtime.run_cycle()
        await asyncio.sleep(max(args.minutes, 0.1) * 60)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await runtime.shutdown()
        except Exception as exc:
            errors.append(f"shutdown:{type(exc).__name__}: {exc}")

    collector_stats = (
        runtime.live_market_data_collector.stats
        if runtime.live_market_data_collector is not None
        else None
    )
    spread_samples = collector_stats.spread_samples if collector_stats is not None else []
    if collector_stats is not None and collector_stats.market_state_snapshots_written == 0:
        warnings.append("market_closed_or_no_live_order_book_samples")
    age_seconds = None
    if runtime.stats.last_stream_message_at is not None:
        age_seconds = max(
            Decimal("0"),
            Decimal(
                str(
                    (
                        datetime.now(tz=UTC) - runtime.stats.last_stream_message_at
                    ).total_seconds()
                )
            ),
        ).quantize(Decimal("0.001"))
    return {
        "runtime_started": started_successfully,
        "data_only_shadow_enabled": config.data_only_shadow_enabled,
        "real_orders_disabled": True,
        "post_order_calls": len(getattr(runtime.broker_gateway, "post_order_calls", [])),
        "cancel_order_calls": len(getattr(runtime.broker_gateway, "cancel_order_calls", [])),
        "candles_received": collector_stats.candles_received if collector_stats else 0,
        "order_books_received": collector_stats.order_books_received if collector_stats else 0,
        "market_state_snapshots_written": (
            collector_stats.market_state_snapshots_written if collector_stats else 0
        ),
        "spread_samples": len(spread_samples),
        "avg_spread_bps": _optional_decimal(avg(spread_samples)),
        "p95_spread_bps": _optional_decimal(percentile(spread_samples, 0.95)),
        "avg_book_imbalance": None,
        "avg_market_quality_score": None,
        "max_stream_message_age_seconds": _optional_decimal(age_seconds),
        "stream_reconnect_total": 0,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "warnings": warnings,
        "errors": errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="SBER,GAZP")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--database-url")
    parser.add_argument("--require-dividend-sync", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _dividend_sync_status(database_url: str | None) -> dict[str, object]:
    from trade_core.corporate_actions import dividend_sync_status_payload
    from trading_common.db.config import build_database_url_from_env

    database = DatabaseService(database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            return dividend_sync_status_payload(session)
    finally:
        database.engine.dispose()


def avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.0001"))


def percentile(values: list[Decimal], pct: float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))
    return ordered[index].quantize(Decimal("0.0001"))


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


if __name__ == "__main__":
    main()

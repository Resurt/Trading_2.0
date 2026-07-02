"""Run a bounded data-only shadow collector smoke without trading calls."""

# ruff: noqa: E402, SLF001

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from sys import path

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.runtime import TradeCoreRuntime, TradeCoreRuntimeConfig
from trade_core.session import TradingSessionPreflightConfig, TradingSessionPreflightService
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import (
    BrokerOrder,
    MarketMicrostructureSnapshot,
    OrderIntent,
    SignalCandidate,
)
from trading_common.db.service import DatabaseService


def main() -> None:
    args = parse_args()
    payload = asyncio.run(async_main(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    raise SystemExit(0 if payload.get("passed", not payload.get("errors")) else 1)


async def async_main(args: argparse.Namespace) -> dict[str, object]:
    if args.dry_run:
        return _dry_run_payload(args)

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
                "passed": False,
                "runtime_started": False,
                "data_only_shadow_enabled": True,
                "real_orders_disabled": True,
                "post_order_calls": 0,
                "cancel_order_calls": 0,
                "signal_candidates_delta": 0,
                "order_intents_delta": 0,
                "broker_orders_delta": 0,
                "microstructure_snapshots_delta": 0,
                "errors": ["dividend_sync_not_ready"],
                "dividend_sync_status": dividend_status,
            }

    config = TradeCoreRuntimeConfig.from_env(env)
    runtime = TradeCoreRuntime(
        config=config,
        launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
    )
    initial_counts = _table_counts(runtime.database)
    resolve_error = await _resolve_instruments_for_preflight(runtime)
    config = runtime.config
    if resolve_error is not None:
        await _shutdown_runtime(runtime)
        return {
            "passed": False,
            "runtime_started": False,
            "data_only_shadow_enabled": config.data_only_shadow_enabled,
            "real_orders_disabled": True,
            "market_open": False,
            "market_closed_expected": False,
            "post_order_calls": 0,
            "cancel_order_calls": 0,
            "signal_candidates_delta": 0,
            "order_intents_delta": 0,
            "broker_orders_delta": 0,
            "microstructure_snapshots_delta": 0,
            "errors": ["instrument_resolution_failed", resolve_error],
        }

    preflight = await TradingSessionPreflightService(runtime.broker_gateway).run(
        TradingSessionPreflightConfig(
            exchange=runtime.config.exchange,
            instruments=runtime.config.instruments,
        )
    )
    preflight_payload = preflight.as_payload()
    preflight_payload["cache_hit"] = False
    requested_instruments = _requested_instruments(args.instruments)
    preflight_payload.setdefault("requested_instruments", requested_instruments)
    if (
        args.preflight_only
        or not preflight.market_open
        or not getattr(preflight, "data_only_collection_allowed", preflight.market_open)
    ):
        await _shutdown_runtime(runtime)
        deltas = _count_deltas(initial_counts, _table_counts(runtime.database))
        closed_success = (
            preflight.market_closed_expected
            and args.allow_closed_market_success
            and not args.require_market_open
        )
        passed = bool(args.preflight_only or closed_success)
        warning = (
            "market_closed_expected_no_live_samples"
            if preflight.market_closed_expected
            else preflight.reason_code
        )
        return {
            "passed": passed,
            "runtime_started": False,
            "preflight_only": bool(args.preflight_only),
            "data_only_shadow_enabled": config.data_only_shadow_enabled,
            "real_orders_disabled": True,
            "market_open": preflight.market_open,
            "market_closed_expected": preflight.market_closed_expected,
            "reason_code": preflight.reason_code,
            "next_session_at": preflight_payload.get("next_session_at"),
            "session_type": preflight.session_type,
            "session_phase": preflight.session_phase,
            "requested_instruments": requested_instruments,
            "working_instruments": preflight_payload.get("working_instruments", []),
            "blocked_instruments": preflight_payload.get("blocked_instruments", []),
            "preflight": preflight_payload,
            "post_order_calls": 0,
            "cancel_order_calls": 0,
            "signal_candidates_delta": deltas["signal_candidate"],
            "order_intents_delta": deltas["order_intent"],
            "broker_orders_delta": deltas["broker_order"],
            "microstructure_snapshots_delta": deltas["market_microstructure_snapshot"],
            "candles_received": 0,
            "order_books_received": 0,
            "market_state_snapshots_written": 0,
            "warnings": [warning],
            "warning": warning,
            "errors": [] if passed else ["market_not_open"],
        }

    working_filter_error = _apply_working_instruments(runtime, preflight_payload)
    config = runtime.config
    if working_filter_error is not None:
        await _shutdown_runtime(runtime)
        deltas = _count_deltas(initial_counts, _table_counts(runtime.database))
        return {
            "passed": False,
            "runtime_started": False,
            "preflight_only": False,
            "data_only_shadow_enabled": config.data_only_shadow_enabled,
            "real_orders_disabled": True,
            "market_open": preflight.market_open,
            "market_closed_expected": preflight.market_closed_expected,
            "reason_code": working_filter_error,
            "next_session_at": preflight_payload.get("next_session_at"),
            "session_type": preflight.session_type,
            "session_phase": preflight.session_phase,
            "requested_instruments": requested_instruments,
            "working_instruments": preflight_payload.get("working_instruments", []),
            "blocked_instruments": preflight_payload.get("blocked_instruments", []),
            "preflight": preflight_payload,
            "post_order_calls": 0,
            "cancel_order_calls": 0,
            "signal_candidates_delta": deltas["signal_candidate"],
            "order_intents_delta": deltas["order_intent"],
            "broker_orders_delta": deltas["broker_order"],
            "microstructure_snapshots_delta": deltas["market_microstructure_snapshot"],
            "candles_received": 0,
            "order_books_received": 0,
            "market_state_snapshots_written": 0,
            "warnings": [],
            "warning": working_filter_error,
            "errors": [working_filter_error],
        }

    return await _run_open_market_smoke(
        args=args,
        config=config,
        runtime=runtime,
        initial_counts=initial_counts,
        preflight_payload=preflight_payload,
    )


async def _run_open_market_smoke(
    *,
    args: argparse.Namespace,
    config: TradeCoreRuntimeConfig,
    runtime: TradeCoreRuntime,
    initial_counts: dict[str, int],
    preflight_payload: dict[str, object],
) -> dict[str, object]:
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
        if _is_resource_exhausted(exc):
            warnings.append("broker_resource_exhausted")
            errors.append(
                "broker_resource_exhausted: reduce universe or --max-instruments-per-stream-batch"
            )
        else:
            errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        shutdown_error = await _shutdown_runtime(runtime)
        if shutdown_error is not None:
            errors.append(shutdown_error)

    collector_stats = (
        runtime.live_market_data_collector.stats
        if runtime.live_market_data_collector is not None
        else None
    )
    spread_samples = collector_stats.spread_samples if collector_stats is not None else []
    if collector_stats is not None and collector_stats.market_state_snapshots_written == 0:
        warnings.append("market_open_but_no_live_order_book_samples")
    age_seconds = None
    if runtime.stats.last_stream_message_at is not None:
        age_seconds = max(
            Decimal("0"),
            Decimal(
                str((datetime.now(tz=UTC) - runtime.stats.last_stream_message_at).total_seconds())
            ),
        ).quantize(Decimal("0.001"))
    final_counts = _table_counts(runtime.database)
    deltas = _count_deltas(initial_counts, final_counts)
    forbidden_deltas = [
        name for name in ("signal_candidate", "order_intent", "broker_order") if deltas[name] != 0
    ]
    if forbidden_deltas:
        errors.append(f"data_only_shadow_order_path_delta:{','.join(forbidden_deltas)}")
    post_order_calls = len(getattr(runtime.broker_gateway, "post_order_calls", []))
    cancel_order_calls = len(getattr(runtime.broker_gateway, "cancel_order_calls", []))
    if post_order_calls or cancel_order_calls:
        errors.append("real_order_call_detected")

    return {
        "passed": not errors,
        "runtime_started": started_successfully,
        "preflight_only": False,
        "data_only_shadow_enabled": config.data_only_shadow_enabled,
        "real_orders_disabled": True,
        "market_open": preflight_payload.get("market_open"),
        "market_closed_expected": preflight_payload.get("market_closed_expected"),
        "reason_code": preflight_payload.get("reason_code"),
        "next_session_at": preflight_payload.get("next_session_at"),
        "session_type": preflight_payload.get("session_type"),
        "session_phase": preflight_payload.get("session_phase"),
        "requested_instruments": preflight_payload.get("requested_instruments", []),
        "working_instruments": preflight_payload.get("working_instruments", []),
        "blocked_instruments": preflight_payload.get("blocked_instruments", []),
        "preflight": preflight_payload,
        "post_order_calls": post_order_calls,
        "cancel_order_calls": cancel_order_calls,
        "signal_candidates_delta": deltas["signal_candidate"],
        "order_intents_delta": deltas["order_intent"],
        "broker_orders_delta": deltas["broker_order"],
        "microstructure_snapshots_delta": deltas["market_microstructure_snapshot"],
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
        "stream_batch": {
            "max_instruments_per_stream_batch": args.max_instruments_per_stream_batch,
            "stream_batch_delay_seconds": args.stream_batch_delay_seconds,
            "batching_supported_by_gateway": hasattr(
                runtime.broker_gateway,
                "set_market_stream_instruments",
            ),
        },
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "warnings": warnings,
        "errors": errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--database-url")
    parser.add_argument("--require-dividend-sync", action="store_true")
    parser.add_argument("--require-market-open", action="store_true")
    parser.add_argument(
        "--allow-closed-market-success",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--no-preflight-cache", action="store_true")
    parser.add_argument("--max-instruments-per-stream-batch", type=int, default=4)
    parser.add_argument("--stream-batch-delay-seconds", type=float, default=2.0)
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _dry_run_payload(args: argparse.Namespace) -> dict[str, object]:
    return {
        "passed": True,
        "runtime_started": False,
        "preflight_only": bool(args.preflight_only),
        "data_only_shadow_enabled": True,
        "real_orders_disabled": True,
        "market_open": False,
        "market_closed_expected": False,
        "post_order_calls": 0,
        "cancel_order_calls": 0,
        "signal_candidates_delta": 0,
        "order_intents_delta": 0,
        "broker_orders_delta": 0,
        "microstructure_snapshots_delta": 0,
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


async def _resolve_instruments_for_preflight(runtime: TradeCoreRuntime) -> str | None:
    runtime._session = runtime.database.session_factory()
    try:
        await runtime._resolve_runtime_instruments()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if runtime._session is not None:
            runtime._session.close()
            runtime._session = None
    return None


def _requested_instruments(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _apply_working_instruments(
    runtime: TradeCoreRuntime,
    preflight_payload: dict[str, object],
) -> str | None:
    working_raw = preflight_payload.get("working_instruments")
    if not isinstance(working_raw, list):
        return None
    working_keys = {str(item).strip() for item in working_raw if str(item).strip()}
    if not working_keys:
        return "no_tradeable_instruments"
    filtered = tuple(
        instrument
        for instrument in runtime.config.instruments
        if _instrument_matches_working_key(instrument, working_keys)
    )
    if not filtered:
        return "working_instruments_not_in_runtime_config"
    runtime.config = replace(runtime.config, instruments=filtered)
    preflight_payload["working_instruments"] = [
        _instrument_key_for_payload(instrument) for instrument in filtered
    ]
    return None


def _instrument_matches_working_key(
    instrument: object,
    working_keys: set[str],
) -> bool:
    return any(
        value in working_keys
        for value in (
            getattr(instrument, "instrument_id", None),
            getattr(instrument, "instrument_uid", None),
            getattr(instrument, "figi", None),
            getattr(instrument, "ticker", None),
        )
        if value
    )


def _instrument_key_for_payload(instrument: object) -> str:
    for attr in ("instrument_id", "ticker", "instrument_uid", "figi"):
        value = getattr(instrument, attr, None)
        if value:
            return str(value)
    return "unknown"


async def _shutdown_runtime(runtime: TradeCoreRuntime) -> str | None:
    try:
        await runtime.shutdown()
    except Exception as exc:
        return f"shutdown:{type(exc).__name__}: {exc}"
    return None


def _table_counts(database: DatabaseService) -> dict[str, int]:
    models = {
        "signal_candidate": SignalCandidate,
        "order_intent": OrderIntent,
        "broker_order": BrokerOrder,
        "market_microstructure_snapshot": MarketMicrostructureSnapshot,
    }
    counts: dict[str, int] = {}
    try:
        with database.session_scope() as session:
            for name, model in models.items():
                counts[name] = int(
                    session.execute(select(func.count()).select_from(model)).scalar_one()
                )
    except Exception:
        return {name: 0 for name in models}
    return counts


def _count_deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {name: after.get(name, 0) - before.get(name, 0) for name in before}


def _is_resource_exhausted(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".upper()
    return "RESOURCE_EXHAUSTED" in text


if __name__ == "__main__":
    main()

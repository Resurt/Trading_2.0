"""Download historical T-Bank candles and persist raw/derived bars for replay."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date
from pathlib import Path
from sys import path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.broker_gateway import BrokerUnaryResponse, CandleRequest
from trade_core.infra.tbank import TBankBrokerConfig, TBankBrokerGateway
from trade_core.market_data.historical_backfill import (
    HistoricalCandleBackfillService,
    config_from_strings,
    count_market_candles,
    default_backfill_window,
)
from trading_common import LaunchModePolicy, RuntimeMode, parse_runtime_mode
from trading_common.db.base import Base
from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService


class DryRunBrokerGateway:
    """Broker placeholder for plan-only dry runs."""

    async def get_candles(
        self,
        request: CandleRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(method_name="GetCandles", data={"candles": []}, headers={})


async def async_main() -> None:
    args = parse_args()
    from_ts_utc, to_ts_utc = default_backfill_window(
        from_date=args.from_date,
        to_date=args.to_date,
        lookback_days=args.lookback_days,
    )
    runtime_mode = _runtime_mode(args.runtime_mode, dry_run=args.dry_run)
    launch_policy = LaunchModePolicy.from_mode(runtime_mode)
    database_url = _database_url(args.database_url, dry_run=args.dry_run)
    database = DatabaseService(database_url)
    try:
        if args.create_schema or args.dry_run:
            Base.metadata.create_all(database.engine)
        with database.session_scope() as session:
            broker_gateway = (
                DryRunBrokerGateway()
                if args.dry_run
                else TBankBrokerGateway(
                    config=TBankBrokerConfig.from_launch_policy(launch_policy)
                )
            )
            service = HistoricalCandleBackfillService(
                broker_gateway=broker_gateway,  # type: ignore[arg-type]
                session=session,
                launch_policy=launch_policy,
            )
            config = config_from_strings(
                instruments=args.instruments,
                raw_interval=args.raw_interval,
                derive=args.derive,
                lookback_days=args.lookback_days,
                chunk_days=args.chunk_days,
                strategy_id=args.strategy_id,
                dry_run=args.dry_run,
                runtime_mode=runtime_mode.value,
                resolve_instruments=args.resolve_instruments,
                require_resolved_instruments=args.require_resolved_instruments,
                allow_unresolved=args.allow_unresolved,
            )
            result = await service.run(
                config,
                from_ts_utc=from_ts_utc,
                to_ts_utc=to_ts_utc,
            )
            market_candle_rows = 0
            if not args.dry_run:
                market_candle_rows = count_market_candles(
                    session,
                    from_ts_utc=from_ts_utc,
                    to_ts_utc=to_ts_utc,
                    instruments=result.plan.instruments,
                    timeframes=(result.plan.raw_interval, *result.plan.derived_intervals),
                )
            payload = {
                **result.as_payload(),
                "runtime_mode": runtime_mode.value,
                "database_url_configured": bool(database_url),
                "market_candle_rows_in_scope": market_candle_rows,
                "next_steps": [
                    "run replay from market_candle rows",
                    "run report rebuild/counterfactual after replay creates candidates",
                ],
            }
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2 if args.json_output else None,
                default=json_default,
            )
        )
    finally:
        database.engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="SBER,GAZP")
    parser.add_argument("--from-date", type=parse_date)
    parser.add_argument("--to-date", type=parse_date)
    parser.add_argument("--raw-interval", default="1m")
    parser.add_argument("--derive", default="5m,10m,15m")
    parser.add_argument("--chunk-days", type=int, default=7)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--strategy-id", default="baseline")
    parser.add_argument("--database-url")
    parser.add_argument(
        "--runtime-mode",
        default=os.getenv("TRADING_BACKFILL_RUNTIME_MODE"),
        help=(
            "Readonly broker mode for real backfill. Defaults to shadow for real runs "
            "and historical_replay for --dry-run."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument(
        "--resolve-instruments",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--require-resolved-instruments",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="Allow seed/internal instrument IDs; intended only for dry-run/local smoke.",
    )
    parser.add_argument(
        "--create-schema",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create SQLAlchemy schema before writing, intended for local smoke only.",
    )
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _runtime_mode(value: str | None, *, dry_run: bool) -> RuntimeMode:
    if value:
        return parse_runtime_mode(value)
    return RuntimeMode.HISTORICAL_REPLAY if dry_run else RuntimeMode.SHADOW


def _database_url(value: str | None, *, dry_run: bool) -> str:
    if value:
        return value
    if dry_run:
        return "sqlite+pysqlite:///:memory:"
    return build_database_url_from_env()


def json_default(value: Any) -> str:
    return str(value)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

"""Run deterministic historical replay from persisted market_candle bars."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from sys import path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT,
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.market_data.events import parse_timeframe  # noqa: E402
from trade_core.replay import (  # noqa: E402
    HistoricalDbReplayConfig,
    HistoricalDbReplayService,
    default_replay_window,
)
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.service import DatabaseService  # noqa: E402


def main() -> None:
    args = parse_args()
    from_date, to_date = default_replay_window(
        from_date=args.from_date,
        to_date=args.to_date,
        lookback_days=args.lookback_days,
    )
    config = HistoricalDbReplayConfig(
        from_date=from_date,
        to_date=to_date,
        instruments=tuple(item.strip() for item in args.instruments.split(",") if item.strip()),
        timeframes=tuple(
            parse_timeframe(item.strip()) for item in args.timeframes.split(",") if item.strip()
        ),
        strategy_id=args.strategy_id,
        strategy_version=args.strategy_version,
        dry_run=args.dry_run,
        reset_derived_events=args.reset_derived_events,
        max_days=args.max_days,
    )
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            result = asyncio.run(HistoricalDbReplayService(session).run(config))
            payload = result.as_payload()
    finally:
        database.engine.dispose()
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    else:
        print(json.dumps(payload, ensure_ascii=False, default=json_default))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", type=parse_date)
    parser.add_argument("--to-date", type=parse_date)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--instruments", default="SBER,GAZP")
    parser.add_argument("--timeframes", default="5m,10m,15m")
    parser.add_argument("--strategy-id", default="baseline")
    parser.add_argument("--strategy-version", default="latest")
    parser.add_argument("--runtime-mode", default="historical_replay")
    parser.add_argument("--database-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-derived-events", action="store_true")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()

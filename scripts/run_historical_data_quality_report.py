"""Build a quality report for historical candles stored in market_candle."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
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

from trade_core.market_data.events import parse_timeframe
from trade_core.market_data.quality import (
    HistoricalDataQualityConfig,
    HistoricalDataQualityService,
    default_quality_window,
)
from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService


def main() -> None:
    args = parse_args()
    from_date, to_date = default_quality_window(
        from_date=args.from_date,
        to_date=args.to_date,
        lookback_days=args.lookback_days,
    )
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            service = HistoricalDataQualityService(session)
            report = service.assert_passes(
                HistoricalDataQualityConfig(
                    from_date=from_date,
                    to_date=to_date,
                    instruments=_split(args.instruments),
                    timeframes=tuple(parse_timeframe(item) for item in _split(args.timeframes)),
                    fail_on_missing=args.fail_on_missing,
                    fail_on_invalid_ohlc=args.fail_on_invalid_ohlc,
                    max_missing_intervals=args.max_missing_intervals,
                    write_report=not args.no_write,
                )
            )
            payload = report.as_payload()
    finally:
        database.engine.dispose()

    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", type=parse_date)
    parser.add_argument("--to-date", type=parse_date)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--instruments", default="SBER,GAZP")
    parser.add_argument("--timeframes", default="1m,5m,10m,15m")
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--fail-on-missing", action="store_true")
    parser.add_argument("--fail-on-invalid-ohlc", action="store_true")
    parser.add_argument("--max-missing-intervals", type=int)
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _split(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()

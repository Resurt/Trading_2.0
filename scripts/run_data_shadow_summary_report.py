"""Build a lightweight data-only shadow microstructure summary report."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sys import path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import MarketMicrostructureSnapshot
from trading_common.db.service import DatabaseService


def main() -> None:
    args = parse_args()
    payload = run_report(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def run_report(args: argparse.Namespace) -> dict[str, object]:
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        since = datetime.now(tz=UTC) - timedelta(hours=args.lookback_hours)
        with database.session_scope() as session:
            rows = list(
                session.execute(
                    select(MarketMicrostructureSnapshot).where(
                        MarketMicrostructureSnapshot.ts_utc >= since
                    )
                ).scalars()
            )
        payload = summarize(rows, lookback_hours=args.lookback_hours)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "data_shadow_summary_latest.json"
        output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["output_file"] = str(output_file)
        return payload
    finally:
        database.engine.dispose()


def summarize(
    rows: list[MarketMicrostructureSnapshot],
    *,
    lookback_hours: int,
) -> dict[str, object]:
    spreads = [row.spread_bps for row in rows if row.spread_bps is not None]
    bid_depth = [row.bid_depth_lots for row in rows if row.bid_depth_lots is not None]
    ask_depth = [row.ask_depth_lots for row in rows if row.ask_depth_lots is not None]
    imbalance = [row.book_imbalance for row in rows if row.book_imbalance is not None]
    quality = [row.market_quality_score for row in rows if row.market_quality_score is not None]
    instruments = sorted({row.instrument_id for row in rows})
    sessions = sorted({row.session_type for row in rows})
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "lookback_hours": lookback_hours,
        "instruments": instruments,
        "sessions": sessions,
        "snapshots_count": len(rows),
        "avg_spread_bps": _optional_decimal(avg(spreads)),
        "p50_spread_bps": _optional_decimal(percentile(spreads, 0.50)),
        "p95_spread_bps": _optional_decimal(percentile(spreads, 0.95)),
        "avg_bid_depth_lots": _optional_decimal(avg(bid_depth)),
        "p50_bid_depth_lots": _optional_decimal(percentile(bid_depth, 0.50)),
        "p95_bid_depth_lots": _optional_decimal(percentile(bid_depth, 0.95)),
        "avg_ask_depth_lots": _optional_decimal(avg(ask_depth)),
        "p50_ask_depth_lots": _optional_decimal(percentile(ask_depth, 0.50)),
        "p95_ask_depth_lots": _optional_decimal(percentile(ask_depth, 0.95)),
        "avg_imbalance": _optional_decimal(avg(imbalance)),
        "avg_market_quality_score": _optional_decimal(avg(quality)),
        "stale_data_incidents": sum(1 for row in rows if row.is_stale),
        "stream_gap_count": 0,
        "candle_lag_p95": None,
        "market_quality_distribution": {
            "samples": len(quality),
            "p50": _optional_decimal(percentile(quality, 0.50)),
            "p95": _optional_decimal(percentile(quality, 0.95)),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument(
        "--output-dir",
        default=".local/collection_reports/data_shadow",
    )
    return parser.parse_args()


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

"""Rebuild hourly and daily reports from historical replay facts."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from sys import path
from time import monotonic
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT,
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from report_worker.analytics.historical_reports import (  # noqa: E402
    HistoricalReportRebuildConfig,
    HistoricalReportRebuildService,
    default_report_window,
)
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.service import DatabaseService  # noqa: E402


def main() -> None:
    args = parse_args()
    from_date, to_date = default_report_window(
        from_date=args.from_date,
        to_date=args.to_date,
        lookback_days=args.lookback_days,
    )
    progress_events: list[dict[str, object]] = []
    config = HistoricalReportRebuildConfig(
        from_date=from_date,
        to_date=to_date,
        strategy_id=args.strategy_id,
        instrument=args.instrument,
        timeframe=args.timeframe,
        session_type=args.session_type,
        include_counterfactual=args.include_counterfactual,
        force_rebuild=args.force_rebuild,
        skip_existing=args.skip_existing,
        chunk_days=args.chunk_days,
        progress_every=args.progress_every,
        max_days=args.max_days,
        dry_run=args.dry_run,
        progress_callback=progress_callback(args.progress_every, progress_events),
    )
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            payload = HistoricalReportRebuildService(session).rebuild(config).as_payload()
            if progress_events:
                payload["progress_events"] = progress_events
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
    parser.add_argument("--strategy-id", default="baseline")
    parser.add_argument("--instrument")
    parser.add_argument("--timeframe")
    parser.add_argument("--session-type")
    parser.add_argument("--include-counterfactual", action="store_true")
    parser.add_argument("--force-rebuild", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--chunk-days", type=int, default=30)
    parser.add_argument("--progress-every", type=int, default=0)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--database-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def json_default(value: Any) -> str:
    return str(value)


def progress_callback(progress_every: int, progress_events: list[dict[str, object]]):
    if progress_every <= 0:
        return None
    started_at = monotonic()

    def _emit(payload: dict[str, object]) -> None:
        progress_events.append(
            {
                **payload,
                "elapsed_seconds": round(monotonic() - started_at, 3),
            }
        )

    return _emit


if __name__ == "__main__":
    main()

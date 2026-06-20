"""Build Intraday Analytics snapshots from persisted market and decision facts."""

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
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from report_worker.analytics.calibration_observatory import IntradayAnalyticsService
from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="trading_date", type=date.fromisoformat)
    parser.add_argument("--session-type")
    parser.add_argument("--micro-session-id")
    parser.add_argument(
        "--mode",
        choices=("data_shadow", "historical", "strategy_shadow", "all"),
        default="all",
    )
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument(
        "--output-dir",
        default=".local/collection_reports/intraday",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def run(args: argparse.Namespace) -> dict[str, Any]:
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            service = IntradayAnalyticsService(session)
            if args.micro_session_id:
                payload = service.build_for_micro_session(args.micro_session_id)
            elif args.session_type:
                trading_date = args.trading_date or date.today()
                payload = service.build_for_session(
                    trading_date,
                    args.session_type,
                    mode=args.mode,
                )
            elif args.trading_date:
                payload = service.build_for_trading_date(args.trading_date, mode=args.mode)
            else:
                payload = service.build_current_day_snapshot()
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        name = _output_name(args)
        output_file = output_dir / name
        output_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        payload["output_file"] = str(output_file)
        return payload
    finally:
        database.engine.dispose()


def _output_name(args: argparse.Namespace) -> str:
    if args.micro_session_id:
        safe = args.micro_session_id.replace(":", "_")
        return f"intraday_micro_session_{safe}.json"
    trading_date = args.trading_date.isoformat() if args.trading_date else "current"
    if args.session_type:
        return f"intraday_{trading_date}_{args.session_type}.json"
    return f"intraday_{trading_date}.json"


if __name__ == "__main__":
    main()

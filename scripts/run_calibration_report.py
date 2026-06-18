"""Build calibration report from historical replay analytics tables."""

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
    ROOT,
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from report_worker.analytics.calibration import (  # noqa: E402
    CalibrationReportConfig,
    CalibrationReportService,
    default_calibration_window,
)
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.service import DatabaseService  # noqa: E402


def main() -> None:
    args = parse_args()
    from_date, to_date = default_calibration_window(
        from_date=args.from_date,
        to_date=args.to_date,
        lookback_days=args.lookback_days,
    )
    config = CalibrationReportConfig(
        from_date=from_date,
        to_date=to_date,
        strategy_id=args.strategy_id,
        instruments=split_csv(args.instruments),
        timeframes=split_csv(args.timeframes),
        group_by=split_csv(args.group_by),
        force_rebuild=args.force_rebuild,
        calibration_scope=args.calibration_scope,
        include_dividend_gap_days=args.include_dividend_gap_days,
        include_corporate_action_days=args.include_corporate_action_days,
        include_abnormal_gap_days=args.include_abnormal_gap_days,
        require_special_day_classification=args.require_special_day_classification,
    )
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            payload = CalibrationReportService(session).build(config).as_payload()
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
    parser.add_argument("--instruments", default="SBER,GAZP")
    parser.add_argument("--timeframes", default="5m,10m,15m")
    parser.add_argument(
        "--group-by",
        default="session_type,instrument_id,timeframe,blocker_code",
    )
    parser.add_argument("--database-url")
    parser.add_argument("--force-rebuild", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--calibration-scope",
        choices=("primary_normal_days", "special_days_only", "all_days"),
        default="primary_normal_days",
    )
    parser.add_argument("--include-dividend-gap-days", action="store_true")
    parser.add_argument("--include-corporate-action-days", action="store_true")
    parser.add_argument("--include-abnormal-gap-days", action="store_true")
    parser.add_argument("--require-special-day-classification", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()

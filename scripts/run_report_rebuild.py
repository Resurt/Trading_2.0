"""Rebuild hourly/daily/counterfactual reports for a date."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from sys import path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT,
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from report_worker.analytics import ReportAnalyticsService  # noqa: E402
from tests.fixtures.logging_analytics_acceptance import (  # noqa: E402
    STRATEGY_ID,
    TRADING_DATE,
    seed_logging_analytics_acceptance_day,
)
from trading_common.db.base import Base  # noqa: E402


def main() -> None:
    args = parse_args()
    engine = create_engine(args.database_url)
    try:
        if args.create_schema:
            Base.metadata.create_all(engine)
        with Session(engine) as session:
            if args.seed_fixture:
                seed_logging_analytics_acceptance_day(session)
                session.flush()
            daily = ReportAnalyticsService(session).rebuild_reports_for_date(
                trading_date=args.trading_date,
                strategy_id=args.strategy_id,
                force_rebuild=args.force_rebuild,
                include_counterfactual=True,
            )
            payload = {
                "trading_date": daily.trading_date.isoformat(),
                "strategy_id": daily.strategy_id,
                "market_regime": daily.market_regime,
                "signal_count": daily.signal_count,
                "blocked_count": daily.blocked_count,
                "fill_ratio": str(daily.fill_ratio),
                "pnl_gross": str(daily.pnl_gross),
                "pnl_net": str(daily.pnl_net),
                "report_payload_keys": sorted(daily.report_payload),
                "funnel": daily.report_payload.get("funnel"),
                "blocker_ranking": daily.report_payload.get("blocker_ranking"),
                "missed_opportunity_summary": daily.report_payload.get(
                    "missed_opportunity_summary"
                ),
            }
            session.rollback()
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="trading_date", type=parse_date, default=TRADING_DATE)
    parser.add_argument("--strategy-id", default=STRATEGY_ID)
    parser.add_argument("--database-url", default="sqlite+pysqlite:///:memory:")
    parser.add_argument("--force-rebuild", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--seed-fixture", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--create-schema", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()

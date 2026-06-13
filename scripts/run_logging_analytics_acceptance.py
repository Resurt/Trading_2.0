"""Run logging/analytics acceptance checks on a deterministic fixture or database."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from sys import exit, path
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
from trading_common.analytics_acceptance import AnalyticsAcceptanceChecker  # noqa: E402
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
            ReportAnalyticsService(session).rebuild_reports_for_date(
                trading_date=args.trading_date,
                strategy_id=args.strategy_id,
                force_rebuild=True,
                include_counterfactual=True,
            )
            report = AnalyticsAcceptanceChecker(session).run(
                trading_date=args.trading_date,
                strategy_id=args.strategy_id,
            )
            session.rollback()
        print(json.dumps(report.as_payload(), ensure_ascii=False, indent=2, default=json_default))
        exit(0 if report.passed else 1)
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="trading_date", type=parse_date, default=TRADING_DATE)
    parser.add_argument("--strategy-id", default=STRATEGY_ID)
    parser.add_argument("--database-url", default="sqlite+pysqlite:///:memory:")
    parser.add_argument(
        "--seed-fixture",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Seed deterministic acceptance scenarios before running checks.",
    )
    parser.add_argument(
        "--create-schema",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Create SQLAlchemy tables before seeding/checking. "
            "Use --no-create-schema for migrated DBs."
        ),
    )
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()

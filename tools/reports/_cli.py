from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


def bootstrap_repo_paths() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for relative in (
        "apps/report-worker/src",
        "packages/common/src",
    ):
        path = str(repo_root / relative)
        if path not in sys.path:
            sys.path.insert(0, path)


bootstrap_repo_paths()

from report_worker.analytics import ReportAnalyticsService  # noqa: E402
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.service import DatabaseService  # noqa: E402


def add_common_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", required=True, help="Trading date in YYYY-MM-DD format.")
    parser.add_argument("--strategy-id", default="baseline")
    parser.add_argument("--instrument", dest="instrument_id")
    parser.add_argument("--timeframe")
    parser.add_argument("--session-type")
    parser.add_argument("--strategy-version", type=int)
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument(
        "--output-format",
        choices=("json", "html", "both"),
        default="json",
    )


def parsed_date(value: str) -> date:
    return date.fromisoformat(value)


def run_with_service(database_url: str | None) -> tuple[DatabaseService, Any]:
    database = DatabaseService(database_url or build_database_url_from_env())
    return database, database.session_scope()


def print_payload(payload: dict[str, object], *, output_format: str) -> None:
    if output_format == "html":
        html = payload.get("html")
        print(html if isinstance(html, str) else "")
        return
    if output_format == "both":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        html = payload.get("html")
        if isinstance(html, str):
            print("\n<!-- html_output -->")
            print(html)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_service(session: Any) -> ReportAnalyticsService:
    return ReportAnalyticsService(session)

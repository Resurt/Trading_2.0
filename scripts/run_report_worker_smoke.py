"""Submit build_hourly_report to Celery and wait for a completed Redis result."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from sys import path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT,
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from report_worker.celery_app import REPORTS_QUEUE, celery_app  # noqa: E402
from trading_common.db.base import Base  # noqa: E402
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.models import SessionRun  # noqa: E402


def main() -> None:
    args = parse_args()
    trading_date = date.fromisoformat(args.date)
    database_url = args.database_url or build_database_url_from_env()

    seed_smoke_session(
        database_url=database_url,
        micro_session_id=args.micro_session_id,
        trading_date=trading_date,
        strategy_id=args.strategy_id,
        create_schema=args.create_schema,
    )

    result = celery_app.send_task(
        "report_worker.build_hourly_report",
        kwargs={
            "micro_session_id": args.micro_session_id,
            "strategy_id": args.strategy_id,
            "force_rebuild": True,
        },
        queue=os.getenv("CELERY_REPORTS_QUEUE", REPORTS_QUEUE),
    )
    payload = result.get(timeout=args.timeout_seconds, interval=0.5)
    print(
        json.dumps(
            {
                "ok": True,
                "task_id": result.id,
                "task_name": "report_worker.build_hourly_report",
                "queue": os.getenv("CELERY_REPORTS_QUEUE", REPORTS_QUEUE),
                "micro_session_id": args.micro_session_id,
                "strategy_id": args.strategy_id,
                "status": result.status,
                "result": payload,
            },
            ensure_ascii=False,
            indent=2,
            default=json_default,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-06-12")
    parser.add_argument("--strategy-id", default="baseline")
    parser.add_argument("--micro-session-id", default="2026-06-12:weekday_main:1000")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument(
        "--create-schema",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Create SQLAlchemy tables before seeding the minimal smoke session.",
    )
    return parser.parse_args()


def seed_smoke_session(
    *,
    database_url: str,
    micro_session_id: str,
    trading_date: date,
    strategy_id: str,
    create_schema: bool,
) -> None:
    engine = create_engine(database_url)
    now = datetime.combine(trading_date, datetime.min.time(), tzinfo=UTC) + timedelta(hours=10)
    try:
        if create_schema:
            Base.metadata.create_all(engine)
        with Session(engine) as session:
            existing = session.execute(
                select(SessionRun).where(
                    SessionRun.micro_session_id == micro_session_id,
                    SessionRun.strategy_id == strategy_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    SessionRun(
                        calendar_date=trading_date,
                        trading_date=trading_date,
                        session_type="weekday_main",
                        session_phase="continuous_trading",
                        micro_session_id=micro_session_id,
                        broker_trading_status="normal_trading",
                        strategy_id=strategy_id,
                        strategy_version=1,
                        status="closed",
                        started_at=now,
                        ended_at=now + timedelta(hours=1),
                        freeze_started_at=None,
                        report_requested_at=now + timedelta(hours=1),
                        close_reason_code="report_worker_smoke",
                        run_payload={"seed": "report_worker_smoke"},
                    )
                )
            session.commit()
    finally:
        engine.dispose()


def json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()

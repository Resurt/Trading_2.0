"""Run Calibration Observatory diagnostics from persisted analytics facts."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sys import path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from report_worker.analytics.calibration_observatory import (
    CalibrationDiagnosticService,
    RollingPerformanceCubeService,
    StrategyConfigProposalService,
)
from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import MarketMicrostructureSnapshot, MarketTradeSample
from trading_common.db.service import DatabaseService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default="SBER,GAZP")
    parser.add_argument("--lookback-days", type=int, default=20)
    parser.add_argument("--windows", default="7d,20d,60d,90d,180d,365d")
    parser.add_argument(
        "--mode",
        choices=("data_shadow", "historical", "strategy_shadow", "all"),
        default="all",
    )
    parser.add_argument(
        "--trigger-type",
        choices=("manual", "scheduled_daily", "scheduled_weekly", "no_trade_alert", "drift_alert"),
        default="manual",
    )
    parser.add_argument("--create-candidate-config", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument(
        "--output-dir",
        default=".local/collection_reports/calibration_observatory",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def run(args: argparse.Namespace) -> dict[str, Any]:
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        universe = _parse_csv(args.universe)
        windows = _parse_csv(args.windows)
        with database.session_scope() as session:
            diagnostic = CalibrationDiagnosticService(session).run_diagnostics(
                universe,
                args.lookback_days,
                trigger_type=args.trigger_type,
                requested_by="cli",
                mode=args.mode,
            )
            cube_rows = RollingPerformanceCubeService(session).build_rolling_cube(
                windows,
                universe=universe,
                mode=args.mode,
            )
            candidate_config_id: str | None = None
            if args.create_candidate_config:
                proposal = StrategyConfigProposalService(session).create_strategy_config_candidate(
                    base_strategy_id="baseline",
                    proposed_strategy_id="baseline_candidate_draft",
                    source_diagnostic_run_id=UUID(str(diagnostic["diagnostic_run_id"])),
                    proposal_payload={
                        "source": "calibration_observatory",
                        "diagnosis": diagnostic["diagnosis"],
                        "apply_automatically": False,
                    },
                    validation_payload={"rolling_cube_rows": len(cube_rows)},
                    proposed_by="system",
                )
                candidate_config_id = str(proposal["candidate_config_id"])
            exchange_ts_metadata = _exchange_ts_metadata(session, lookback_days=args.lookback_days)
        payload = _output_payload(
            diagnostic=diagnostic,
            cube_rows=cube_rows,
            candidate_config_id=candidate_config_id,
            exchange_ts_metadata=exchange_ts_metadata,
        )
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "calibration_observatory_latest.json"
        output_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        payload["output_file"] = str(output_file)
        return payload
    finally:
        database.engine.dispose()


def _output_payload(
    *,
    diagnostic: dict[str, Any],
    cube_rows: list[dict[str, Any]],
    candidate_config_id: str | None,
    exchange_ts_metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "diagnostic_run_id": diagnostic["diagnostic_run_id"],
        "diagnosis": diagnostic["diagnosis"],
        "confidence": diagnostic["confidence"],
        "rolling_cube_rows": len(cube_rows),
        "regime_summary": diagnostic["regime_summary"],
        "top_contours": _top_contours(cube_rows),
        "dead_contours": _dead_contours(cube_rows),
        "calibration_recommended": diagnostic["calibration_recommended"],
        "candidate_config_id": candidate_config_id,
        "exchange_ts_metadata": exchange_ts_metadata,
        "warnings": diagnostic["warnings"],
        "blocking_issues": diagnostic["blocking_issues"],
        "diagnostic": diagnostic,
    }


def _exchange_ts_metadata(session: Any, *, lookback_days: int) -> dict[str, Any]:
    since = datetime.now(tz=UTC) - timedelta(days=lookback_days)
    rows = list(
        session.execute(
            select(
                MarketMicrostructureSnapshot.exchange_ts,
                MarketMicrostructureSnapshot.freshness_basis,
                MarketMicrostructureSnapshot.strict_dual_freshness_eligible,
            ).where(MarketMicrostructureSnapshot.ts_utc >= since)
        )
    )
    basis: dict[str, int] = {}
    for row in rows:
        key = str(row.freshness_basis or "unknown")
        basis[key] = basis.get(key, 0) + 1
    trade_tape_sample_count = int(
        session.scalar(
            select(func.count())
            .select_from(MarketTradeSample)
            .where(MarketTradeSample.received_ts >= since)
        )
        or 0
    )
    return {
        "exchange_ts_present_count": sum(1 for row in rows if row.exchange_ts is not None),
        "exchange_ts_missing_count": sum(1 for row in rows if row.exchange_ts is None),
        "received_ts_only_count": basis.get("received_ts_only", 0),
        "strict_dual_freshness_eligible_count": sum(
            1 for row in rows if row.strict_dual_freshness_eligible
        ),
        "freshness_basis_distribution": basis,
        "trade_tape_sample_count": trade_tape_sample_count,
        "tape_confirmed_candidate_count": 0,
    }


def _top_contours(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            float(row.get("net_pnl_proxy") or 0),
            int(row.get("candidate_count") or 0),
        ),
        reverse=True,
    )[:10]


def _dead_contours(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("sample_warning") is not None
        or int(row.get("candidate_count") or 0) == 0
        or row.get("contour_status") in {"data_only", "research_only"}
    ][:20]


def _parse_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


if __name__ == "__main__":
    main()

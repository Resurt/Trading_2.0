"""Sync dividend corporate actions from T-Bank GetDividends."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.corporate_actions import DividendSyncConfig, DividendSyncService  # noqa: E402
from trade_core.infra.tbank import TBankBrokerGateway  # noqa: E402
from trade_core.runtime import SafeNoopBrokerGateway  # noqa: E402
from trading_common import RuntimeMode, parse_runtime_mode  # noqa: E402
from trading_common.db.base import Base  # noqa: E402
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.service import DatabaseService  # noqa: E402


def main() -> None:
    args = parse_args()
    database = DatabaseService(_database_url(args.database_url, dry_run=args.dry_run))
    broker_gateway = SafeNoopBrokerGateway() if args.dry_run else TBankBrokerGateway()
    runtime_mode = _runtime_mode(args.runtime_mode, dry_run=args.dry_run)
    config = DividendSyncConfig(
        instruments=split_csv(args.instruments),
        from_date=args.from_date,
        to_date=args.to_date,
        lookback_days=args.lookback_days,
        lookahead_days=args.lookahead_days,
        dry_run=args.dry_run,
        force_rebuild=args.force_rebuild,
        classify_special_days=args.classify_special_days,
        gap_threshold_bps=args.gap_threshold_bps,
        dividend_gap_threshold_bps=args.dividend_gap_threshold_bps,
        runtime_mode=runtime_mode.value,
        resolve_instruments=args.resolve_instruments,
        require_resolved_instruments=args.require_resolved_instruments,
    )
    try:
        if args.dry_run:
            Base.metadata.create_all(database.engine)
        with database.session_scope() as session:
            result = asyncio.run(
                DividendSyncService(
                    session=session,
                    broker_gateway=broker_gateway,
                ).run(config)
            )
            payload = result.as_payload()
    finally:
        database.engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    raise SystemExit(_exit_code(payload, allow_partial=args.allow_partial))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T")
    parser.add_argument("--from-date", type=parse_date)
    parser.add_argument("--to-date", type=parse_date)
    parser.add_argument("--lookback-days", type=int, default=730)
    parser.add_argument("--lookahead-days", type=int, default=365)
    parser.add_argument("--database-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--runtime-mode", default=None)
    parser.add_argument(
        "--resolve-instruments",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--require-resolved-instruments",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--classify-special-days",
        dest="classify_special_days",
        action="store_true",
    )
    parser.add_argument(
        "--no-classify-special-days",
        dest="classify_special_days",
        action="store_false",
    )
    parser.set_defaults(classify_special_days=True)
    parser.add_argument("--gap-threshold-bps", type=Decimal, default=Decimal("150"))
    parser.add_argument("--dividend-gap-threshold-bps", type=Decimal, default=Decimal("50"))
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _runtime_mode(value: str | None, *, dry_run: bool) -> RuntimeMode:
    if value:
        return parse_runtime_mode(value)
    return RuntimeMode.HISTORICAL_REPLAY if dry_run else RuntimeMode.SHADOW


def _database_url(value: str | None, *, dry_run: bool) -> str:
    if value:
        return value
    if dry_run:
        return "sqlite+pysqlite:///:memory:"
    return build_database_url_from_env()


def _exit_code(payload: dict[str, object], *, allow_partial: bool) -> int:
    status = str(payload.get("status", payload.get("dividend_sync_status", "")))
    clean = bool(payload.get("clean", payload.get("dividend_sync_clean", False)))
    instruments_processed = int(payload.get("instruments_processed", 0) or 0)
    error_count = int(payload.get("error_count", 0) or 0)
    if instruments_processed <= 0:
        return 7
    if clean and error_count == 0 and status in {"completed", "dry_run"}:
        return 0
    if allow_partial and status == "completed_with_errors":
        return 0
    return 7


if __name__ == "__main__":
    main()

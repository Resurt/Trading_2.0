"""Classify market special days from corporate actions and candle open gaps."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sys import path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.corporate_actions import (  # noqa: E402
    CorporateActionService,
    MarketSpecialDayClassifier,
)
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.service import DatabaseService  # noqa: E402


def main() -> None:
    args = parse_args()
    from_date, to_date = window(
        from_date=args.from_date,
        to_date=args.to_date,
        lookback_days=args.lookback_days,
    )
    database = DatabaseService(args.database_url or build_database_url_from_env())
    progress_events: list[dict[str, object]] = []
    try:
        with database.session_scope() as session:
            effective_to_date = (
                to_date + timedelta(days=args.lookahead_days) if args.include_future else to_date
            )
            if args.require_dividend_sync and not CorporateActionService(
                session
            ).api_imported_dividend_events_exist(
                from_date=from_date,
                to_date=effective_to_date,
                instruments=split_csv(args.instruments),
            ):
                payload = {
                    "passed": False,
                    "error_code": "dividend_sync_missing",
                    "from_date": from_date.isoformat(),
                    "to_date": effective_to_date.isoformat(),
                    "source": "market_special_day_classification",
                }
                print(
                    json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None)
                )
                raise SystemExit(6)
            result = MarketSpecialDayClassifier(session).classify(
                from_date=from_date,
                to_date=to_date,
                instruments=split_csv(args.instruments),
                gap_threshold_bps=args.gap_threshold_bps,
                dividend_gap_threshold_bps=args.dividend_gap_threshold_bps,
                force_rebuild=args.force_rebuild,
                include_future=args.include_future,
                lookahead_days=args.lookahead_days,
                skip_existing=args.skip_existing,
                chunk_days=args.chunk_days,
                progress_every=args.progress_every,
                progress_callback=progress_callback(args.progress_every, progress_events),
            )
            payload = result.as_payload()
            if progress_events:
                payload["progress_events"] = progress_events
    finally:
        database.engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", type=parse_date)
    parser.add_argument("--to-date", type=parse_date)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--instruments", default="SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T")
    parser.add_argument("--database-url")
    parser.add_argument("--gap-threshold-bps", type=Decimal, default=Decimal("150"))
    parser.add_argument("--dividend-gap-threshold-bps", type=Decimal, default=Decimal("50"))
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--include-future", action="store_true")
    parser.add_argument("--lookahead-days", type=int, default=365)
    parser.add_argument("--require-dividend-sync", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--chunk-days", type=int, default=30)
    parser.add_argument("--progress-every", type=int, default=0)
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def window(
    *,
    from_date: date | None,
    to_date: date | None,
    lookback_days: int,
) -> tuple[date, date]:
    end = to_date or datetime.now(tz=UTC).date()
    start = from_date or (end - timedelta(days=lookback_days - 1))
    if start > end:
        msg = "from_date must be <= to_date"
        raise ValueError(msg)
    return start, end


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def progress_callback(progress_every: int, progress_events: list[dict[str, object]]):
    if progress_every <= 0:
        return None
    started_at = monotonic()

    def _emit(payload: dict[str, object]) -> None:
        progress_events.append(
            {
                **payload,
                "elapsed_seconds": round(monotonic() - started_at, 3),
                "source": "market_special_day_classification_progress",
            }
        )

    return _emit


if __name__ == "__main__":
    main()

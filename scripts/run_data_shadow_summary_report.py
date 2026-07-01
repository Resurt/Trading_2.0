"""Build a lightweight data-only shadow microstructure summary report."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sys import path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import MarketMicrostructureSnapshot, MarketTradeSample
from trading_common.db.service import DatabaseService

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def main() -> None:
    args = parse_args()
    payload = run_report(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def run_report(args: argparse.Namespace) -> dict[str, object]:
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        since = datetime.now(tz=UTC) - timedelta(hours=args.lookback_hours)
        with database.session_scope() as session:
            rows = list(
                session.execute(
                    select(MarketMicrostructureSnapshot).where(
                        MarketMicrostructureSnapshot.ts_utc >= since
                    )
                ).scalars()
            )
            trade_tape_sample_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(MarketTradeSample)
                    .where(MarketTradeSample.received_ts >= since)
                )
                or 0
            )
        payload = summarize(
            rows,
            lookback_hours=args.lookback_hours,
            trade_tape_sample_count=trade_tape_sample_count,
        )
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "data_shadow_summary_latest.json"
        output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["output_file"] = str(output_file)
        return payload
    finally:
        database.engine.dispose()


def summarize(
    rows: list[MarketMicrostructureSnapshot],
    *,
    lookback_hours: int,
    trade_tape_sample_count: int = 0,
) -> dict[str, object]:
    eligible_rows, rejection_reasons = calibration_rows(rows)
    spreads = [row.spread_bps for row in rows if row.spread_bps is not None]
    bid_depth = [row.bid_depth_lots for row in rows if row.bid_depth_lots is not None]
    ask_depth = [row.ask_depth_lots for row in rows if row.ask_depth_lots is not None]
    imbalance = [row.book_imbalance for row in rows if row.book_imbalance is not None]
    quality = [row.market_quality_score for row in rows if row.market_quality_score is not None]
    eligible_spreads = [row.spread_bps for row in eligible_rows if row.spread_bps is not None]
    eligible_bid_depth = [
        row.bid_depth_lots for row in eligible_rows if row.bid_depth_lots is not None
    ]
    eligible_ask_depth = [
        row.ask_depth_lots for row in eligible_rows if row.ask_depth_lots is not None
    ]
    eligible_imbalance = [
        row.book_imbalance for row in eligible_rows if row.book_imbalance is not None
    ]
    eligible_quality = [
        row.market_quality_score for row in eligible_rows if row.market_quality_score is not None
    ]
    instruments = sorted({row.instrument_id for row in rows})
    sessions = sorted({row.session_type for row in rows})
    stream_gap_count = count_stream_gaps(rows)
    warnings: list[str] = []
    if rejection_reasons:
        warnings.append("some_rows_not_calibration_eligible")
    if rejection_reasons.get("late_after_session_close"):
        warnings.append("late_after_session_close_rows_excluded_from_calibration")
    if stream_gap_count:
        warnings.append("stream_gaps_detected")
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "lookback_hours": lookback_hours,
        "instruments": instruments,
        "sessions": sessions,
        "snapshots_count": len(rows),
        "exchange_ts_present_count": sum(1 for row in rows if row.exchange_ts is not None),
        "exchange_ts_missing_count": sum(1 for row in rows if row.exchange_ts is None),
        "received_ts_only_count": sum(
            1 for row in rows if row.freshness_basis == "received_ts_only"
        ),
        "strict_dual_freshness_eligible_count": sum(
            1 for row in rows if row.strict_dual_freshness_eligible
        ),
        "freshness_basis_distribution": dict(
            sorted(Counter(str(row.freshness_basis or "unknown") for row in rows).items())
        ),
        "trade_tape_sample_count": trade_tape_sample_count,
        "tape_confirmed_candidate_count": 0,
        "avg_spread_bps": _optional_decimal(avg(spreads)),
        "p50_spread_bps": _optional_decimal(percentile(spreads, 0.50)),
        "p95_spread_bps": _optional_decimal(percentile(spreads, 0.95)),
        "avg_bid_depth_lots": _optional_decimal(avg(bid_depth)),
        "p50_bid_depth_lots": _optional_decimal(percentile(bid_depth, 0.50)),
        "p95_bid_depth_lots": _optional_decimal(percentile(bid_depth, 0.95)),
        "avg_ask_depth_lots": _optional_decimal(avg(ask_depth)),
        "p50_ask_depth_lots": _optional_decimal(percentile(ask_depth, 0.50)),
        "p95_ask_depth_lots": _optional_decimal(percentile(ask_depth, 0.95)),
        "avg_imbalance": _optional_decimal(avg(imbalance)),
        "avg_market_quality_score": _optional_decimal(avg(quality)),
        "calibration_eligible_count": len(eligible_rows),
        "calibration_rejected_count": len(rows) - len(eligible_rows),
        "calibration_rejection_reasons": dict(rejection_reasons),
        "calibration_avg_spread_bps": _optional_decimal(avg(eligible_spreads)),
        "calibration_p95_spread_bps": _optional_decimal(percentile(eligible_spreads, 0.95)),
        "calibration_avg_bid_depth_lots": _optional_decimal(avg(eligible_bid_depth)),
        "calibration_avg_ask_depth_lots": _optional_decimal(avg(eligible_ask_depth)),
        "calibration_avg_imbalance": _optional_decimal(avg(eligible_imbalance)),
        "calibration_avg_market_quality_score": _optional_decimal(avg(eligible_quality)),
        "stale_data_incidents": sum(1 for row in rows if row.is_stale),
        "stream_gap_count": stream_gap_count,
        "candle_lag_p95": None,
        "market_quality_distribution": {
            "samples": len(quality),
            "p50": _optional_decimal(percentile(quality, 0.50)),
            "p95": _optional_decimal(percentile(quality, 0.95)),
        },
        "warnings": warnings,
    }


def calibration_rows(
    rows: list[MarketMicrostructureSnapshot],
) -> tuple[list[MarketMicrostructureSnapshot], Counter[str]]:
    eligible: list[MarketMicrostructureSnapshot] = []
    rejection_reasons: Counter[str] = Counter()
    for row in rows:
        reason = calibration_rejection_reason(row)
        if reason is None:
            eligible.append(row)
        else:
            rejection_reasons[reason] += 1
    return eligible, rejection_reasons


def calibration_rejection_reason(row: MarketMicrostructureSnapshot) -> str | None:
    payload = row.snapshot_payload if isinstance(row.snapshot_payload, dict) else {}
    if row.is_stale:
        return "stale"
    if not row.instrument_id:
        return "instrument_unknown"
    if not row.session_type or not row.session_phase:
        return "missing_session_context"
    if not inside_session_window(row):
        return "late_after_session_close"
    if row.best_bid is None or row.best_ask is None:
        return "no_bid_ask"
    if row.best_ask < row.best_bid:
        return "invalid_spread"
    if row.spread_abs is None or row.mid_price is None or row.spread_bps is None:
        return "invalid_spread"
    expected_spread = row.best_ask - row.best_bid
    expected_mid = (row.best_ask + row.best_bid) / Decimal("2")
    if expected_mid <= Decimal("0"):
        return "invalid_spread"
    expected_spread_bps = expected_spread / expected_mid * Decimal("10000")
    if abs(row.spread_abs - expected_spread) > Decimal("0.0001"):
        return "invalid_spread"
    if abs(row.mid_price - expected_mid) > Decimal("0.0001"):
        return "invalid_spread"
    if abs(row.spread_bps - expected_spread_bps) > Decimal("0.01"):
        return "invalid_spread"
    if row.bid_depth_lots is None or row.ask_depth_lots is None:
        return "no_depth"
    if row.bid_depth_lots < Decimal("0") or row.ask_depth_lots < Decimal("0"):
        return "invalid_depth"
    if row.book_imbalance is None or not (Decimal("-1") <= row.book_imbalance <= Decimal("1")):
        return "invalid_imbalance"
    if payload_bool(payload, "include_in_calibration") is False:
        return "calibration_flag_false"
    if payload_bool(payload, "calibration_allowed") is False:
        return "calibration_flag_false"
    source = str(row.source or payload.get("source") or "").lower()
    venue_type = str(payload.get("venue_type") or "").lower()
    if source.startswith("broker") or source in {
        "stale_local",
        "local_history",
        "latest_market_candle_close",
        "previous_close",
    }:
        return "source_not_allowed"
    if venue_type in {"broker_otc", "broker_indicative", "stale_local", "display_only"}:
        return "broker_otc_or_indicative"
    return None


def inside_session_window(row: MarketMicrostructureSnapshot) -> bool:
    if row.session_phase == "closed":
        return False
    ts_msk = ensure_utc(row.ts_utc).astimezone(MOSCOW_TZ)
    minutes = ts_msk.hour * 60 + ts_msk.minute
    if row.session_type == "weekend":
        return 10 * 60 <= minutes < 19 * 60
    if row.session_type == "weekday_morning":
        return 7 * 60 <= minutes < 10 * 60
    if row.session_type == "weekday_main":
        return 10 * 60 <= minutes < 19 * 60
    if row.session_type == "weekday_evening":
        return 19 * 60 <= minutes < 23 * 60 + 50
    return False


def count_stream_gaps(rows: list[MarketMicrostructureSnapshot]) -> int:
    by_instrument: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        if row.instrument_id:
            by_instrument[row.instrument_id].append(ensure_utc(row.ts_utc))
    gaps = 0
    for timestamps in by_instrument.values():
        ordered = sorted(timestamps)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if (current - previous).total_seconds() > 60:
                gaps += 1
    return gaps


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def payload_bool(payload: dict[str, object], key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    for nested in payload.values():
        if isinstance(nested, dict):
            nested_value = nested.get(key)
            if isinstance(nested_value, bool):
                return nested_value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument(
        "--output-dir",
        default=".local/collection_reports/data_shadow",
    )
    return parser.parse_args()


def avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.0001"))


def percentile(values: list[Decimal], pct: float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))
    return ordered[index].quantize(Decimal("0.0001"))


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


if __name__ == "__main__":
    main()

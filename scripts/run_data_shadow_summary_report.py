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
from typing import cast
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
    stream_gap_report = stream_gap_diagnostics(rows)
    stream_gap_count = cast(int, stream_gap_report["total_gap_count"])
    stream_gap_warning_count = cast(int, stream_gap_report["warning_gap_count"])
    eligibility_breakdown = calibration_eligibility_breakdown(
        rows,
        eligible_count=len(eligible_rows),
        rejection_reasons=rejection_reasons,
    )
    warnings: list[str] = []
    if rejection_reasons:
        warnings.append("some_rows_not_calibration_eligible")
    if rejection_reasons.get("late_after_session_close"):
        warnings.append("late_after_session_close_rows_excluded_from_calibration")
    if stream_gap_warning_count:
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
        "calibration_eligibility_breakdown": eligibility_breakdown,
        "strict_timestamp_eligible_count": eligibility_breakdown[
            "strict_timestamp_eligible_count"
        ],
        "diagnostic_eligible_count": eligibility_breakdown["diagnostic_eligible_count"],
        "strict_timestamp_eligible_but_calibration_rejected_count": eligibility_breakdown[
            "strict_timestamp_eligible_but_calibration_rejected_count"
        ],
        "calibration_avg_spread_bps": _optional_decimal(avg(eligible_spreads)),
        "calibration_p95_spread_bps": _optional_decimal(percentile(eligible_spreads, 0.95)),
        "calibration_avg_bid_depth_lots": _optional_decimal(avg(eligible_bid_depth)),
        "calibration_avg_ask_depth_lots": _optional_decimal(avg(eligible_ask_depth)),
        "calibration_avg_imbalance": _optional_decimal(avg(eligible_imbalance)),
        "calibration_avg_market_quality_score": _optional_decimal(avg(eligible_quality)),
        "stale_data_incidents": sum(1 for row in rows if row.is_stale),
        "stream_gap_count": stream_gap_count,
        "stream_gap_warning_count": stream_gap_warning_count,
        "stream_gap_info_count": cast(int, stream_gap_report["info_gap_count"]),
        "stream_gap_threshold_seconds": stream_gap_report["threshold_seconds"],
        "stream_gap_classification_counts": stream_gap_report["classification_counts"],
        "stream_gap_severity_counts": stream_gap_report["severity_counts"],
        "stream_gap_by_instrument": stream_gap_report["by_instrument"],
        "stream_gap_details_top": stream_gap_report["details_top"],
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


def calibration_eligibility_breakdown(
    rows: list[MarketMicrostructureSnapshot],
    *,
    eligible_count: int,
    rejection_reasons: Counter[str],
) -> dict[str, object]:
    diagnostic_rejections = Counter(
        reason
        for row in rows
        if (reason := diagnostic_rejection_reason(row)) is not None
    )
    strict_rejected = sum(
        1
        for row in rows
        if row.strict_dual_freshness_eligible and calibration_rejection_reason(row) is not None
    )
    return {
        "strict_timestamp_eligible_count": sum(
            1 for row in rows if row.strict_dual_freshness_eligible
        ),
        "diagnostic_eligible_count": len(rows) - sum(diagnostic_rejections.values()),
        "strict_calibration_eligible_count": eligible_count,
        "tape_confirmed_eligible_count": 0,
        "calibration_rejected_count": len(rows) - eligible_count,
        "strict_timestamp_eligible_but_calibration_rejected_count": strict_rejected,
        "calibration_rejection_reasons": dict(rejection_reasons),
        "diagnostic_rejection_reasons": dict(diagnostic_rejections),
    }


def calibration_rejection_reason(row: MarketMicrostructureSnapshot) -> str | None:
    return _microstructure_rejection_reason(row, reject_stale=True)


def diagnostic_rejection_reason(row: MarketMicrostructureSnapshot) -> str | None:
    return _microstructure_rejection_reason(row, reject_stale=False)


def _microstructure_rejection_reason(
    row: MarketMicrostructureSnapshot,
    *,
    reject_stale: bool,
) -> str | None:
    payload = row.snapshot_payload if isinstance(row.snapshot_payload, dict) else {}
    if reject_stale and row.is_stale:
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
    return cast(int, stream_gap_diagnostics(rows)["total_gap_count"])


def stream_gap_diagnostics(
    rows: list[MarketMicrostructureSnapshot],
    *,
    threshold_seconds: int = 60,
) -> dict[str, object]:
    by_instrument: dict[str, list[MarketMicrostructureSnapshot]] = defaultdict(list)
    for row in rows:
        if row.instrument_id:
            by_instrument[row.instrument_id].append(row)
    details: list[dict[str, object]] = []
    for instrument_id, instrument_rows in by_instrument.items():
        ordered = sorted(instrument_rows, key=lambda row: ensure_utc(row.ts_utc))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            previous_ts = ensure_utc(previous.ts_utc)
            current_ts = ensure_utc(current.ts_utc)
            gap_seconds = (current_ts - previous_ts).total_seconds()
            if gap_seconds <= threshold_seconds:
                continue
            classification, severity = classify_stream_gap(
                previous,
                current,
                row_count=len(ordered),
            )
            details.append(
                {
                    "instrument_id": instrument_id,
                    "previous_ts_utc": previous_ts.isoformat(),
                    "current_ts_utc": current_ts.isoformat(),
                    "gap_seconds": round(gap_seconds, 3),
                    "previous_session_type": previous.session_type,
                    "current_session_type": current.session_type,
                    "previous_session_phase": previous.session_phase,
                    "current_session_phase": current.session_phase,
                    "previous_source": previous.source,
                    "current_source": current.source,
                    "previous_is_stale": bool(previous.is_stale),
                    "current_is_stale": bool(current.is_stale),
                    "classification": classification,
                    "severity": severity,
                }
            )
    classification_counts = Counter(str(item["classification"]) for item in details)
    severity_counts = Counter(str(item["severity"]) for item in details)
    by_instrument_payload: dict[str, dict[str, object]] = {}
    for instrument_id, instrument_rows in sorted(by_instrument.items()):
        instrument_details = [
            item for item in details if item["instrument_id"] == instrument_id
        ]
        gap_values = sorted(_object_to_float(item["gap_seconds"]) for item in instrument_details)
        by_instrument_payload[instrument_id] = {
            "rows": len(instrument_rows),
            "gap_count": len(instrument_details),
            "warning_gap_count": sum(
                1 for item in instrument_details if item["severity"] == "warning"
            ),
            "max_gap_seconds": max(gap_values) if gap_values else 0,
            "p95_gap_seconds": percentile_float(gap_values, 0.95),
            "classifications": dict(
                Counter(str(item["classification"]) for item in instrument_details)
            ),
        }
    return {
        "threshold_seconds": threshold_seconds,
        "total_gap_count": len(details),
        "warning_gap_count": severity_counts.get("warning", 0),
        "info_gap_count": severity_counts.get("info", 0),
        "classification_counts": dict(classification_counts),
        "severity_counts": dict(severity_counts),
        "by_instrument": by_instrument_payload,
        "details_top": sorted(
            details,
            key=lambda item: _object_to_float(item["gap_seconds"]),
            reverse=True,
        )[:20],
    }


def classify_stream_gap(
    previous: MarketMicrostructureSnapshot,
    current: MarketMicrostructureSnapshot,
    *,
    row_count: int,
) -> tuple[str, str]:
    if previous.session_phase == "closed" or current.session_phase == "closed":
        return "session_closed_gap", "info"
    if (
        previous.session_type != current.session_type
        or previous.session_phase != current.session_phase
    ):
        return "session_boundary_gap", "info"
    if row_count < 5 and str(previous.instrument_id).startswith("MOEX:"):
        return "sparse_identifier_gap", "info"
    if previous.source != current.source:
        return "source_switch_gap", "info"
    if previous.is_stale or current.is_stale:
        return "stale_snapshot_gap", "warning"
    return "real_stream_gap", "warning"


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


def percentile_float(values: list[float], pct: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))
    return round(ordered[index], 3)


def _object_to_float(value: object) -> float:
    if isinstance(value, int | float | str):
        return float(value)
    return 0.0


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


if __name__ == "__main__":
    main()

"""Read-only daily trend calibration diagnostics from data-only microstructure rows.

The script builds pseudo-bars from `market_microstructure_snapshot` and evaluates
retrospective long/short forward-return windows. It never writes trading domain
entities, never mutates strategy_config, and never calls broker APIs.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sys import path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
for src in (ROOT, ROOT / "packages" / "common" / "src"):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import (
    InstrumentRegistry,
    MarketMicrostructureSnapshot,
    MarketTradeSample,
)
from trading_common.db.service import DatabaseService

MSK = ZoneInfo("Europe/Moscow")
TEN_THOUSAND = Decimal("10000")
DEFAULT_HORIZON_MINUTES = 15
EXIT_TOLERANCE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class PricePoint:
    instrument_id: str
    session_type: str
    ts_utc: datetime
    ts_msk: datetime
    mid_price: Decimal
    spread_bps: Decimal | None
    bid_depth_lots: Decimal | None
    ask_depth_lots: Decimal | None
    imbalance: Decimal | None


@dataclass(frozen=True, slots=True)
class PseudoBar:
    instrument_id: str
    session_type: str
    timeframe_minutes: int
    open_ts_utc: datetime
    open_ts_msk: datetime
    close_ts_utc: datetime
    open_mid: Decimal
    high_mid: Decimal
    low_mid: Decimal
    close_mid: Decimal
    avg_spread_bps: Decimal
    samples_count: int


@dataclass(frozen=True, slots=True)
class ForwardWindow:
    instrument: str
    session_type: str
    timeframe_minutes: int
    side: str
    entry_ts_utc: datetime
    entry_ts_msk: datetime
    requested_horizon_minutes: int
    target_exit_ts_utc: datetime
    actual_exit_ts_utc: datetime | None
    actual_horizon_minutes: float | None
    exit_alignment_seconds: float | None
    horizon_valid: bool
    entry_mid: Decimal
    exit_mid: Decimal | None
    gross_bps: Decimal | None
    estimated_cost_bps: Decimal
    net_bps_proxy: Decimal | None
    rejection_reason: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--instruments", required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    instruments = _normalize_instruments(args.instruments)
    database = DatabaseService(args.database_url or build_database_url_from_env())
    with database.session_scope() as session:
        aliases = _instrument_aliases(session, instruments)
        query_instruments = tuple(dict.fromkeys((*instruments, *aliases.keys())))
        rows = list(
            session.execute(
                select(MarketMicrostructureSnapshot)
                .where(MarketMicrostructureSnapshot.trading_date == args.date)
                .where(MarketMicrostructureSnapshot.instrument_id.in_(query_instruments))
                .order_by(
                    MarketMicrostructureSnapshot.instrument_id,
                    MarketMicrostructureSnapshot.ts_utc,
                )
            ).scalars()
        )
        trade_tape_sample_count = int(
            session.scalar(
                select(func.count())
                .select_from(MarketTradeSample)
                .where(MarketTradeSample.trading_date == args.date)
            )
            or 0
        )
    points = _price_points(rows, aliases)
    bars = _build_pseudo_bars(points, timeframes=(1, 5, 10, 15))
    windows = _forward_windows(points, bars)
    known = _known_window_validation(points, bars)
    payload = _payload(
        target_date=args.date,
        requested_instruments=instruments,
        rows=rows,
        points=points,
        bars=bars,
        windows=windows,
        known_windows=known,
        trade_tape_sample_count=trade_tape_sample_count,
    )
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    else:
        print(json.dumps(payload, ensure_ascii=False, default=_json_default))


def _normalize_instruments(raw: str) -> tuple[str, ...]:
    values: list[str] = []
    for item in raw.split(","):
        ticker = item.strip()
        if not ticker:
            continue
        if ":" in ticker:
            values.append(ticker.upper())
            values.append(ticker.rsplit(":", 1)[-1].upper())
        else:
            values.append(ticker.upper())
            values.append(f"MOEX:{ticker.upper()}")
    return tuple(dict.fromkeys(values))


def _instrument_aliases(session: Any, requested_instruments: tuple[str, ...]) -> dict[str, str]:
    requested_tickers = {_ticker_symbol(item) for item in requested_instruments}
    aliases: dict[str, str] = {}
    rows = session.execute(select(InstrumentRegistry)).scalars()
    for row in rows:
        ticker = str(row.ticker or "").upper()
        if ticker not in requested_tickers:
            continue
        for value in (
            row.instrument_id,
            row.instrument_uid,
            row.figi,
            row.ticker,
            f"MOEX:{ticker}",
        ):
            if value:
                aliases[str(value)] = ticker
    for item in requested_instruments:
        aliases[item] = _ticker_symbol(item)
    return aliases


def _price_points(
    rows: list[MarketMicrostructureSnapshot],
    aliases: Mapping[str, str],
) -> list[PricePoint]:
    points: list[PricePoint] = []
    for row in rows:
        mid = row.mid_price
        if mid is None and row.best_bid is not None and row.best_ask is not None:
            mid = (row.best_bid + row.best_ask) / Decimal("2")
        if mid is None or mid <= 0 or row.is_stale:
            continue
        if row.spread_bps is not None and row.spread_bps < 0:
            continue
        if row.spread_abs is not None and row.spread_abs < 0:
            continue
        points.append(
            PricePoint(
                instrument_id=aliases.get(row.instrument_id, row.instrument_id),
                session_type=row.session_type,
                ts_utc=_ensure_utc(row.ts_utc),
                ts_msk=_ensure_utc(row.ts_utc).astimezone(MSK),
                mid_price=mid,
                spread_bps=row.spread_bps,
                bid_depth_lots=row.bid_depth_lots,
                ask_depth_lots=row.ask_depth_lots,
                imbalance=row.book_imbalance,
            )
        )
    return points


def _build_pseudo_bars(
    points: list[PricePoint],
    *,
    timeframes: tuple[int, ...],
) -> list[PseudoBar]:
    buckets: dict[tuple[str, str, int, datetime], list[PricePoint]] = defaultdict(list)
    for point in points:
        for timeframe in timeframes:
            open_ts_msk = _bucket_open(point.ts_msk, timeframe)
            buckets[
                (
                    point.instrument_id,
                    point.session_type,
                    timeframe,
                    open_ts_msk.astimezone(UTC),
                )
            ].append(point)
    bars: list[PseudoBar] = []
    for (instrument_id, session_type, timeframe, open_ts_utc), bucket_points in buckets.items():
        ordered = sorted(bucket_points, key=lambda item: item.ts_utc)
        open_ts_msk = open_ts_utc.astimezone(MSK)
        close_ts_utc = (open_ts_msk + timedelta(minutes=timeframe)).astimezone(UTC)
        spreads = [item.spread_bps for item in ordered if item.spread_bps is not None]
        bars.append(
            PseudoBar(
                instrument_id=instrument_id,
                session_type=session_type,
                timeframe_minutes=timeframe,
                open_ts_utc=open_ts_utc,
                open_ts_msk=open_ts_msk,
                close_ts_utc=close_ts_utc,
                open_mid=ordered[0].mid_price,
                high_mid=max(item.mid_price for item in ordered),
                low_mid=min(item.mid_price for item in ordered),
                close_mid=ordered[-1].mid_price,
                avg_spread_bps=(
                    sum(spreads, Decimal("0")) / Decimal(len(spreads))
                    if spreads
                    else Decimal("0")
                ),
                samples_count=len(ordered),
            )
        )
    return sorted(
        bars,
        key=lambda item: (item.instrument_id, item.timeframe_minutes, item.open_ts_utc),
    )


def _forward_windows(points: list[PricePoint], bars: list[PseudoBar]) -> list[ForwardWindow]:
    points_by_scope: dict[tuple[str, str], list[PricePoint]] = defaultdict(list)
    for point in points:
        points_by_scope[(point.instrument_id, point.session_type)].append(point)
    for scope_points in points_by_scope.values():
        scope_points.sort(key=lambda item: item.ts_utc)

    windows: list[ForwardWindow] = []
    for bar in bars:
        if bar.samples_count < 1:
            continue
        scope_points = points_by_scope[(bar.instrument_id, bar.session_type)]
        for side in ("long", "short"):
            windows.append(_forward_window(bar, scope_points, side=side))
    return windows


def _forward_window(bar: PseudoBar, points: list[PricePoint], *, side: str) -> ForwardWindow:
    target_exit_ts = bar.open_ts_utc + timedelta(minutes=DEFAULT_HORIZON_MINUTES)
    exit_point, alignment_seconds = _nearest_point(points, target_exit_ts)
    if (
        exit_point is None
        or alignment_seconds is None
        or abs(alignment_seconds) > EXIT_TOLERANCE_SECONDS
    ):
        return ForwardWindow(
            instrument=bar.instrument_id,
            session_type=bar.session_type,
            timeframe_minutes=bar.timeframe_minutes,
            side=side,
            entry_ts_utc=bar.open_ts_utc,
            entry_ts_msk=bar.open_ts_msk,
            requested_horizon_minutes=DEFAULT_HORIZON_MINUTES,
            target_exit_ts_utc=target_exit_ts,
            actual_exit_ts_utc=exit_point.ts_utc if exit_point is not None else None,
            actual_horizon_minutes=(
                (exit_point.ts_utc - bar.open_ts_utc).total_seconds() / 60.0
                if exit_point is not None
                else None
            ),
            exit_alignment_seconds=alignment_seconds,
            horizon_valid=False,
            entry_mid=bar.open_mid,
            exit_mid=exit_point.mid_price if exit_point is not None else None,
            gross_bps=None,
            estimated_cost_bps=_estimated_cost_bps(bar),
            net_bps_proxy=None,
            rejection_reason="horizon_mismatch",
        )
    gross = _return_bps(bar.open_mid, exit_point.mid_price)
    if side == "short":
        gross = -gross
    cost = _estimated_cost_bps(bar)
    net = gross - cost
    return ForwardWindow(
        instrument=bar.instrument_id,
        session_type=bar.session_type,
        timeframe_minutes=bar.timeframe_minutes,
        side=side,
        entry_ts_utc=bar.open_ts_utc,
        entry_ts_msk=bar.open_ts_msk,
        requested_horizon_minutes=DEFAULT_HORIZON_MINUTES,
        target_exit_ts_utc=target_exit_ts,
        actual_exit_ts_utc=exit_point.ts_utc,
        actual_horizon_minutes=(exit_point.ts_utc - bar.open_ts_utc).total_seconds() / 60.0,
        exit_alignment_seconds=alignment_seconds,
        horizon_valid=True,
        entry_mid=bar.open_mid,
        exit_mid=exit_point.mid_price,
        gross_bps=gross,
        estimated_cost_bps=cost,
        net_bps_proxy=net,
        rejection_reason=None,
    )


def _known_window_validation(
    points: list[PricePoint],
    bars: list[PseudoBar],
) -> list[dict[str, Any]]:
    specs = (
        ("TATN", "weekday_main", 10, "12:20", "long"),
        ("TATN", "weekday_main", 10, "16:30", "short"),
        ("OZON", "weekday_main", 10, "16:40", "short"),
        ("TATN", "weekday_main", 1, "16:44", "short"),
        ("TATN", "weekday_main", 5, "16:40", "short"),
    )
    points_by_scope: dict[tuple[str, str], list[PricePoint]] = defaultdict(list)
    for point in points:
        points_by_scope[(point.instrument_id, point.session_type)].append(point)
    bar_index = {
        (
            _ticker_symbol(item.instrument_id),
            item.session_type,
            item.timeframe_minutes,
            item.open_ts_msk.strftime("%H:%M"),
        ): item
        for item in bars
    }
    results: list[dict[str, Any]] = []
    for ticker, session_type, timeframe, hhmm, side in specs:
        bar = bar_index.get((ticker, session_type, timeframe, hhmm))
        if bar is None:
            results.append(
                {
                    "window": f"{ticker} {session_type} {timeframe}m {hhmm} {side}",
                    "found": False,
                    "horizon_valid": False,
                    "artifact_excluded": True,
                    "reason": "bar_not_found",
                }
            )
            continue
        fixed = _forward_window(
            bar,
            points_by_scope[(bar.instrument_id, session_type)],
            side=side,
        )
        old_bucket_exit = _old_bucket_exit(bar, bars)
        old_horizon_minutes = (
            (old_bucket_exit.close_ts_utc - bar.open_ts_utc).total_seconds() / 60.0
            if old_bucket_exit is not None
            else None
        )
        old_bucket_artifact = old_horizon_minutes not in (None, DEFAULT_HORIZON_MINUTES)
        results.append(
            {
                "window": f"{ticker} {session_type} {timeframe}m {hhmm} {side}",
                "found": True,
                "requested_horizon_minutes": DEFAULT_HORIZON_MINUTES,
                "old_bucket_actual_horizon_minutes": old_horizon_minutes,
                "actual_horizon_minutes": fixed.actual_horizon_minutes,
                "exit_alignment_seconds": fixed.exit_alignment_seconds,
                "horizon_valid": fixed.horizon_valid,
                "old_bucket_artifact_excluded": old_bucket_artifact,
                "artifact_excluded": old_bucket_artifact,
                "fixed_window": asdict(fixed),
            }
        )
    return results


def _old_bucket_exit(bar: PseudoBar, bars: list[PseudoBar]) -> PseudoBar | None:
    target_exit_ts = bar.open_ts_utc + timedelta(minutes=DEFAULT_HORIZON_MINUTES)
    candidates = [
        item
        for item in bars
        if item.instrument_id == bar.instrument_id
        and item.session_type == bar.session_type
        and item.timeframe_minutes == bar.timeframe_minutes
        and item.close_ts_utc >= target_exit_ts
    ]
    return min(candidates, key=lambda item: item.close_ts_utc) if candidates else None


def _ticker_symbol(instrument_id: str) -> str:
    return instrument_id.rsplit(":", 1)[-1].upper()


def _payload(
    *,
    target_date: date,
    requested_instruments: tuple[str, ...],
    rows: list[MarketMicrostructureSnapshot],
    points: list[PricePoint],
    bars: list[PseudoBar],
    windows: list[ForwardWindow],
    known_windows: list[dict[str, Any]],
    trade_tape_sample_count: int,
) -> dict[str, Any]:
    valid = [
        window for window in windows if window.horizon_valid and window.net_bps_proxy is not None
    ]
    invalid = [window for window in windows if not window.horizon_valid]
    top = sorted(valid, key=lambda item: item.net_bps_proxy or Decimal("-999999"), reverse=True)
    worst = sorted(valid, key=lambda item: item.net_bps_proxy or Decimal("999999"))
    return {
        "status": "ok",
        "source": "daily_trend_calibration_analysis",
        "date": target_date.isoformat(),
        "real_orders_disabled": True,
        "broker_calls_disabled": True,
        "shadow_runtime_started": False,
        "strategy_config_mutated": False,
        "requested_instruments": list(requested_instruments),
        "rows_read": len(rows),
        "exchange_ts_present_count": sum(1 for row in rows if row.exchange_ts is not None),
        "exchange_ts_missing_count": sum(1 for row in rows if row.exchange_ts is None),
        "received_ts_only_count": sum(
            1 for row in rows if row.freshness_basis == "received_ts_only"
        ),
        "strict_dual_freshness_eligible_count": sum(
            1 for row in rows if row.strict_dual_freshness_eligible
        ),
        "freshness_basis_distribution": dict(
            sorted(
                {
                    str(row.freshness_basis or "unknown"): sum(
                        1
                        for candidate in rows
                        if str(candidate.freshness_basis or "unknown")
                        == str(row.freshness_basis or "unknown")
                    )
                    for row in rows
                }.items()
            )
        ),
        "trade_tape_sample_count": trade_tape_sample_count,
        "tape_confirmed_candidate_count": 0,
        "valid_price_points": len(points),
        "pseudo_bars_count": len(bars),
        "forward_windows_count": len(windows),
        "horizon_mismatch_count": len(invalid),
        "artifact_windows_excluded": any(
            bool(item.get("artifact_excluded")) for item in known_windows
        ),
        "known_window_validation": known_windows,
        "top_windows": [asdict(item) for item in top[:20]],
        "worst_windows": [asdict(item) for item in worst[:20]],
        "summary_by_instrument_session_side": _summary(valid),
        "warnings": [
            "retrospective calibration research only",
            "trade tape confirmation still required for executable conclusions",
        ],
    }


def _summary(windows: list[ForwardWindow]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[ForwardWindow]] = defaultdict(list)
    for window in windows:
        grouped[(window.instrument, window.session_type, window.side)].append(window)
    result: list[dict[str, Any]] = []
    for (instrument, session_type, side), items in sorted(grouped.items()):
        values = [item.net_bps_proxy for item in items if item.net_bps_proxy is not None]
        if not values:
            continue
        result.append(
            {
                "instrument": instrument,
                "session_type": session_type,
                "side": side,
                "candidates_count": len(values),
                "avg_net_bps_proxy": sum(values, Decimal("0")) / Decimal(len(values)),
                "best_net_bps_proxy": max(values),
                "worst_net_bps_proxy": min(values),
                "hit_rate_net_positive": (
                    sum(1 for value in values if value > 0) / len(values)
                ),
            }
        )
    return result


def _nearest_point(
    points: list[PricePoint],
    target_ts: datetime,
) -> tuple[PricePoint | None, float | None]:
    if not points:
        return None, None
    best = min(points, key=lambda item: abs((item.ts_utc - target_ts).total_seconds()))
    return best, (best.ts_utc - target_ts).total_seconds()


def _bucket_open(ts_msk: datetime, minutes: int) -> datetime:
    floored_minute = (ts_msk.minute // minutes) * minutes
    return ts_msk.replace(minute=floored_minute, second=0, microsecond=0)


def _estimated_cost_bps(bar: PseudoBar) -> Decimal:
    return max(bar.avg_spread_bps, Decimal("0")) + Decimal("10")


def _return_bps(start: Decimal, end: Decimal) -> Decimal:
    if start <= 0:
        return Decimal("0")
    return ((end - start) / start) * TEN_THOUSAND


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.0001")))
    return str(value)


if __name__ == "__main__":
    main()

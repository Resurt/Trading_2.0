"""Run candle-only feature research on persisted market_candle rows.

This script is intentionally offline/research-only. It reads historical candles
from Postgres, evaluates simple candle-feature hypotheses, and writes reports
under `.local/collection_reports`. It never calls broker APIs and never writes
strategy_config/domain trading events.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from sys import path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT,
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import InstrumentRegistry, MarketCandle, MarketSpecialDay
from trading_common.db.service import DatabaseService

TEN_THOUSAND = 10_000.0
MIN_ROUND_TRIP_FEE_BPS = 10.0
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class CandlePoint:
    instrument_id: str
    timeframe: str
    trading_date: date
    session_type: str
    open_ts_utc: datetime
    close_ts_utc: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume_lots: float
    is_special_day: bool = False
    special_day_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HorizonOutcome:
    requested_horizon_minutes: int
    target_exit_ts_utc: datetime
    actual_exit_ts_utc: datetime | None
    actual_horizon_minutes: float | None
    exit_alignment_seconds: float | None
    horizon_valid: bool
    future_return_bps: float | None
    mfe_bps: float | None
    mae_bps: float | None
    exit_alignment: str


@dataclass(frozen=True, slots=True)
class OutcomeSet:
    future_return_5m_bps: float | None
    future_return_10m_bps: float | None
    future_return_15m_bps: float | None
    mfe_5m_bps: float | None
    mfe_10m_bps: float | None
    mfe_15m_bps: float | None
    mae_5m_bps: float | None
    mae_10m_bps: float | None
    mae_15m_bps: float | None
    horizon_5m: HorizonOutcome | None = None
    horizon_10m: HorizonOutcome | None = None
    horizon_15m: HorizonOutcome | None = None

    def gross_for_horizon(self, horizon_minutes: int) -> float | None:
        detail = self.detail_for_horizon(horizon_minutes)
        if detail is not None and not detail.horizon_valid:
            return None
        return {
            5: self.future_return_5m_bps,
            10: self.future_return_10m_bps,
            15: self.future_return_15m_bps,
        }[horizon_minutes]

    def gross_for_side(self, horizon_minutes: int, side: str) -> float | None:
        gross = self.gross_for_horizon(horizon_minutes)
        if gross is None:
            return None
        return -gross if side == "short" else gross

    def detail_for_horizon(self, horizon_minutes: int) -> HorizonOutcome | None:
        return {
            5: self.horizon_5m,
            10: self.horizon_10m,
            15: self.horizon_15m,
        }[horizon_minutes]


@dataclass(frozen=True, slots=True)
class FeatureRow:
    instrument_id: str
    timeframe: str
    trading_date: date
    session_type: str
    close_ts_utc: datetime
    hour_msk: int
    day_of_week: int
    is_morning: bool
    is_main: bool
    is_evening: bool
    special_day: bool
    dividend_or_corporate_action: bool
    return_1_bar_bps: float
    return_2_bar_bps: float
    return_3_bar_bps: float
    return_6_bar_bps: float
    trend_slope_3: float
    trend_slope_6: float
    close_vs_sma_3_bps: float
    close_vs_sma_6_bps: float
    close_vs_sma_12_bps: float
    range_bps: float
    avg_range_3_bps: float
    avg_range_6_bps: float
    volatility_6_bps: float
    volatility_12_bps: float
    abnormal_range_flag: bool
    volume_lots: float
    volume_ratio_vs_6: float
    volume_ratio_vs_12: float
    low_volume_flag: bool
    close_position_in_range: float
    outcomes: OutcomeSet


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    config_id: str
    hypothesis: str
    horizon_minutes: int
    side: str = "long"
    return_bars: int | None = None
    return_threshold_bps: float | None = None
    slope_threshold_bps: float | None = None
    breakout_multiplier: float | None = None
    compression_max_avg_range_bps: float | None = None
    max_volatility_12_bps: float | None = None
    edge_margin_bps: float = 5.0
    instruments: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    short_available_by_broker: bool = True
    short_requires_broker_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    candidates: int
    gross_pnl_bps_proxy: float
    net_pnl_bps_proxy: float
    average_net_bps_proxy: float
    win_proxy: float
    active_days: int
    max_bad_day_bps_proxy: float
    top_day_contribution: float


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    config: ResearchConfig
    train: EvaluationMetrics
    validation: EvaluationMetrics
    full: EvaluationMetrics
    passed: bool
    rejection_reasons: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", type=parse_date)
    parser.add_argument("--to-date", type=parse_date)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--instruments", default="SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T")
    parser.add_argument("--timeframes", default="5m,10m,15m")
    parser.add_argument(
        "--sessions",
        default="weekday_morning,weekday_main,weekday_evening",
    )
    parser.add_argument("--commission-bps-per-side", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--max-configs", type=int, default=100)
    parser.add_argument("--min-validation-candidates", type=int, default=100)
    parser.add_argument("--sides", default="long")
    parser.add_argument("--allow-short-only-if-broker-short-available", action="store_true")
    parser.add_argument("--exclude-dividend-windows-for-shorts", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--output-dir", default=".local/collection_reports/365d")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    instruments = normalize_instruments(args.instruments)
    timeframes = parse_csv(args.timeframes)
    sessions = parse_csv(args.sessions)
    sides = parse_sides(args.sides)
    total_cost = total_cost_bps(args.commission_bps_per_side, args.slippage_bps)
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            from_date, to_date = resolve_period(
                session,
                instruments=instruments,
                timeframes=timeframes,
                from_date=args.from_date,
                to_date=args.to_date,
                lookback_days=args.lookback_days,
            )
            special_days = load_special_days(
                session,
                from_date=from_date,
                to_date=to_date,
                instruments=instruments,
            )
            candles = load_candles(
                session,
                from_date=from_date,
                to_date=to_date,
                instruments=instruments,
                timeframes=timeframes,
                sessions=sessions,
                special_days=special_days,
            )
            short_available_by_instrument = load_short_availability(
                session,
                instruments=instruments,
            )
    finally:
        database.engine.dispose()

    features = compute_feature_rows(candles, selected_timeframes=set(timeframes))
    configs = generate_research_configs(
        instruments=instruments,
        sides=sides,
        max_configs=args.max_configs,
        short_available_by_instrument=short_available_by_instrument,
        enforce_short_availability=args.allow_short_only_if_broker_short_available,
    )
    train_dates, validation_dates = split_trading_dates(
        [feature.trading_date for feature in features]
    )
    results = evaluate_configs(
        features,
        configs=configs,
        train_dates=train_dates,
        validation_dates=validation_dates,
        total_cost_bps_value=total_cost,
        min_validation_candidates=args.min_validation_candidates,
        short_available_by_instrument=short_available_by_instrument,
        enforce_short_availability=args.allow_short_only_if_broker_short_available,
    )
    payload = build_report_payload(
        features=features,
        configs=configs,
        results=results,
        from_date=from_date,
        to_date=to_date,
        instruments=instruments,
        timeframes=timeframes,
        sessions=sessions,
        total_cost=total_cost,
        sides=sides,
        short_available_by_instrument=short_available_by_instrument,
        enforce_short_availability=args.allow_short_only_if_broker_short_available,
        exclude_dividend_windows_for_shorts=args.exclude_dividend_windows_for_shorts,
        train_dates=train_dates,
        validation_dates=validation_dates,
        dry_run=args.dry_run,
    )
    output_dir = Path(args.output_dir)
    write_reports(payload, output_dir=output_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_sides(value: str) -> tuple[str, ...]:
    sides = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    invalid = sorted(set(sides) - {"long", "short"})
    if invalid:
        msg = "Unsupported research sides: " + ", ".join(invalid)
        raise ValueError(msg)
    return sides or ("long",)


def normalize_instruments(value: str) -> tuple[str, ...]:
    instruments: list[str] = []
    for item in parse_csv(value):
        if ":" in item:
            instruments.append(item)
        else:
            instruments.append(f"MOEX:{item.upper()}")
    return tuple(dict.fromkeys(instruments))


def load_short_availability(
    session: Any,
    *,
    instruments: tuple[str, ...],
) -> dict[str, bool]:
    rows = session.execute(
        select(
            InstrumentRegistry.instrument_id,
            InstrumentRegistry.instrument_payload,
            InstrumentRegistry.broker_payload,
        ).where(InstrumentRegistry.instrument_id.in_(instruments))
    ).all()
    return {
        str(instrument_id): _short_available_from_payloads(instrument_payload, broker_payload)
        for instrument_id, instrument_payload, broker_payload in rows
    }


def _short_available_from_payloads(*payloads: Any) -> bool:
    for payload in payloads:
        if isinstance(payload, dict) and "short_available" in payload:
            return bool(payload.get("short_available"))
    return False


def resolve_period(
    session: Any,
    *,
    instruments: tuple[str, ...],
    timeframes: tuple[str, ...],
    from_date: date | None,
    to_date: date | None,
    lookback_days: int,
) -> tuple[date, date]:
    if to_date is None:
        to_date = session.scalar(
            select(func.max(MarketCandle.trading_date)).where(
                MarketCandle.instrument_id.in_(instruments),
                MarketCandle.timeframe.in_(set(timeframes) | {"5m"}),
                MarketCandle.is_closed.is_(True),
            )
        )
    if to_date is None:
        to_date = datetime.now(tz=UTC).date()
    if from_date is None:
        from_date = to_date - timedelta(days=max(lookback_days - 1, 0))
    return from_date, to_date


def load_special_days(
    session: Any,
    *,
    from_date: date,
    to_date: date,
    instruments: tuple[str, ...],
) -> dict[tuple[date, str], tuple[str, ...]]:
    rows = session.execute(
        select(
            MarketSpecialDay.trading_date,
            MarketSpecialDay.instrument_id,
            MarketSpecialDay.special_day_type,
        ).where(
            MarketSpecialDay.trading_date >= from_date,
            MarketSpecialDay.trading_date <= to_date,
            MarketSpecialDay.instrument_id.in_(instruments),
            MarketSpecialDay.exclude_from_primary_calibration.is_(True),
        )
    ).all()
    grouped: dict[tuple[date, str], list[str]] = defaultdict(list)
    for trading_date, instrument_id, special_day_type in rows:
        grouped[(trading_date, instrument_id)].append(str(special_day_type))
    return {key: tuple(sorted(set(values))) for key, values in grouped.items()}


def load_candles(
    session: Any,
    *,
    from_date: date,
    to_date: date,
    instruments: tuple[str, ...],
    timeframes: tuple[str, ...],
    sessions: tuple[str, ...],
    special_days: Mapping[tuple[date, str], tuple[str, ...]],
) -> list[CandlePoint]:
    query_timeframes = tuple(sorted(set(timeframes) | {"5m"}))
    rows = session.execute(
        select(MarketCandle)
        .where(
            MarketCandle.trading_date >= from_date,
            MarketCandle.trading_date <= to_date,
            MarketCandle.instrument_id.in_(instruments),
            MarketCandle.timeframe.in_(query_timeframes),
            MarketCandle.session_type.in_(sessions),
            MarketCandle.is_closed.is_(True),
        )
        .order_by(
            MarketCandle.instrument_id,
            MarketCandle.timeframe,
            MarketCandle.open_ts_utc,
        )
    ).scalars()
    candles: list[CandlePoint] = []
    for row in rows:
        key = (row.trading_date, row.instrument_id)
        types = special_days.get(key, ())
        candles.append(
            CandlePoint(
                instrument_id=row.instrument_id,
                timeframe=row.timeframe,
                trading_date=row.trading_date,
                session_type=row.session_type,
                open_ts_utc=_utc(row.open_ts_utc),
                close_ts_utc=_utc(row.close_ts_utc),
                open_price=float(row.open_price),
                high_price=float(row.high_price),
                low_price=float(row.low_price),
                close_price=float(row.close_price),
                volume_lots=float(row.volume_lots),
                is_special_day=bool(types),
                special_day_types=types,
            )
        )
    return candles


def compute_feature_rows(
    candles: Sequence[CandlePoint],
    *,
    selected_timeframes: set[str],
) -> list[FeatureRow]:
    grouped: dict[tuple[str, str], list[CandlePoint]] = defaultdict(list)
    outcome_candles: dict[str, list[CandlePoint]] = defaultdict(list)
    for candle in candles:
        grouped[(candle.instrument_id, candle.timeframe)].append(candle)
        if candle.timeframe == "5m":
            outcome_candles[candle.instrument_id].append(candle)
    for values in grouped.values():
        values.sort(key=lambda item: item.close_ts_utc)
    for values in outcome_candles.values():
        values.sort(key=lambda item: item.close_ts_utc)
    outcome_timestamps = {
        instrument_id: [item.close_ts_utc for item in values]
        for instrument_id, values in outcome_candles.items()
    }

    features: list[FeatureRow] = []
    for (_instrument_id, timeframe), values in grouped.items():
        if timeframe not in selected_timeframes:
            continue
        for index in range(12, len(values)):
            candle = values[index]
            if candle.is_special_day:
                continue
            outcomes = compute_outcomes(
                candle,
                outcome_candles.get(candle.instrument_id, ()),
                outcome_timestamps.get(candle.instrument_id, ()),
            )
            if outcomes.future_return_15m_bps is None:
                continue
            features.append(feature_from_history(values, index, outcomes))
    return features


def feature_from_history(
    values: Sequence[CandlePoint],
    index: int,
    outcomes: OutcomeSet,
) -> FeatureRow:
    candle = values[index]
    closes = [item.close_price for item in values[: index + 1]]
    ranges = [range_bps(item) for item in values[: index + 1]]
    volumes = [item.volume_lots for item in values[: index + 1]]
    close_msk = candle.close_ts_utc.astimezone(MOSCOW_TZ)
    avg_range_6 = average(ranges[index - 5 : index + 1])
    volume_ratio_vs_6 = safe_ratio(candle.volume_lots, average(volumes[index - 6 : index]))
    volume_ratio_vs_12 = safe_ratio(candle.volume_lots, average(volumes[index - 12 : index]))
    return FeatureRow(
        instrument_id=candle.instrument_id,
        timeframe=candle.timeframe,
        trading_date=candle.trading_date,
        session_type=candle.session_type,
        close_ts_utc=candle.close_ts_utc,
        hour_msk=close_msk.hour,
        day_of_week=close_msk.weekday(),
        is_morning=candle.session_type == "weekday_morning",
        is_main=candle.session_type == "weekday_main",
        is_evening=candle.session_type == "weekday_evening",
        special_day=candle.is_special_day,
        dividend_or_corporate_action=any(
            item in {"dividend_gap_day", "corporate_action_day"}
            for item in candle.special_day_types
        ),
        return_1_bar_bps=return_bps(closes[index - 1], closes[index]),
        return_2_bar_bps=return_bps(closes[index - 2], closes[index]),
        return_3_bar_bps=return_bps(closes[index - 3], closes[index]),
        return_6_bar_bps=return_bps(closes[index - 6], closes[index]),
        trend_slope_3=return_bps(closes[index - 3], closes[index]) / 3.0,
        trend_slope_6=return_bps(closes[index - 6], closes[index]) / 6.0,
        close_vs_sma_3_bps=return_bps(average(closes[index - 2 : index + 1]), closes[index]),
        close_vs_sma_6_bps=return_bps(average(closes[index - 5 : index + 1]), closes[index]),
        close_vs_sma_12_bps=return_bps(average(closes[index - 11 : index + 1]), closes[index]),
        range_bps=ranges[index],
        avg_range_3_bps=average(ranges[index - 2 : index + 1]),
        avg_range_6_bps=avg_range_6,
        volatility_6_bps=stddev(
            [return_bps(closes[pos - 1], closes[pos]) for pos in range(index - 5, index + 1)]
        ),
        volatility_12_bps=stddev(
            [return_bps(closes[pos - 1], closes[pos]) for pos in range(index - 11, index + 1)]
        ),
        abnormal_range_flag=avg_range_6 > 0 and ranges[index] > avg_range_6 * 3.0,
        volume_lots=candle.volume_lots,
        volume_ratio_vs_6=volume_ratio_vs_6,
        volume_ratio_vs_12=volume_ratio_vs_12,
        low_volume_flag=volume_ratio_vs_6 < 0.5,
        close_position_in_range=close_position_in_range(candle),
        outcomes=outcomes,
    )


def compute_outcomes(
    candle: CandlePoint,
    future_candles_5m: Sequence[CandlePoint],
    future_timestamps_5m: Sequence[datetime],
) -> OutcomeSet:
    horizon_5m = outcome_for_horizon(candle, future_candles_5m, future_timestamps_5m, 5)
    horizon_10m = outcome_for_horizon(candle, future_candles_5m, future_timestamps_5m, 10)
    horizon_15m = outcome_for_horizon(candle, future_candles_5m, future_timestamps_5m, 15)
    return OutcomeSet(
        future_return_5m_bps=horizon_5m.future_return_bps,
        future_return_10m_bps=horizon_10m.future_return_bps,
        future_return_15m_bps=horizon_15m.future_return_bps,
        mfe_5m_bps=horizon_5m.mfe_bps,
        mfe_10m_bps=horizon_10m.mfe_bps,
        mfe_15m_bps=horizon_15m.mfe_bps,
        mae_5m_bps=horizon_5m.mae_bps,
        mae_10m_bps=horizon_10m.mae_bps,
        mae_15m_bps=horizon_15m.mae_bps,
        horizon_5m=horizon_5m,
        horizon_10m=horizon_10m,
        horizon_15m=horizon_15m,
    )


def outcome_for_horizon(
    candle: CandlePoint,
    future_candles_5m: Sequence[CandlePoint],
    future_timestamps_5m: Sequence[datetime],
    horizon_minutes: int,
) -> HorizonOutcome:
    target_exit_ts = candle.close_ts_utc + timedelta(minutes=horizon_minutes)
    start = bisect.bisect_right(future_timestamps_5m, candle.close_ts_utc)
    exact = bisect.bisect_left(future_timestamps_5m, target_exit_ts, lo=start)
    if exact >= len(future_timestamps_5m) or future_timestamps_5m[exact] != target_exit_ts:
        return HorizonOutcome(
            requested_horizon_minutes=horizon_minutes,
            target_exit_ts_utc=target_exit_ts,
            actual_exit_ts_utc=None,
            actual_horizon_minutes=None,
            exit_alignment_seconds=None,
            horizon_valid=False,
            future_return_bps=None,
            mfe_bps=None,
            mae_bps=None,
            exit_alignment="missing_exact_target",
        )
    end = exact + 1
    window = future_candles_5m[start:end]
    if not window:
        return HorizonOutcome(
            requested_horizon_minutes=horizon_minutes,
            target_exit_ts_utc=target_exit_ts,
            actual_exit_ts_utc=None,
            actual_horizon_minutes=None,
            exit_alignment_seconds=None,
            horizon_valid=False,
            future_return_bps=None,
            mfe_bps=None,
            mae_bps=None,
            exit_alignment="missing_forward_window",
        )
    entry = candle.close_price
    future_return = return_bps(entry, window[-1].close_price)
    mfe = return_bps(entry, max(item.high_price for item in window))
    mae = return_bps(entry, min(item.low_price for item in window))
    actual_exit_ts = window[-1].close_ts_utc
    actual_horizon = (actual_exit_ts - candle.close_ts_utc).total_seconds() / 60.0
    alignment = (actual_exit_ts - target_exit_ts).total_seconds()
    return HorizonOutcome(
        requested_horizon_minutes=horizon_minutes,
        target_exit_ts_utc=target_exit_ts,
        actual_exit_ts_utc=actual_exit_ts,
        actual_horizon_minutes=actual_horizon,
        exit_alignment_seconds=alignment,
        horizon_valid=alignment == 0 and actual_horizon == horizon_minutes,
        future_return_bps=future_return,
        mfe_bps=mfe,
        mae_bps=mae,
        exit_alignment="exact",
    )


def generate_research_configs(
    *,
    instruments: tuple[str, ...],
    sides: tuple[str, ...],
    max_configs: int,
    short_available_by_instrument: Mapping[str, bool] | None = None,
    enforce_short_availability: bool = False,
) -> list[ResearchConfig]:
    configs: list[ResearchConfig] = []
    counter = 1

    def add(config: ResearchConfig) -> None:
        nonlocal counter
        if len(configs) >= max_configs:
            return
        configs.append(
            ResearchConfig(
                config_id=f"candle_feature_{counter:03d}",
                hypothesis=config.hypothesis,
                horizon_minutes=config.horizon_minutes,
                side=config.side,
                return_bars=config.return_bars,
                return_threshold_bps=config.return_threshold_bps,
                slope_threshold_bps=config.slope_threshold_bps,
                breakout_multiplier=config.breakout_multiplier,
                compression_max_avg_range_bps=config.compression_max_avg_range_bps,
                max_volatility_12_bps=config.max_volatility_12_bps,
                edge_margin_bps=config.edge_margin_bps,
                instruments=config.instruments,
                timeframes=config.timeframes,
                sessions=config.sessions,
                short_available_by_broker=config.short_available_by_broker,
                short_requires_broker_confirmation=config.short_requires_broker_confirmation,
            )
        )
        counter += 1

    short_availability = short_available_by_instrument or {}

    def short_ready(instrument: str | None = None) -> bool:
        if not enforce_short_availability:
            return True
        if instrument is not None:
            return short_availability.get(instrument, False)
        return any(short_availability.get(item, False) for item in instruments)

    for side in sides:
        short_available_for_side = short_ready()
        short_requires_confirmation = side == "short" and not enforce_short_availability
        if side == "long":
            for bars in (1, 2, 3):
                for threshold in (20, 30, 45, 60, 90):
                    for horizon in (5, 10, 15):
                        add(
                            ResearchConfig(
                                config_id="",
                                hypothesis="momentum_continuation",
                                horizon_minutes=horizon,
                                side=side,
                                return_bars=bars,
                                return_threshold_bps=float(threshold),
                            )
                        )
            for slope in (15, 30, 45):
                for horizon in (5, 10, 15):
                    add(
                        ResearchConfig(
                            config_id="",
                            hypothesis="pullback_in_uptrend",
                            horizon_minutes=horizon,
                            side=side,
                            slope_threshold_bps=float(slope),
                            edge_margin_bps=10.0,
                        )
                    )
            for multiplier in (1.5, 2.0, 2.5):
                for horizon in (5, 10, 15):
                    add(
                        ResearchConfig(
                            config_id="",
                            hypothesis="breakout_after_compression",
                            horizon_minutes=horizon,
                            side=side,
                            breakout_multiplier=multiplier,
                            compression_max_avg_range_bps=120.0,
                            edge_margin_bps=10.0,
                        )
                    )
            for threshold in (30, 45):
                for max_vol in (80, 120, 160, 220):
                    add(
                        ResearchConfig(
                            config_id="",
                            hypothesis="momentum_with_volatility_filter",
                            horizon_minutes=15,
                            side=side,
                            return_bars=3,
                            return_threshold_bps=float(threshold),
                            max_volatility_12_bps=float(max_vol),
                            edge_margin_bps=10.0,
                        )
                    )
            for instrument in instruments:
                for session_type in ("weekday_morning", "weekday_main", "weekday_evening"):
                    for threshold in (45, 60):
                        add(
                            ResearchConfig(
                                config_id="",
                                hypothesis="restricted_momentum_15m",
                                horizon_minutes=15,
                                side=side,
                                return_bars=3,
                                return_threshold_bps=float(threshold),
                                instruments=(instrument,),
                                timeframes=("15m",),
                                sessions=(session_type,),
                                edge_margin_bps=10.0,
                            )
                        )
            for margin in (5, 10, 15, 25):
                add(
                    ResearchConfig(
                        config_id="",
                        hypothesis="cost_aware_momentum",
                        horizon_minutes=15,
                        side=side,
                        return_bars=3,
                        return_threshold_bps=60.0,
                        edge_margin_bps=float(margin),
                    )
                )
        if side == "short":
            for bars in (1, 2, 3):
                for threshold in (20, 30, 45, 60, 90):
                    for horizon in (5, 10, 15):
                        add(
                            ResearchConfig(
                                config_id="",
                                hypothesis="momentum_breakdown",
                                horizon_minutes=horizon,
                                side=side,
                                return_bars=bars,
                                return_threshold_bps=float(threshold),
                                short_available_by_broker=short_available_for_side,
                                short_requires_broker_confirmation=short_requires_confirmation,
                            )
                        )
            for slope in (15, 30, 45):
                for horizon in (5, 10, 15):
                    add(
                        ResearchConfig(
                            config_id="",
                            hypothesis="pullback_in_downtrend",
                            horizon_minutes=horizon,
                            side=side,
                            slope_threshold_bps=float(slope),
                            edge_margin_bps=10.0,
                            short_available_by_broker=short_available_for_side,
                            short_requires_broker_confirmation=short_requires_confirmation,
                        )
                    )
            for multiplier in (1.5, 2.0, 2.5):
                for horizon in (5, 10, 15):
                    add(
                        ResearchConfig(
                            config_id="",
                            hypothesis="breakdown_after_compression",
                            horizon_minutes=horizon,
                            side=side,
                            breakout_multiplier=multiplier,
                            compression_max_avg_range_bps=120.0,
                            edge_margin_bps=10.0,
                            short_available_by_broker=short_available_for_side,
                            short_requires_broker_confirmation=short_requires_confirmation,
                        )
                    )
            for threshold in (30, 45):
                for max_vol in (80, 120, 160, 220):
                    add(
                        ResearchConfig(
                            config_id="",
                            hypothesis="breakdown_with_volatility_filter",
                            horizon_minutes=15,
                            side=side,
                            return_bars=3,
                            return_threshold_bps=float(threshold),
                            max_volatility_12_bps=float(max_vol),
                            edge_margin_bps=10.0,
                            short_available_by_broker=short_available_for_side,
                            short_requires_broker_confirmation=short_requires_confirmation,
                        )
                    )
            for instrument in instruments:
                if enforce_short_availability and not short_ready(instrument):
                    continue
                for session_type in ("weekday_morning", "weekday_main", "weekday_evening"):
                    for threshold in (45, 60):
                        add(
                            ResearchConfig(
                                config_id="",
                                hypothesis="restricted_breakdown_15m",
                                horizon_minutes=15,
                                side=side,
                                return_bars=3,
                                return_threshold_bps=float(threshold),
                                instruments=(instrument,),
                                timeframes=("15m",),
                                sessions=(session_type,),
                                edge_margin_bps=10.0,
                                short_available_by_broker=short_ready(instrument),
                                short_requires_broker_confirmation=not enforce_short_availability,
                            )
                        )
            for margin in (5, 10, 15, 25):
                add(
                    ResearchConfig(
                        config_id="",
                        hypothesis="cost_aware_breakdown",
                        horizon_minutes=15,
                        side=side,
                        return_bars=3,
                        return_threshold_bps=60.0,
                        edge_margin_bps=float(margin),
                        short_available_by_broker=short_available_for_side,
                        short_requires_broker_confirmation=short_requires_confirmation,
                    )
                )
    return configs[:max_configs]


def evaluate_configs(
    features: Sequence[FeatureRow],
    *,
    configs: Sequence[ResearchConfig],
    train_dates: set[date],
    validation_dates: set[date],
    total_cost_bps_value: float,
    min_validation_candidates: int,
    short_available_by_instrument: Mapping[str, bool] | None = None,
    enforce_short_availability: bool = False,
) -> list[EvaluationResult]:
    results: list[EvaluationResult] = []
    short_availability = short_available_by_instrument or {}
    feature_index = build_feature_index(features)
    for config in configs:
        train, validation, full = evaluate_config_features(
            iter_features_for_config(config, features=features, feature_index=feature_index),
            config=config,
            train_dates=train_dates,
            validation_dates=validation_dates,
            total_cost_bps_value=total_cost_bps_value,
            short_available_by_instrument=short_availability,
            enforce_short_availability=enforce_short_availability,
        )
        passed, reasons = classify_result(
            config,
            train,
            validation,
            min_validation_candidates=min_validation_candidates,
        )
        results.append(
            EvaluationResult(
                config=config,
                train=train,
                validation=validation,
                full=full,
                passed=passed,
                rejection_reasons=tuple(reasons),
            )
        )
    return results


def build_feature_index(
    features: Sequence[FeatureRow],
) -> dict[tuple[str, str, str], list[FeatureRow]]:
    index: dict[tuple[str, str, str], list[FeatureRow]] = defaultdict(list)
    for feature in features:
        index[(feature.instrument_id, feature.timeframe, feature.session_type)].append(feature)
    return index


def iter_features_for_config(
    config: ResearchConfig,
    *,
    features: Sequence[FeatureRow],
    feature_index: Mapping[tuple[str, str, str], Sequence[FeatureRow]],
) -> Iterable[FeatureRow]:
    if not (config.instruments or config.timeframes or config.sessions):
        return iter(features)

    instruments = config.instruments or tuple({feature.instrument_id for feature in features})
    timeframes = config.timeframes or tuple({feature.timeframe for feature in features})
    sessions = config.sessions or tuple({feature.session_type for feature in features})

    def scoped() -> Iterable[FeatureRow]:
        for instrument in instruments:
            for timeframe in timeframes:
                for session_type in sessions:
                    yield from feature_index.get((instrument, timeframe, session_type), ())

    return scoped()


def config_matches_feature(
    config: ResearchConfig,
    feature: FeatureRow,
    *,
    short_available_by_instrument: Mapping[str, bool],
    enforce_short_availability: bool,
) -> bool:
    if config.instruments and feature.instrument_id not in config.instruments:
        return False
    if config.timeframes and feature.timeframe not in config.timeframes:
        return False
    if config.sessions and feature.session_type not in config.sessions:
        return False
    if feature.special_day or feature.dividend_or_corporate_action:
        return False
    if (
        config.side == "short"
        and enforce_short_availability
        and not short_available_by_instrument.get(feature.instrument_id, False)
    ):
        return False
    if config.max_volatility_12_bps is not None and (
        feature.volatility_12_bps > config.max_volatility_12_bps
    ):
        return False
    if config.hypothesis in {
        "momentum_continuation",
        "momentum_with_volatility_filter",
        "restricted_momentum_15m",
        "cost_aware_momentum",
    }:
        assert config.return_bars is not None
        value = return_for_bars(feature, config.return_bars)
        return value >= float(config.return_threshold_bps or 0)
    if config.hypothesis in {
        "momentum_breakdown",
        "breakdown_with_volatility_filter",
        "restricted_breakdown_15m",
        "cost_aware_breakdown",
    }:
        assert config.return_bars is not None
        value = return_for_bars(feature, config.return_bars)
        return value <= -float(config.return_threshold_bps or 0)
    if config.hypothesis == "pullback_in_uptrend":
        return (
            feature.trend_slope_6 >= float(config.slope_threshold_bps or 0)
            and feature.return_1_bar_bps < 0
            and feature.close_vs_sma_6_bps > 0
        )
    if config.hypothesis == "pullback_in_downtrend":
        return (
            feature.trend_slope_6 <= -float(config.slope_threshold_bps or 0)
            and feature.return_1_bar_bps > 0
            and feature.close_vs_sma_6_bps < 0
        )
    if config.hypothesis == "breakout_after_compression":
        return (
            feature.avg_range_6_bps <= float(config.compression_max_avg_range_bps or 120.0)
            and feature.range_bps
            >= feature.avg_range_6_bps * float(config.breakout_multiplier or 1.0)
            and feature.close_position_in_range >= 0.75
        )
    if config.hypothesis == "breakdown_after_compression":
        return (
            feature.avg_range_6_bps <= float(config.compression_max_avg_range_bps or 120.0)
            and feature.range_bps
            >= feature.avg_range_6_bps * float(config.breakout_multiplier or 1.0)
            and feature.close_position_in_range <= 0.25
        )
    return False


@dataclass(slots=True)
class MetricsAccumulator:
    day_net: dict[date, float] = field(default_factory=lambda: defaultdict(float))
    candidates: int = 0
    gross_total: float = 0.0
    net_total: float = 0.0
    wins: int = 0

    def add(self, *, trading_date: date, gross: float, net: float) -> None:
        self.candidates += 1
        self.gross_total += gross
        self.net_total += net
        if net > 0:
            self.wins += 1
        self.day_net[trading_date] += net

    def metrics(self) -> EvaluationMetrics:
        max_bad_day = min(self.day_net.values()) if self.day_net else 0.0
        top_day = max((abs(value) for value in self.day_net.values()), default=0.0)
        top_day_contribution = top_day / abs(self.net_total) if abs(self.net_total) > 0 else 0.0
        return EvaluationMetrics(
            candidates=self.candidates,
            gross_pnl_bps_proxy=round(self.gross_total, 4),
            net_pnl_bps_proxy=round(self.net_total, 4),
            average_net_bps_proxy=round(self.net_total / self.candidates, 4)
            if self.candidates
            else 0.0,
            win_proxy=round(self.wins / self.candidates, 4) if self.candidates else 0.0,
            active_days=len(self.day_net),
            max_bad_day_bps_proxy=round(max_bad_day, 4),
            top_day_contribution=round(top_day_contribution, 4),
        )


def evaluate_config_features(
    rows: Iterable[FeatureRow],
    *,
    config: ResearchConfig,
    train_dates: set[date],
    validation_dates: set[date],
    total_cost_bps_value: float,
    short_available_by_instrument: Mapping[str, bool],
    enforce_short_availability: bool,
) -> tuple[EvaluationMetrics, EvaluationMetrics, EvaluationMetrics]:
    train = MetricsAccumulator()
    validation = MetricsAccumulator()
    full = MetricsAccumulator()
    for row in rows:
        if not config_matches_feature(
            config,
            row,
            short_available_by_instrument=short_available_by_instrument,
            enforce_short_availability=enforce_short_availability,
        ):
            continue
        signal_strength = signal_strength_bps(row, config)
        if signal_strength < total_cost_bps_value + config.edge_margin_bps:
            continue
        gross = row.outcomes.gross_for_side(config.horizon_minutes, config.side)
        if gross is None:
            continue
        net = gross - total_cost_bps_value
        full.add(trading_date=row.trading_date, gross=gross, net=net)
        if row.trading_date in train_dates:
            train.add(trading_date=row.trading_date, gross=gross, net=net)
        elif row.trading_date in validation_dates:
            validation.add(trading_date=row.trading_date, gross=gross, net=net)
    return train.metrics(), validation.metrics(), full.metrics()


def evaluate_rows(
    rows: Sequence[FeatureRow],
    *,
    config: ResearchConfig,
    total_cost_bps_value: float,
) -> EvaluationMetrics:
    day_net: dict[date, float] = defaultdict(float)
    gross_values: list[float] = []
    net_values: list[float] = []
    for row in rows:
        signal_strength = signal_strength_bps(row, config)
        if signal_strength < total_cost_bps_value + config.edge_margin_bps:
            continue
        gross = row.outcomes.gross_for_side(config.horizon_minutes, config.side)
        if gross is None:
            continue
        net = gross - total_cost_bps_value
        gross_values.append(gross)
        net_values.append(net)
        day_net[row.trading_date] += net
    candidates = len(net_values)
    gross_total = sum(gross_values)
    net_total = sum(net_values)
    max_bad_day = min(day_net.values()) if day_net else 0.0
    top_day = max((abs(value) for value in day_net.values()), default=0.0)
    top_day_contribution = top_day / abs(net_total) if abs(net_total) > 0 else 0.0
    return EvaluationMetrics(
        candidates=candidates,
        gross_pnl_bps_proxy=round(gross_total, 4),
        net_pnl_bps_proxy=round(net_total, 4),
        average_net_bps_proxy=round(net_total / candidates, 4) if candidates else 0.0,
        win_proxy=round(sum(1 for value in net_values if value > 0) / candidates, 4)
        if candidates
        else 0.0,
        active_days=len(day_net),
        max_bad_day_bps_proxy=round(max_bad_day, 4),
        top_day_contribution=round(top_day_contribution, 4),
    )


def classify_result(
    config: ResearchConfig,
    train: EvaluationMetrics,
    validation: EvaluationMetrics,
    *,
    min_validation_candidates: int,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if validation.net_pnl_bps_proxy <= 0:
        reasons.append("validation_net_not_positive")
    if validation.candidates < min_validation_candidates:
        reasons.append("too_few_validation_candidates")
    if validation.active_days < 10:
        reasons.append("too_few_active_validation_days")
    if validation.top_day_contribution > 0.5:
        reasons.append("validation_dominated_by_one_day")
    if train.net_pnl_bps_proxy < 0 <= validation.net_pnl_bps_proxy:
        reasons.append("train_validation_signs_contradict")
    if train.net_pnl_bps_proxy <= 0:
        reasons.append("train_net_not_positive")
    if config.side == "short" and not config.short_available_by_broker:
        reasons.append("short_not_available_by_broker")
    if config.side == "short" and config.short_requires_broker_confirmation:
        reasons.append("short_requires_broker_confirmation")
    return not reasons, reasons


def split_trading_dates(values: Iterable[date]) -> tuple[set[date], set[date]]:
    dates = sorted(set(values))
    if not dates:
        return set(), set()
    split_index = max(1, int(len(dates) * 0.7))
    if split_index >= len(dates):
        split_index = max(1, len(dates) - 1)
    return set(dates[:split_index]), set(dates[split_index:])


def build_report_payload(
    *,
    features: Sequence[FeatureRow],
    configs: Sequence[ResearchConfig],
    results: Sequence[EvaluationResult],
    from_date: date,
    to_date: date,
    instruments: tuple[str, ...],
    timeframes: tuple[str, ...],
    sessions: tuple[str, ...],
    total_cost: float,
    sides: tuple[str, ...],
    short_available_by_instrument: Mapping[str, bool],
    enforce_short_availability: bool,
    exclude_dividend_windows_for_shorts: bool,
    train_dates: set[date],
    validation_dates: set[date],
    dry_run: bool,
) -> dict[str, Any]:
    sorted_by_validation = sorted(
        results,
        key=lambda result: (
            result.validation.net_pnl_bps_proxy,
            result.validation.candidates,
            -result.validation.top_day_contribution,
        ),
        reverse=True,
    )
    sorted_by_avg = sorted(
        results,
        key=lambda result: (
            result.validation.average_net_bps_proxy,
            result.validation.candidates,
        ),
        reverse=True,
    )
    sorted_by_stability = sorted(
        results,
        key=lambda result: (
            result.passed,
            result.validation.net_pnl_bps_proxy,
            -result.validation.top_day_contribution,
            result.validation.active_days,
        ),
        reverse=True,
    )
    passing = [result for result in results if result.passed]
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "source": "candle_feature_research",
        "dry_run": dry_run,
        "dataset_summary": dataset_summary(features, from_date, to_date, instruments, timeframes),
        "features_summary": features_summary(features),
        "horizon_mismatch_count": horizon_mismatch_count(features),
        "train_period": period_payload(train_dates),
        "validation_period": period_payload(validation_dates),
        "cost_model": {
            "total_cost_bps": round(total_cost, 4),
            "minimum_round_trip_fee_bps": MIN_ROUND_TRIP_FEE_BPS,
        },
        "sides": list(sides),
        "short_available_by_instrument": dict(sorted(short_available_by_instrument.items())),
        "allow_short_only_if_broker_short_available": enforce_short_availability,
        "exclude_dividend_windows_for_shorts": exclude_dividend_windows_for_shorts,
        "short_restrictions": {
            "exclude_dividend_or_corporate_action_days": True,
            "exclude_future_dividend_risk_windows": exclude_dividend_windows_for_shorts,
            "minimum_validation_candidates": 100,
            "requires_broker_short_available": enforce_short_availability,
        },
        "tested_configs": [result_payload(result) for result in results],
        "passing_configs": [result_payload(result) for result in passing],
        "rejected_configs": [result_payload(result) for result in results if not result.passed],
        "best_by_validation_net": result_payload(sorted_by_validation[0]) if results else None,
        "best_by_avg_net": result_payload(sorted_by_avg[0]) if results else None,
        "best_by_stability": result_payload(sorted_by_stability[0]) if results else None,
        "ready_for_shadow_candidate": bool(passing),
        "warnings": warnings_for_results(features, results),
        "next_recommendation": next_recommendation(passing),
        "real_orders_disabled": True,
        "broker_calls_disabled": True,
        "shadow_runtime_started": False,
        "production_started": False,
        "configs_requested": len(configs),
        "configs_tested": len(results),
    }


def dataset_summary(
    features: Sequence[FeatureRow],
    from_date: date,
    to_date: date,
    instruments: tuple[str, ...],
    timeframes: tuple[str, ...],
) -> dict[str, Any]:
    by_instrument = count_by(features, "instrument_id")
    by_timeframe = count_by(features, "timeframe")
    by_session = count_by(features, "session_type")
    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "instruments": list(instruments),
        "timeframes": list(timeframes),
        "feature_rows": len(features),
        "trading_dates": len({feature.trading_date for feature in features}),
        "by_instrument": by_instrument,
        "by_timeframe": by_timeframe,
        "by_session": by_session,
        "special_days_excluded_by_default": True,
    }


def features_summary(features: Sequence[FeatureRow]) -> dict[str, Any]:
    return {
        "feature_names": [
            "return_1_bar_bps",
            "return_2_bar_bps",
            "return_3_bar_bps",
            "return_6_bar_bps",
            "trend_slope_3",
            "trend_slope_6",
            "close_vs_sma_3_bps",
            "close_vs_sma_6_bps",
            "close_vs_sma_12_bps",
            "range_bps",
            "avg_range_3_bps",
            "avg_range_6_bps",
            "volatility_6_bps",
            "volatility_12_bps",
            "volume_ratio_vs_6",
            "volume_ratio_vs_12",
            "hour_msk",
            "day_of_week",
        ],
        "outcome_names": [
            "future_return_5m_bps",
            "future_return_10m_bps",
            "future_return_15m_bps",
            "mfe_5/10/15",
            "mae_5/10/15",
        ],
        "abnormal_range_rows": sum(1 for feature in features if feature.abnormal_range_flag),
        "low_volume_rows": sum(1 for feature in features if feature.low_volume_flag),
        "horizon_mismatch_count": horizon_mismatch_count(features),
    }


def horizon_mismatch_count(features: Sequence[FeatureRow]) -> int:
    count = 0
    for feature in features:
        for horizon in (5, 10, 15):
            detail = feature.outcomes.detail_for_horizon(horizon)
            if detail is not None and not detail.horizon_valid:
                count += 1
    return count


def period_payload(values: set[date]) -> dict[str, Any]:
    if not values:
        return {"from_date": None, "to_date": None, "trading_days": 0}
    return {
        "from_date": min(values).isoformat(),
        "to_date": max(values).isoformat(),
        "trading_days": len(values),
    }


def result_payload(result: EvaluationResult) -> dict[str, Any]:
    return {
        "config": asdict(result.config),
        "train": asdict(result.train),
        "validation": asdict(result.validation),
        "full": asdict(result.full),
        "passed": result.passed,
        "rejection_reasons": list(result.rejection_reasons),
    }


def warnings_for_results(
    features: Sequence[FeatureRow],
    results: Sequence[EvaluationResult],
) -> list[str]:
    warnings = [
        "historical candles do not validate spread/depth/slippage/latency/rejects/partial fills",
        "all results are candle-only gross/net bps proxy, not executable PnL",
        (
            "short results require broker short availability and later live microstructure "
            "confirmation"
        ),
    ]
    if not any(result.passed for result in results):
        warnings.append("no configuration passed validation criteria")
    if len(features) == 0:
        warnings.append("no feature rows were available")
    return warnings


def next_recommendation(passing: Sequence[EvaluationResult]) -> str:
    if passing:
        return (
            "Create a separate shadow-candidate config payload only after operator review; "
            "still requires live shadow confirmation before any real orders."
        )
    return (
        "Do not start shadow from candle-only research. Build a data-only shadow collector "
        "for spread/depth/order-book/latency features or change the signal model."
    )


def write_reports(payload: dict[str, Any], *, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "CANDLE_FEATURE_RESEARCH_365D.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "CANDLE_FEATURE_RESEARCH_365D.md").write_text(
        markdown_report(payload),
        encoding="utf-8",
    )
    if payload["ready_for_shadow_candidate"]:
        target = output_dir / "SUGGESTED_SHADOW_CANDIDATE_CONFIG_FROM_CANDLE_RESEARCH.json"
        target.write_text(
            json.dumps(suggested_shadow_config(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        target = output_dir / "NO_SHADOW_CANDIDATE_FROM_CANDLE_RESEARCH.json"
        target.write_text(
            json.dumps(
                {
                    "ready_for_shadow_candidate": False,
                    "reasons": [
                        "no tested candle-only config passed validation criteria",
                        "do not launch shadow runtime from this result",
                    ],
                    "best_by_validation_net": payload["best_by_validation_net"],
                    "next_recommendation": payload["next_recommendation"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def markdown_report(payload: dict[str, Any]) -> str:
    best = payload["best_by_validation_net"]
    best_validation = best["validation"] if best else {}
    top_config_columns = [
        "config",
        "hypothesis",
        "horizon",
        "validation_net",
        "validation_candidates",
        "passed",
    ]
    top_rows = []
    for result in payload["tested_configs"][:10]:
        top_rows.append(
            {
                "config": result["config"]["config_id"],
                "hypothesis": result["config"]["hypothesis"],
                "horizon": result["config"]["horizon_minutes"],
                "validation_net": result["validation"]["net_pnl_bps_proxy"],
                "validation_candidates": result["validation"]["candidates"],
                "passed": result["passed"],
            }
        )
    rejected = payload["rejected_configs"][:10]
    return f"""# Candle Feature Research 365D

## 1. Executive summary

Ready for shadow candidate: `{payload["ready_for_shadow_candidate"]}`.
Configs tested: `{payload["configs_tested"]}`.
Passing configs: `{len(payload["passing_configs"])}`.
Best validation net bps proxy: `{best_validation.get("net_pnl_bps_proxy")}`.
Best validation candidates: `{best_validation.get("candidates")}`.
Horizon mismatches rejected: `{payload["horizon_mismatch_count"]}`.

## 2. Dataset summary

```json
{json.dumps(payload["dataset_summary"], ensure_ascii=False, indent=2)}
```

## 3. Why previous strategy failed

The previous candle directional move strategy produced positive gross but negative net
after realistic fee/slippage assumptions. This run keeps the cost floor and tests
whether additional candle-only filters improve walk-forward validation.

## 4. Feature set

```json
{json.dumps(payload["features_summary"], ensure_ascii=False, indent=2)}
```

## 5. Hypotheses tested

Momentum continuation, pullback in uptrend, breakout after compression, volatility
filtering, session/timeframe restriction, and cost-aware edge filtering. When `short`
is included, the symmetric breakdown/downtrend hypotheses use `gross=-future_return`
and are rejected unless broker short availability is confirmed.

## 6. Train/validation methodology

Train period: `{payload["train_period"]}`.
Validation period: `{payload["validation_period"]}`.
No feature uses future candles; future candles are used only for outcomes.

## 7. Top configs

{markdown_table(top_rows, top_config_columns)}

## 8. Rejected configs

First rejected configs:

```json
{json.dumps(rejected, ensure_ascii=False, indent=2)}
```

## 9. Best stable config, if any

```json
{json.dumps(payload["best_by_stability"], ensure_ascii=False, indent=2)}
```

## 10. Whether shadow candidate exists

`{payload["ready_for_shadow_candidate"]}`.

## 11. If no candidate exists, why

{chr(10).join(f"- {warning}" for warning in payload["warnings"])}

## 12. What requires live shadow data

Spread, order-book depth, queue priority, real slippage, latency, broker rejects,
partial fills, and stream quality.

## 13. Next steps

{payload["next_recommendation"]}

## 14. Suggested config payload

See either `SUGGESTED_SHADOW_CANDIDATE_CONFIG_FROM_CANDLE_RESEARCH.json` or
`NO_SHADOW_CANDIDATE_FROM_CANDLE_RESEARCH.json`.
"""


def suggested_shadow_config(payload: dict[str, Any]) -> dict[str, Any]:
    best = payload["best_by_stability"]
    return {
        "source": "candle_feature_research_365d",
        "apply_automatically": False,
        "requires_operator_review": True,
        "requires_shadow_confirmation": True,
        "config": best["config"],
        "validation": best["validation"],
        "warnings": payload["warnings"],
    }


def markdown_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def count_by(features: Sequence[FeatureRow], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for feature in features:
        counts[str(getattr(feature, attribute))] += 1
    return dict(sorted(counts.items()))


def return_for_bars(feature: FeatureRow, bars: int) -> float:
    return {
        1: feature.return_1_bar_bps,
        2: feature.return_2_bar_bps,
        3: feature.return_3_bar_bps,
        6: feature.return_6_bar_bps,
    }[bars]


def signal_strength_bps(row: FeatureRow, config: ResearchConfig) -> float:
    if config.hypothesis == "pullback_in_uptrend":
        return row.trend_slope_6
    if config.hypothesis == "pullback_in_downtrend":
        return abs(row.trend_slope_6)
    if config.hypothesis == "breakout_after_compression":
        return row.range_bps
    if config.hypothesis == "breakdown_after_compression":
        return row.range_bps
    if config.return_bars is not None:
        value = return_for_bars(row, config.return_bars)
        return abs(value) if config.side == "short" else value
    return 0.0


def total_cost_bps(commission_bps_per_side: float, slippage_bps: float) -> float:
    fee = max(commission_bps_per_side * 2.0, MIN_ROUND_TRIP_FEE_BPS)
    return fee + max(slippage_bps, 0.0)


def range_bps(candle: CandlePoint) -> float:
    return return_bps(candle.low_price, candle.high_price)


def return_bps(start: float, end: float) -> float:
    if start <= 0:
        return 0.0
    return ((end - start) / start) * TEN_THOUSAND


def average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stddev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = average(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def safe_ratio(value: float, denominator: float) -> float:
    return value / denominator if denominator > 0 else 0.0


def close_position_in_range(candle: CandlePoint) -> float:
    if candle.high_price <= candle.low_price:
        return 0.5
    return (candle.close_price - candle.low_price) / (candle.high_price - candle.low_price)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


if __name__ == "__main__":
    main()

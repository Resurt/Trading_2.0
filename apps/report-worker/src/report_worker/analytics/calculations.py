"""Deterministic report and counterfactual calculations."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from report_worker.analytics.models import (
    AnalyticsAssumptions,
    CounterfactualAnalysis,
    CounterfactualSource,
    FunnelMetrics,
    PricePathPoint,
    TrendClassification,
    WindowOutcome,
)

TEN_THOUSAND = Decimal("10000")
ZERO = Decimal("0")
LONG_BIAS_THRESHOLD_BPS = Decimal("25.0")
SHORT_BIAS_THRESHOLD_BPS = Decimal("-25.0")


def classify_day_trend(
    candles_by_instrument: dict[str, list[PricePathPoint]],
) -> TrendClassification:
    """Classify daily trend from first open to last close per instrument.

    Algorithm v1:
    - take closed market candles for the trading day;
    - for each instrument, calculate return from first open to last close;
    - average instrument returns equally;
    - `long_bias` if average >= +25 bps;
    - `short_bias` if average <= -25 bps;
    - otherwise `mixed_flat`.
    """

    returns: dict[str, Decimal] = {}
    for instrument_id, candles in candles_by_instrument.items():
        ordered = sorted(candles, key=lambda candle: candle.ts_utc)
        if not ordered:
            continue
        first_open = ordered[0].open_price
        last_close = ordered[-1].close_price
        if first_open <= ZERO:
            continue
        returns[instrument_id] = ((last_close - first_open) / first_open) * TEN_THOUSAND

    if not returns:
        return TrendClassification(
            market_regime="mixed_flat",
            average_return_bps=ZERO,
            instrument_returns_bps={},
        )

    average = sum(returns.values(), ZERO) / Decimal(len(returns))
    if average >= LONG_BIAS_THRESHOLD_BPS:
        regime = "long_bias"
    elif average <= SHORT_BIAS_THRESHOLD_BPS:
        regime = "short_bias"
    else:
        regime = "mixed_flat"
    return TrendClassification(
        market_regime=regime,
        average_return_bps=average.quantize(Decimal("0.0001")),
        instrument_returns_bps={
            key: value.quantize(Decimal("0.0001")) for key, value in returns.items()
        },
    )


def analyze_counterfactual(
    *,
    source: CounterfactualSource,
    price_path: Iterable[PricePathPoint],
    assumptions: AnalyticsAssumptions,
    windows_minutes: tuple[int, ...] = (5, 10, 15),
) -> CounterfactualAnalysis:
    """Evaluate missed/cancelled outcome over 5/10/15 minute windows."""

    ordered = sorted(price_path, key=lambda point: point.ts_utc)
    outcomes: dict[int, WindowOutcome] = {}
    for window in windows_minutes:
        end_ts = source.event_ts + timedelta(minutes=window)
        window_points = tuple(
            point for point in ordered if source.event_ts < point.ts_utc <= end_ts
        )
        outcomes[window] = _window_outcome(
            source=source,
            window_minutes=window,
            price_path=window_points,
            assumptions=assumptions,
        )
    return CounterfactualAnalysis(source=source, windows=outcomes, assumptions=assumptions)


def build_funnel_metrics(
    *,
    candidates: int,
    blockers: int,
    approved: int,
    posted: int,
    filled: int,
    profitable: int,
) -> FunnelMetrics:
    return FunnelMetrics(
        candidates=candidates,
        blockers=blockers,
        approved=approved,
        posted=posted,
        filled=filled,
        profitable=profitable,
    )


def state_time_distribution(events: Iterable[tuple[datetime, str]]) -> dict[str, float]:
    """Estimate seconds spent in each state from ordered state transition events."""

    ordered = sorted(events, key=lambda item: item[0])
    seconds_by_state: dict[str, float] = defaultdict(float)
    for index, (ts_utc, state) in enumerate(ordered[:-1]):
        next_ts = ordered[index + 1][0]
        seconds_by_state[state] += max(0.0, (next_ts - ts_utc).total_seconds())
    return dict(sorted(seconds_by_state.items()))


def counts_by(values: Iterable[str | None]) -> dict[str, int]:
    counter = Counter(value for value in values if value)
    return dict(sorted(counter.items()))


def fill_ratio(*, filled: int, posted: int) -> Decimal:
    if posted <= 0:
        return ZERO
    return (Decimal(filled) / Decimal(posted)).quantize(Decimal("0.0001"))


def realised_pnl_from_fills(
    fills: Iterable[tuple[str, int, Decimal, Decimal]],
) -> Decimal:
    """Estimate realised PnL as sell proceeds minus buy costs and commissions."""

    pnl = ZERO
    for side, lot_qty, price, commission in fills:
        notional = Decimal(lot_qty) * price
        if side.lower() == "sell":
            pnl += notional
        else:
            pnl -= notional
        pnl -= commission
    return pnl.quantize(Decimal("0.0001"))


def _window_outcome(
    *,
    source: CounterfactualSource,
    window_minutes: int,
    price_path: tuple[PricePathPoint, ...],
    assumptions: AnalyticsAssumptions,
) -> WindowOutcome:
    if not price_path or source.entry_price <= ZERO:
        return WindowOutcome(
            window_minutes=window_minutes,
            mfe_bps=None,
            mae_bps=None,
            close_return_bps=None,
            theoretical_pnl_bps=None,
            theoretical_pnl_rub=None,
            would_profit=None,
            tp_hit=None,
            sl_hit=None,
        )

    max_high = max(point.high_price for point in price_path)
    min_low = min(point.low_price for point in price_path)
    last_close = price_path[-1].close_price
    direction = Decimal("1") if source.side.lower() == "buy" else Decimal("-1")

    if direction > ZERO:
        mfe = ((max_high - source.entry_price) / source.entry_price) * TEN_THOUSAND
        mae = ((min_low - source.entry_price) / source.entry_price) * TEN_THOUSAND
    else:
        mfe = ((source.entry_price - min_low) / source.entry_price) * TEN_THOUSAND
        mae = ((source.entry_price - max_high) / source.entry_price) * TEN_THOUSAND

    close_return = ((last_close - source.entry_price) / source.entry_price) * TEN_THOUSAND
    directional_return = close_return * direction
    theoretical_pnl_bps = directional_return - assumptions.total_cost_bps
    theoretical_pnl_rub = (
        source.entry_price
        * Decimal(source.lot_qty)
        * theoretical_pnl_bps
        / TEN_THOUSAND
    )

    return WindowOutcome(
        window_minutes=window_minutes,
        mfe_bps=mfe.quantize(Decimal("0.0001")),
        mae_bps=mae.quantize(Decimal("0.0001")),
        close_return_bps=directional_return.quantize(Decimal("0.0001")),
        theoretical_pnl_bps=theoretical_pnl_bps.quantize(Decimal("0.0001")),
        theoretical_pnl_rub=theoretical_pnl_rub.quantize(Decimal("0.0001")),
        would_profit=theoretical_pnl_bps > ZERO,
        tp_hit=mfe >= assumptions.take_profit_bps,
        sl_hit=mae <= -assumptions.stop_loss_bps,
    )

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
TREND_UP_THRESHOLD_BPS = Decimal("25.0")
TREND_DOWN_THRESHOLD_BPS = Decimal("-25.0")
CHOPPY_RANGE_THRESHOLD_BPS = Decimal("80.0")


def classify_day_trend(
    candles_by_instrument: dict[str, list[PricePathPoint]],
) -> TrendClassification:
    """Classify daily trend from first open to last close per instrument.

    Algorithm v1:
    - take closed market candles for the trading day;
    - for each instrument, calculate return from first open to last close;
    - average instrument returns equally;
    - `trend_up` if average >= +25 bps;
    - `trend_down` if average <= -25 bps;
    - `choppy` if average is flat but intraday range is wide;
    - otherwise `flat`.
    """

    return classify_day_regimes(candles_by_scope=candles_by_instrument)


def classify_day_regimes(
    candles_by_scope: dict[str, list[PricePathPoint]],
) -> TrendClassification:
    """Classify market regime for each instrument/timeframe scope.

    Scope keys are typically `MOEX:SBER|5m`. The function also aggregates
    instrument-level returns for backwards-compatible daily summaries.
    """

    returns: dict[str, Decimal] = {}
    ranges: dict[str, Decimal] = {}
    regimes: dict[str, str] = {}
    instrument_returns: dict[str, list[Decimal]] = defaultdict(list)
    for scope, candles in candles_by_scope.items():
        ordered = sorted(candles, key=lambda candle: candle.ts_utc)
        if not ordered:
            continue
        first_open = ordered[0].open_price
        last_close = ordered[-1].close_price
        if first_open <= ZERO:
            continue
        return_bps = ((last_close - first_open) / first_open) * TEN_THOUSAND
        daily_range = max(point.high_price for point in ordered) - min(
            point.low_price for point in ordered
        )
        range_bps = (daily_range / first_open) * TEN_THOUSAND
        returns[scope] = return_bps.quantize(Decimal("0.0001"))
        ranges[scope] = range_bps.quantize(Decimal("0.0001"))
        regimes[scope] = _regime_for(return_bps=return_bps, range_bps=range_bps)
        instrument_id = scope.split("|", maxsplit=1)[0]
        instrument_returns[instrument_id].append(return_bps)

    if not returns:
        return TrendClassification(
            market_regime="flat",
            average_return_bps=ZERO,
            instrument_returns_bps={},
            scope_returns_bps={},
            scope_range_bps={},
            regime_by_scope={},
        )

    average = sum(returns.values(), ZERO) / Decimal(len(returns))
    average_range = sum(ranges.values(), ZERO) / Decimal(len(ranges))
    regime = _regime_for(return_bps=average, range_bps=average_range)
    if regimes and _all_same(tuple(regimes.values())):
        regime = next(iter(regimes.values()))
    elif regime == "flat" and "choppy" in set(regimes.values()):
        regime = "choppy"

    instrument_average_returns = {
        instrument_id: (sum(values, ZERO) / Decimal(len(values))).quantize(Decimal("0.0001"))
        for instrument_id, values in instrument_returns.items()
        if values
    }
    return TrendClassification(
        market_regime=regime,
        average_return_bps=average.quantize(Decimal("0.0001")),
        instrument_returns_bps=instrument_average_returns,
        scope_returns_bps=returns,
        scope_range_bps=ranges,
        regime_by_scope=regimes,
    )


def _regime_for(*, return_bps: Decimal, range_bps: Decimal) -> str:
    if return_bps >= TREND_UP_THRESHOLD_BPS:
        return "trend_up"
    if return_bps <= TREND_DOWN_THRESHOLD_BPS:
        return "trend_down"
    if range_bps >= CHOPPY_RANGE_THRESHOLD_BPS:
        return "choppy"
    return "flat"


def _all_same(values: tuple[str, ...]) -> bool:
    if not values:
        return False
    return len(set(values)) == 1


def analyze_counterfactual(
    *,
    source: CounterfactualSource,
    price_path: Iterable[PricePathPoint],
    assumptions: AnalyticsAssumptions,
    windows_minutes: tuple[int, ...] = (5, 10, 15),
) -> CounterfactualAnalysis:
    """Evaluate missed/cancelled outcome over 5/10/15 minute windows."""

    ordered = tuple(sorted(price_path, key=lambda point: point.ts_utc))
    outcomes = _window_outcomes(
        source=source,
        price_path=ordered,
        assumptions=assumptions,
        windows_minutes=windows_minutes,
    )
    scenarios = {
        "blocked-as-if-entered": outcomes,
        "kept-limit-order": _kept_limit_order_outcomes(
            source=source,
            price_path=ordered,
            assumptions=assumptions,
            windows_minutes=windows_minutes,
        ),
        "aggressive-fill": _aggressive_fill_outcomes(
            source=source,
            price_path=ordered,
            assumptions=assumptions,
            windows_minutes=windows_minutes,
        ),
    }
    return CounterfactualAnalysis(
        source=source,
        windows=outcomes,
        assumptions=assumptions,
        scenarios=scenarios,
    )


def build_funnel_metrics(
    *,
    created: int,
    passed_gates: int,
    blockers: int,
    order_intent: int,
    posted: int,
    filled: int,
    exited: int,
    profitable: int,
) -> FunnelMetrics:
    return FunnelMetrics(
        created=created,
        passed_gates=passed_gates,
        blockers=blockers,
        order_intent=order_intent,
        posted=posted,
        filled=filled,
        exited=exited,
        profitable=profitable,
    )


def _window_outcomes(
    *,
    source: CounterfactualSource,
    price_path: tuple[PricePathPoint, ...],
    assumptions: AnalyticsAssumptions,
    windows_minutes: tuple[int, ...],
) -> dict[int, WindowOutcome]:
    outcomes: dict[int, WindowOutcome] = {}
    for window in windows_minutes:
        end_ts = source.event_ts + timedelta(minutes=window)
        window_points = tuple(
            point for point in price_path if source.event_ts < point.ts_utc <= end_ts
        )
        outcomes[window] = _window_outcome(
            source=source,
            window_minutes=window,
            price_path=window_points,
            assumptions=assumptions,
        )
    return outcomes


def _kept_limit_order_outcomes(
    *,
    source: CounterfactualSource,
    price_path: tuple[PricePathPoint, ...],
    assumptions: AnalyticsAssumptions,
    windows_minutes: tuple[int, ...],
) -> dict[int, WindowOutcome]:
    touched_at = _first_limit_touch(source=source, price_path=price_path)
    if touched_at is None:
        return {
            window: _empty_window_outcome(window_minutes=window)
            for window in windows_minutes
        }
    limit_source = CounterfactualSource(
        candidate_id=source.candidate_id,
        order_intent_id=source.order_intent_id,
        source_event_type=source.source_event_type,
        instrument_id=source.instrument_id,
        strategy_id=source.strategy_id,
        side=source.side,
        event_ts=touched_at,
        entry_price=source.entry_price,
        lot_qty=source.lot_qty,
        blocker_code=source.blocker_code,
        cancel_reason_code=source.cancel_reason_code,
        timeframe=source.timeframe,
        strategy_version=source.strategy_version,
    )
    return _window_outcomes(
        source=limit_source,
        price_path=price_path,
        assumptions=assumptions,
        windows_minutes=windows_minutes,
    )


def _aggressive_fill_outcomes(
    *,
    source: CounterfactualSource,
    price_path: tuple[PricePathPoint, ...],
    assumptions: AnalyticsAssumptions,
    windows_minutes: tuple[int, ...],
) -> dict[int, WindowOutcome]:
    direction = Decimal("1") if source.side.lower() == "buy" else Decimal("-1")
    adjusted_entry = source.entry_price * (
        Decimal("1") + (direction * assumptions.slippage_bps / TEN_THOUSAND)
    )
    aggressive_source = CounterfactualSource(
        candidate_id=source.candidate_id,
        order_intent_id=source.order_intent_id,
        source_event_type=source.source_event_type,
        instrument_id=source.instrument_id,
        strategy_id=source.strategy_id,
        side=source.side,
        event_ts=source.event_ts,
        entry_price=adjusted_entry,
        lot_qty=source.lot_qty,
        blocker_code=source.blocker_code,
        cancel_reason_code=source.cancel_reason_code,
        timeframe=source.timeframe,
        strategy_version=source.strategy_version,
    )
    return _window_outcomes(
        source=aggressive_source,
        price_path=price_path,
        assumptions=assumptions,
        windows_minutes=windows_minutes,
    )


def _first_limit_touch(
    *,
    source: CounterfactualSource,
    price_path: tuple[PricePathPoint, ...],
) -> datetime | None:
    for point in price_path:
        if point.ts_utc <= source.event_ts:
            continue
        if source.side.lower() == "buy" and point.low_price <= source.entry_price:
            return point.ts_utc
        if source.side.lower() != "buy" and point.high_price >= source.entry_price:
            return point.ts_utc
    return None


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
        return _empty_window_outcome(window_minutes=window_minutes)

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
    gross_pnl_bps = directional_return
    gross_pnl_rub = source.entry_price * Decimal(source.lot_qty) * gross_pnl_bps / TEN_THOUSAND
    net_pnl_bps = gross_pnl_bps - assumptions.total_cost_bps
    net_pnl_rub = source.entry_price * Decimal(source.lot_qty) * net_pnl_bps / TEN_THOUSAND

    return WindowOutcome(
        window_minutes=window_minutes,
        mfe_bps=mfe.quantize(Decimal("0.0001")),
        mae_bps=mae.quantize(Decimal("0.0001")),
        close_return_bps=directional_return.quantize(Decimal("0.0001")),
        gross_pnl_bps=gross_pnl_bps.quantize(Decimal("0.0001")),
        gross_pnl_rub=gross_pnl_rub.quantize(Decimal("0.0001")),
        net_pnl_bps=net_pnl_bps.quantize(Decimal("0.0001")),
        net_pnl_rub=net_pnl_rub.quantize(Decimal("0.0001")),
        theoretical_pnl_bps=net_pnl_bps.quantize(Decimal("0.0001")),
        theoretical_pnl_rub=net_pnl_rub.quantize(Decimal("0.0001")),
        would_profit=net_pnl_bps > ZERO,
        tp_hit=mfe >= assumptions.take_profit_bps,
        sl_hit=mae <= -assumptions.stop_loss_bps,
    )


def _empty_window_outcome(*, window_minutes: int) -> WindowOutcome:
    return WindowOutcome(
        window_minutes=window_minutes,
        mfe_bps=None,
        mae_bps=None,
        close_return_bps=None,
        gross_pnl_bps=None,
        gross_pnl_rub=None,
        net_pnl_bps=None,
        net_pnl_rub=None,
        theoretical_pnl_bps=None,
        theoretical_pnl_rub=None,
        would_profit=None,
        tp_hit=None,
        sl_hit=None,
    )

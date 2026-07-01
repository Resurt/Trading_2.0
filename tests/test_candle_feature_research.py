from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from scripts.run_candle_feature_research import (
    CandlePoint,
    EvaluationMetrics,
    ResearchConfig,
    _short_available_from_payloads,
    build_report_payload,
    classify_result,
    compute_feature_rows,
    evaluate_configs,
    normalize_instruments,
    outcome_for_horizon,
    split_trading_dates,
    total_cost_bps,
)


def test_feature_computation_does_not_use_future_close_in_returns() -> None:
    candles = _candles([100.0] * 13 + [110.0, 111.0, 112.0, 113.0])

    features = compute_feature_rows(candles, selected_timeframes={"5m"})

    first = features[0]
    assert first.return_1_bar_bps == 0
    assert first.outcomes.future_return_5m_bps is not None
    assert first.outcomes.future_return_5m_bps > 900


def test_horizon_outcome_requires_exact_target_timestamp() -> None:
    candle = _candle_at(datetime(2026, 1, 1, 7, 50, tzinfo=UTC), close_price=100.0)
    target = _candle_at(datetime(2026, 1, 1, 8, 5, tzinfo=UTC), close_price=101.0)

    outcome = outcome_for_horizon(candle, [target], [target.close_ts_utc], 15)

    assert outcome.horizon_valid is True
    assert outcome.requested_horizon_minutes == 15
    assert outcome.actual_horizon_minutes == 15
    assert outcome.exit_alignment_seconds == 0
    assert outcome.future_return_bps == 100.0


def test_horizon_outcome_does_not_use_next_bucket_beyond_requested_horizon() -> None:
    candle = _candle_at(datetime(2026, 1, 1, 7, 50, tzinfo=UTC), close_price=100.0)
    twenty_minute_exit = _candle_at(datetime(2026, 1, 1, 8, 10, tzinfo=UTC), close_price=102.0)

    outcome = outcome_for_horizon(
        candle,
        [twenty_minute_exit],
        [twenty_minute_exit.close_ts_utc],
        15,
    )

    assert outcome.horizon_valid is False
    assert outcome.future_return_bps is None
    assert outcome.actual_exit_ts_utc is None
    assert outcome.exit_alignment == "missing_exact_target"


def test_train_validation_split_uses_trading_date_order() -> None:
    dates = [date(2026, 1, day) for day in range(1, 11)]

    train, validation = split_trading_dates(dates)

    assert max(train) < min(validation)
    assert len(train) == 7
    assert len(validation) == 3


def test_special_days_are_excluded_by_default() -> None:
    candles = _candles([100.0 + index for index in range(16)], special_index=12)

    features = compute_feature_rows(candles, selected_timeframes={"5m"})

    assert all(not feature.special_day for feature in features)
    assert all(feature.close_ts_utc != candles[12].close_ts_utc for feature in features)


def test_total_cost_floor_is_not_below_ten_bps() -> None:
    assert total_cost_bps(commission_bps_per_side=1.0, slippage_bps=0.0) == 10.0
    assert total_cost_bps(commission_bps_per_side=5.0, slippage_bps=2.0) == 12.0


def test_negative_validation_result_is_rejected() -> None:
    train = EvaluationMetrics(
        candidates=200,
        gross_pnl_bps_proxy=500,
        net_pnl_bps_proxy=100,
        average_net_bps_proxy=0.5,
        win_proxy=0.51,
        active_days=20,
        max_bad_day_bps_proxy=-20,
        top_day_contribution=0.2,
    )
    validation = EvaluationMetrics(
        candidates=200,
        gross_pnl_bps_proxy=100,
        net_pnl_bps_proxy=-1,
        average_net_bps_proxy=-0.005,
        win_proxy=0.49,
        active_days=20,
        max_bad_day_bps_proxy=-20,
        top_day_contribution=0.2,
    )

    passed, reasons = classify_result(
        ResearchConfig(config_id="test", hypothesis="momentum_continuation", horizon_minutes=5),
        train,
        validation,
        min_validation_candidates=100,
    )

    assert not passed
    assert "validation_net_not_positive" in reasons


def test_too_few_validation_candidates_are_rejected() -> None:
    train = _positive_metrics(candidates=200)
    validation = _positive_metrics(candidates=99)

    passed, reasons = classify_result(
        ResearchConfig(config_id="test", hypothesis="momentum_continuation", horizon_minutes=5),
        train,
        validation,
        min_validation_candidates=100,
    )

    assert not passed
    assert "too_few_validation_candidates" in reasons


def test_json_report_payload_is_valid() -> None:
    candles = _candles([100.0 + index for index in range(25)])
    features = compute_feature_rows(candles, selected_timeframes={"5m"})
    train_dates, validation_dates = split_trading_dates(
        [feature.trading_date for feature in features]
    )
    configs = [
        ResearchConfig(
            config_id="test",
            hypothesis="momentum_continuation",
            horizon_minutes=5,
            return_bars=1,
            return_threshold_bps=1,
        )
    ]
    results = evaluate_configs(
        features,
        configs=configs,
        train_dates=train_dates,
        validation_dates=validation_dates,
        total_cost_bps_value=12,
        min_validation_candidates=100,
        short_available_by_instrument={},
        enforce_short_availability=False,
    )

    payload = build_report_payload(
        features=features,
        configs=configs,
        results=results,
        from_date=min(feature.trading_date for feature in features),
        to_date=max(feature.trading_date for feature in features),
        instruments=("MOEX:SBER",),
        timeframes=("5m",),
        sessions=("weekday_main",),
        total_cost=12,
        sides=("long",),
        short_available_by_instrument={},
        enforce_short_availability=False,
        exclude_dividend_windows_for_shorts=False,
        train_dates=train_dates,
        validation_dates=validation_dates,
        dry_run=True,
    )

    encoded = json.dumps(payload)
    assert json.loads(encoded)["real_orders_disabled"] is True


def test_expanded_instruments_normalize_without_whitelist() -> None:
    assert normalize_instruments("YDEX,TATN,GMKN,OZON,VTBR") == (
        "MOEX:YDEX",
        "MOEX:TATN",
        "MOEX:GMKN",
        "MOEX:OZON",
        "MOEX:VTBR",
    )


def test_short_outcome_uses_negative_future_return_minus_costs() -> None:
    candles = _candles([100.0] * 13 + [99.0, 98.0, 97.0, 96.0, 95.0])
    features = compute_feature_rows(candles, selected_timeframes={"5m"})
    train_dates, validation_dates = split_trading_dates(
        [feature.trading_date for feature in features]
    )
    config = ResearchConfig(
        config_id="short-test",
        hypothesis="momentum_breakdown",
        horizon_minutes=5,
        side="short",
        return_bars=1,
        return_threshold_bps=1,
    )

    results = evaluate_configs(
        features,
        configs=[config],
        train_dates=train_dates,
        validation_dates=validation_dates,
        total_cost_bps_value=12,
        min_validation_candidates=1,
        short_available_by_instrument={"MOEX:SBER": True},
        enforce_short_availability=True,
    )

    assert results[0].full.candidates > 0
    assert results[0].full.net_pnl_bps_proxy > 0


def test_short_rejected_when_broker_short_unavailable() -> None:
    candles = _candles([100.0] * 13 + [99.0, 98.0, 97.0, 96.0, 95.0])
    features = compute_feature_rows(candles, selected_timeframes={"5m"})
    train_dates, validation_dates = split_trading_dates(
        [feature.trading_date for feature in features]
    )
    config = ResearchConfig(
        config_id="short-test",
        hypothesis="momentum_breakdown",
        horizon_minutes=5,
        side="short",
        return_bars=1,
        return_threshold_bps=1,
        short_available_by_broker=False,
    )

    results = evaluate_configs(
        features,
        configs=[config],
        train_dates=train_dates,
        validation_dates=validation_dates,
        total_cost_bps_value=12,
        min_validation_candidates=1,
        short_available_by_instrument={"MOEX:SBER": False},
        enforce_short_availability=True,
    )

    assert results[0].full.candidates == 0
    assert "short_not_available_by_broker" in results[0].rejection_reasons


def test_short_available_loaded_from_registry_payloads() -> None:
    assert _short_available_from_payloads({"short_available": True}, {}) is True
    assert _short_available_from_payloads({}, {"short_available": True}) is True
    assert (
        _short_available_from_payloads({"short_available": False}, {"short_available": True})
        is False
    )
    assert _short_available_from_payloads({}, {}) is False


def _positive_metrics(*, candidates: int) -> EvaluationMetrics:
    return EvaluationMetrics(
        candidates=candidates,
        gross_pnl_bps_proxy=500,
        net_pnl_bps_proxy=100,
        average_net_bps_proxy=100 / candidates,
        win_proxy=0.6,
        active_days=20,
        max_bad_day_bps_proxy=-10,
        top_day_contribution=0.2,
    )


def _candles(prices: list[float], *, special_index: int | None = None) -> list[CandlePoint]:
    start = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)
    candles: list[CandlePoint] = []
    for index, close_price in enumerate(prices):
        open_ts = start + timedelta(minutes=5 * index)
        close_ts = open_ts + timedelta(minutes=5)
        is_special = index == special_index
        candles.append(
            CandlePoint(
                instrument_id="MOEX:SBER",
                timeframe="5m",
                trading_date=date(2026, 1, min(28, 1 + index // 4)),
                session_type="weekday_main",
                open_ts_utc=open_ts,
                close_ts_utc=close_ts,
                open_price=close_price,
                high_price=close_price + 0.1,
                low_price=close_price - 0.1,
                close_price=close_price,
                volume_lots=1000,
                is_special_day=is_special,
                special_day_types=("dividend_gap_day",) if is_special else (),
            )
        )
    return candles


def _candle_at(close_ts: datetime, *, close_price: float) -> CandlePoint:
    open_ts = close_ts - timedelta(minutes=5)
    return CandlePoint(
        instrument_id="MOEX:SBER",
        timeframe="5m",
        trading_date=close_ts.date(),
        session_type="weekday_main",
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
        open_price=close_price,
        high_price=close_price,
        low_price=close_price,
        close_price=close_price,
        volume_lots=1000,
    )

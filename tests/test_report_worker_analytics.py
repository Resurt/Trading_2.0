from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from report_worker.analytics import (
    AnalyticsAssumptions,
    CounterfactualSource,
    PricePathPoint,
    ReportAnalyticsService,
    analyze_counterfactual,
    classify_day_trend,
)
from trading_common.db.base import Base
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CounterfactualResult,
    FillEvent,
    MarketCandle,
    OrderIntent,
    SessionRun,
    SignalCandidate,
    StrategyStateEvent,
)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def context_values() -> dict[str, object]:
    return {
        "calendar_date": date(2026, 6, 12),
        "trading_date": date(2026, 6, 12),
        "session_type": "weekday_main",
        "session_phase": "continuous_trading",
        "micro_session_id": "2026-06-12:weekday_main:1000",
        "broker_trading_status": "normal_trading",
    }


def test_day_trend_classification_is_reproducible() -> None:
    trend = classify_day_trend(
        {
            "MOEX:SBER": [
                PricePathPoint(
                    utc(2026, 6, 12, 7),
                    Decimal("100"),
                    Decimal("101"),
                    Decimal("99"),
                    Decimal("100"),
                ),
                PricePathPoint(
                    utc(2026, 6, 12, 18),
                    Decimal("100"),
                    Decimal("103"),
                    Decimal("100"),
                    Decimal("102"),
                ),
            ],
            "MOEX:GAZP": [
                PricePathPoint(
                    utc(2026, 6, 12, 7),
                    Decimal("200"),
                    Decimal("201"),
                    Decimal("199"),
                    Decimal("200"),
                ),
                PricePathPoint(
                    utc(2026, 6, 12, 18),
                    Decimal("200"),
                    Decimal("204"),
                    Decimal("200"),
                    Decimal("203"),
                ),
            ],
        }
    )

    assert trend.market_regime == "trend_up"
    assert trend.instrument_returns_bps["MOEX:SBER"] == Decimal("200.0000")
    assert trend.instrument_returns_bps["MOEX:GAZP"] == Decimal("150.0000")
    assert trend.regime_by_scope["MOEX:SBER"] == "trend_up"


def test_counterfactual_mfe_mae_and_theoretical_pnl() -> None:
    source = CounterfactualSource(
        candidate_id=uuid4(),
        order_intent_id=None,
        source_event_type="blocked_candidate",
        instrument_id="MOEX:SBER",
        strategy_id="baseline",
        side="buy",
        event_ts=utc(2026, 6, 12, 7),
        entry_price=Decimal("100"),
        lot_qty=10,
        blocker_code="spread_too_wide",
        cancel_reason_code=None,
    )
    analysis = analyze_counterfactual(
        source=source,
        price_path=[
            PricePathPoint(
                utc(2026, 6, 12, 7, 5),
                Decimal("100"),
                Decimal("101"),
                Decimal("99.5"),
                Decimal("100.8"),
            ),
            PricePathPoint(
                utc(2026, 6, 12, 7, 10),
                Decimal("100.8"),
                Decimal("102"),
                Decimal("100.7"),
                Decimal("101.5"),
            ),
            PricePathPoint(
                utc(2026, 6, 12, 7, 15),
                Decimal("101.5"),
                Decimal("103"),
                Decimal("101"),
                Decimal("102"),
            ),
        ],
        assumptions=AnalyticsAssumptions(
            fee_bps=Decimal("2"),
            slippage_bps=Decimal("2"),
            take_profit_bps=Decimal("100"),
            stop_loss_bps=Decimal("60"),
        ),
    )

    assert analysis.windows[5].mfe_bps == Decimal("100.0000")
    assert analysis.windows[5].mae_bps == Decimal("-50.0000")
    assert analysis.windows[5].tp_hit is True
    assert analysis.windows[5].sl_hit is False
    assert analysis.windows[15].would_profit is True
    assert analysis.windows[15].gross_pnl_bps == Decimal("200.0000")
    assert analysis.windows[15].net_pnl_bps == Decimal("196.0000")
    assert set(analysis.scenarios) == {
        "blocked-as-if-entered",
        "kept-limit-order",
        "aggressive-fill",
    }


def test_report_service_builds_hourly_daily_and_counterfactual_reports() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = utc(2026, 6, 12, 7)

    with Session(engine) as session:
        candidate_id = uuid4()
        request_order_id = uuid4()
        session.add_all(
            [
                SessionRun(
                    **context_values(),
                    strategy_id="baseline",
                    strategy_version=1,
                    status="closed",
                    started_at=now,
                    ended_at=now + timedelta(hours=1),
                    freeze_started_at=None,
                    report_requested_at=now + timedelta(hours=1),
                    close_reason_code="hourly_rollover",
                    run_payload={},
                ),
                SignalCandidate(
                    **context_values(),
                    candidate_id=candidate_id,
                    ts_utc=now,
                    exchange_ts=None,
                    received_ts=None,
                    run_id=None,
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    strategy_version=1,
                    timeframe="5m",
                    side="buy",
                    signal_type="entry",
                    candidate_status="blocked",
                    expected_edge_bps=Decimal("20"),
                    expected_holding_minutes=5,
                    last_price=Decimal("100"),
                    mid_price=Decimal("100"),
                    spread_abs=Decimal("0.1"),
                    spread_bps=Decimal("10"),
                    market_quality_score=Decimal("0.9"),
                    book_imbalance=Decimal("0"),
                    candle_age_ms=100,
                    data_freshness_ms=100,
                    signal_fingerprint="sig-1",
                    signal_payload={"lot_qty": 10},
                ),
                BlockerEvent(
                    **context_values(),
                    ts_utc=now,
                    exchange_ts=None,
                    received_ts=None,
                    candidate_id=candidate_id,
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    gate_name="spread_limit",
                    gate_rank=1,
                    passed=False,
                    reason_code="spread_too_wide",
                    reason_payload={},
                    is_final_blocker=True,
                    blocker_rank=1,
                    market_quality_score=Decimal("0.9"),
                    spread_bps=Decimal("10"),
                    expected_edge_bps=Decimal("20"),
                ),
                OrderIntent(
                    **context_values(),
                    candidate_id=candidate_id,
                    instrument_id="MOEX:SBER",
                    strategy_id="baseline",
                    side="buy",
                    order_action="place",
                    order_type="limit",
                    lot_qty=10,
                    intended_price=Decimal("100"),
                    time_in_force="day",
                    request_order_id=request_order_id,
                    idempotency_key="baseline:test",
                    execution_policy_version=1,
                    status="cancelled",
                    cancel_reason_code="stale_order",
                    reject_reason_code=None,
                    created_ts=now,
                    submitted_ts=now + timedelta(seconds=1),
                    terminal_ts=now + timedelta(minutes=1),
                    intent_payload={},
                ),
                BrokerOrder(
                    **context_values(),
                    order_intent_id=None,
                    request_order_id=request_order_id,
                    exchange_order_id="exchange-1",
                    broker_status="cancelled",
                    lifecycle_seq=2,
                    posted_at=now,
                    cancelled_at=now + timedelta(minutes=1),
                    rejected_at=None,
                    reject_reason_code=None,
                    broker_tracking_id="tracking",
                    last_observed_at=now + timedelta(minutes=1),
                    broker_payload={"latency_ms": 120},
                ),
                FillEvent(
                    **context_values(),
                    ts_utc=now + timedelta(minutes=2),
                    exchange_ts=None,
                    received_ts=None,
                    request_order_id=request_order_id,
                    exchange_order_id="exchange-1",
                    broker_fill_id="fill-1",
                    instrument_id="MOEX:SBER",
                    side="sell",
                    lot_qty=10,
                    price=Decimal("101"),
                    commission=Decimal("1"),
                    liquidity_flag=None,
                    fill_payload={"estimated_slippage": "0.2"},
                ),
                StrategyStateEvent(
                    **context_values(),
                    ts_utc=now,
                    exchange_ts=None,
                    received_ts=None,
                    strategy_id="baseline",
                    strategy_version=1,
                    instrument_id="MOEX:SBER",
                    previous_state="wait",
                    new_state="candidate",
                    event_type="strategy_state_changed",
                    reason_code=None,
                    state_payload={},
                ),
                StrategyStateEvent(
                    **context_values(),
                    ts_utc=now + timedelta(minutes=5),
                    exchange_ts=None,
                    received_ts=None,
                    strategy_id="baseline",
                    strategy_version=1,
                    instrument_id="MOEX:SBER",
                    previous_state="candidate",
                    new_state="blocked",
                    event_type="strategy_state_changed",
                    reason_code="spread_too_wide",
                    state_payload={},
                ),
            ]
        )
        candle_prices = (
            (5, Decimal("100.8")),
            (10, Decimal("101.5")),
            (15, Decimal("102")),
        )
        for minute, close_price in candle_prices:
            session.add(
                MarketCandle(
                    **context_values(),
                    instrument_id="MOEX:SBER",
                    timeframe="5m",
                    open_ts_utc=now + timedelta(minutes=minute - 5),
                    close_ts_utc=now + timedelta(minutes=minute),
                    exchange_open_ts=now + timedelta(minutes=minute - 5),
                    exchange_close_ts=now + timedelta(minutes=minute),
                    open_price=Decimal("100"),
                    high_price=close_price + Decimal("0.5"),
                    low_price=Decimal("99.5"),
                    close_price=close_price,
                    volume_lots=Decimal("100"),
                    is_closed=True,
                    source="test",
                    candle_payload={},
                )
            )
        session.flush()

        service = ReportAnalyticsService(session)
        hourly = service.build_hourly_report(
            micro_session_id="2026-06-12:weekday_main:1000",
            strategy_id="baseline",
        )
        counterfactuals = service.run_counterfactual_analysis_for_date(
            trading_date=date(2026, 6, 12),
            strategy_id="baseline",
        )
        daily = service.build_daily_report(
            trading_date=date(2026, 6, 12),
            strategy_id="baseline",
        )

        assert hourly.signal_count == 1
        assert hourly.blocked_count == 1
        assert hourly.cancel_count == 1
        assert hourly.report_payload["risk_blockers"] == {"spread_too_wide": 1}
        assert len(counterfactuals) == 2
        assert (
            counterfactuals[0].result_payload["algorithm"]
            == "counterfactual_mfe_mae_realistic_scenarios_v2"
        )
        assert "scenarios" in counterfactuals[0].result_payload
        assert counterfactuals[0].pnl_net is not None
        assert daily.market_regime == "trend_up"
        funnel = cast(dict[str, object], daily.report_payload["funnel"])
        execution_quality = cast(dict[str, object], daily.report_payload["execution_quality"])
        assert funnel["candidates"] == 1
        assert funnel["created"] == 1
        assert funnel["order_intent"] == 1
        assert execution_quality["cancel_count"] == 1
        assert "html_output" in daily.report_payload
        blocker_ranking = cast(list[dict[str, object]], daily.report_payload["blocker_ranking"])
        assert blocker_ranking[0]["blocker_code"] == "spread_too_wide"
        cancel_analytics = cast(dict[str, object], daily.report_payload["canceled_order_analytics"])
        assert cancel_analytics["cancelled_intent_count"] == 1

        same_counterfactuals = service.run_counterfactual_analysis_for_date(
            trading_date=date(2026, 6, 12),
            strategy_id="baseline",
            force_rebuild=False,
        )
        assert len(same_counterfactuals) == 2
        assert session.scalar(select(func.count()).select_from(CounterfactualResult)) == 2

        rebuilt_daily = service.rebuild_reports_for_date(
            trading_date=date(2026, 6, 12),
            strategy_id="baseline",
            include_counterfactual=True,
        )
        missed_opportunity = cast(
            dict[str, object],
            rebuilt_daily.report_payload["missed_opportunity_summary"],
        )
        assert missed_opportunity["total_counterfactuals"] == 2

    engine.dispose()


def test_daily_report_handles_empty_data_deterministically() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        service = ReportAnalyticsService(session)
        first = service.build_daily_report(
            trading_date=date(2026, 6, 14),
            strategy_id="baseline",
        )
        second = service.build_daily_report(
            trading_date=date(2026, 6, 14),
            strategy_id="baseline",
            force_rebuild=False,
        )

        assert first.daily_report_id == second.daily_report_id
        assert first.market_regime == "flat"
        assert first.report_payload["funnel"] == second.report_payload["funnel"]
        assert first.report_payload["missed_opportunity_summary"] == {
            "would_profit_5m": 0,
            "would_profit_10m": 0,
            "would_profit_15m": 0,
            "missed_gross_pnl": "0.0000",
            "missed_net_pnl": "0.0000",
            "avoided_loss": "0.0000",
            "total_counterfactuals": 0,
        }

    engine.dispose()


def test_weekend_filtered_daily_report_and_canceled_counterfactual() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = utc(2026, 6, 13, 8)
    weekend_context = {
        "calendar_date": date(2026, 6, 13),
        "trading_date": date(2026, 6, 13),
        "session_type": "weekend",
        "session_phase": "continuous_trading",
        "micro_session_id": "2026-06-13:weekend:0800",
        "broker_trading_status": "normal_trading",
    }

    with Session(engine) as session:
        request_order_id = uuid4()
        order_intent_id = uuid4()
        session.add(
            OrderIntent(
                **weekend_context,
                order_intent_id=order_intent_id,
                candidate_id=None,
                instrument_id="MOEX:SBER",
                timeframe="5m",
                strategy_id="baseline",
                strategy_version=2,
                side="buy",
                order_action="place",
                order_type="limit",
                lot_qty=10,
                intended_price=Decimal("100"),
                time_in_force="day",
                request_order_id=request_order_id,
                idempotency_key="weekend-cancelled",
                execution_policy_version=1,
                status="cancelled",
                cancel_reason_code="stale_order",
                reject_reason_code=None,
                created_ts=now,
                submitted_ts=now,
                terminal_ts=now + timedelta(minutes=1),
                intent_payload={},
            )
        )
        for minute, close_price in (
            (5, Decimal("101")),
            (10, Decimal("102")),
            (15, Decimal("103")),
        ):
            session.add(
                MarketCandle(
                    **weekend_context,
                    instrument_id="MOEX:SBER",
                    timeframe="5m",
                    open_ts_utc=now + timedelta(minutes=minute - 5),
                    close_ts_utc=now + timedelta(minutes=minute),
                    exchange_open_ts=now + timedelta(minutes=minute - 5),
                    exchange_close_ts=now + timedelta(minutes=minute),
                    open_price=Decimal("100"),
                    high_price=close_price,
                    low_price=Decimal("99.8"),
                    close_price=close_price,
                    volume_lots=Decimal("100"),
                    is_closed=True,
                    source="test",
                    candle_payload={},
                )
            )
        session.flush()

        service = ReportAnalyticsService(session)
        counterfactuals = service.run_counterfactual_analysis_for_date(
            trading_date=date(2026, 6, 13),
            strategy_id="baseline",
            instrument_id="MOEX:SBER",
            timeframe="5m",
            session_type="weekend",
            strategy_version=2,
        )
        daily = service.build_daily_report(
            trading_date=date(2026, 6, 13),
            strategy_id="baseline",
            instrument_id="MOEX:SBER",
            timeframe="5m",
            session_type="weekend",
            strategy_version=2,
        )

        assert len(counterfactuals) == 1
        assert counterfactuals[0].cancel_reason_code == "stale_order"
        assert counterfactuals[0].source_event_type == "cancelled_order"
        assert counterfactuals[0].would_profit_15m is True
        assert daily.session_type == "weekend"
        assert daily.market_regime == "trend_up"
        cancel_analytics = cast(dict[str, object], daily.report_payload["canceled_order_analytics"])
        assert cancel_analytics["cancelled_intent_count"] == 1

    engine.dispose()

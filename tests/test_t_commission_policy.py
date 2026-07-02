from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trade_core.broker_gateway import InstrumentRef
from trade_core.market_data import FeedFreshness, MarketState, PriceLevel, Timeframe
from trade_core.session import SessionSnapshot
from trade_core.strategy import (
    BlockerCode,
    DefaultRiskEngine,
    RiskAssessmentInput,
    RiskBlocker,
    RiskLimits,
    SignalAction,
    SignalCandidateDecision,
    TradeSide,
    count_execution_events,
    estimate_next_execution_commission,
)
from trading_common.enums import SessionPhase, SessionType


def test_t_pro_commission_first_15_executed_trades_are_free() -> None:
    for executed_before in (0, 14):
        result = estimate_next_execution_commission(
            instrument_id="MOEX:T",
            executed_trades_today=executed_before,
            pro_subscription_active=True,
        )

        assert result.commission_bps == Decimal("0")
        assert result.free_commission_applies is True
        assert result.execution_count_scope == "executed_trade"


def test_t_pro_commission_16th_executed_trade_uses_fallback() -> None:
    result = estimate_next_execution_commission(
        instrument_id="MOEX:T",
        executed_trades_today=15,
        pro_subscription_active=True,
        fallback_commission_bps=Decimal("5"),
    )

    assert result.commission_bps == Decimal("5")
    assert result.reason_code == "t_pro_free_quota_exhausted"
    assert result.free_quota_remaining_before_trade == 0


def test_t_pro_unknown_does_not_assume_free_commission() -> None:
    result = estimate_next_execution_commission(
        instrument_id="T",
        executed_trades_today=0,
        pro_subscription_active=None,
        fallback_commission_bps=Decimal("5"),
    )

    assert result.instrument_id == "MOEX:T"
    assert result.commission_bps == Decimal("5")
    assert result.reason_code == "t_pro_subscription_unknown"


def test_regular_instruments_keep_project_default_commission() -> None:
    result = estimate_next_execution_commission(
        instrument_id="MOEX:SBER",
        executed_trades_today=0,
        pro_subscription_active=True,
        fallback_commission_bps=Decimal("5"),
    )

    assert result.commission_profile_id == "project_default_regular_equity"
    assert result.commission_bps == Decimal("5")
    assert result.free_executed_trades_per_day == 0


def test_partial_fills_count_as_execution_events_not_lots() -> None:
    fills = [{"lots": 1}, {"lots": 100}, {"lots": 3}]

    assert count_execution_events(fills) == 3


def test_risk_uses_t_zero_commission_only_when_pro_known_and_quota_available() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=_candidate("MOEX:T", expected_edge_bps=Decimal("9")),
            session_snapshot=_session(),
            market_state=_market_state(spread_bps=Decimal("0")),
            limits=RiskLimits(
                assumed_commission_bps_per_side=Decimal("5"),
                t_pro_subscription_active=True,
                t_executed_trades_today=14,
            ),
        )
    )

    total_cost_gate = _blocker(decision.blockers, BlockerCode.TOTAL_COSTS_EXCEED_EDGE)
    assert total_cost_gate.passed is True
    assert total_cost_gate.reason_payload["total_expected_costs_bps"] == "0"
    assert total_cost_gate.reason_payload["commission_bps_per_side"] == "0"


def test_risk_falls_back_after_t_pro_quota_exhausted() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=_candidate("MOEX:T", expected_edge_bps=Decimal("9")),
            session_snapshot=_session(),
            market_state=_market_state(spread_bps=Decimal("0")),
            limits=RiskLimits(
                assumed_commission_bps_per_side=Decimal("5"),
                t_pro_subscription_active=True,
                t_executed_trades_today=15,
            ),
        )
    )

    total_cost_gate = _blocker(decision.blockers, BlockerCode.TOTAL_COSTS_EXCEED_EDGE)
    assert total_cost_gate.passed is False
    assert total_cost_gate.reason_payload["total_expected_costs_bps"] == "10"
    assert total_cost_gate.reason_payload["commission_bps_per_side"] == "5"


def test_risk_can_block_t_entries_after_free_quota_when_configured() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=_candidate("MOEX:T", expected_edge_bps=Decimal("25")),
            session_snapshot=_session(),
            market_state=_market_state(spread_bps=Decimal("0")),
            limits=RiskLimits(
                assumed_commission_bps_per_side=Decimal("5"),
                t_pro_subscription_active=True,
                t_executed_trades_today=15,
                t_block_after_free_quota=True,
            ),
        )
    )

    quota_gate = _blocker(decision.blockers, BlockerCode.T_PRO_FREE_QUOTA_EXHAUSTED)
    assert quota_gate.passed is False
    assert quota_gate.reason_payload["block_new_entry_after_free_quota"] is True


def _session() -> SessionSnapshot:
    now = datetime(2026, 7, 2, 7, tzinfo=UTC)
    return SessionSnapshot(
        observed_at=now,
        calendar_date=date(2026, 7, 2),
        trading_date=date(2026, 7, 2),
        session_type=SessionType.WEEKDAY_MAIN,
        session_phase=SessionPhase.CONTINUOUS_TRADING,
        broker_phase=SessionPhase.CONTINUOUS_TRADING,
        broker_trading_status="normal_trading",
        broker_api_trade_available=True,
        schedule_phase=SessionPhase.CONTINUOUS_TRADING,
        schedule_window_start_at=now,
        schedule_window_end_at=now + timedelta(hours=1),
        micro_session_id="2026-07-02:weekday_main:1000",
        is_trading_allowed=True,
        deny_reason_code=None,
        status_mismatch=False,
    )


def _candidate(instrument_id: str, *, expected_edge_bps: Decimal) -> SignalCandidateDecision:
    ticker = instrument_id.rsplit(":", 1)[-1]
    return SignalCandidateDecision(
        strategy_id="baseline_config_stub",
        strategy_version=1,
        instrument=InstrumentRef(
            instrument_id=instrument_id,
            instrument_uid=f"uid-{ticker.lower()}",
            class_code="TQBR",
            ticker=ticker,
            lot_size=1,
            min_price_increment=Decimal("0.02"),
        ),
        timeframe=Timeframe.M5,
        action=SignalAction.ENTRY,
        side=TradeSide.BUY,
        order_type="limit",
        lot_qty=1,
        intended_price=Decimal("100"),
        time_in_force="day",
        expected_edge_bps=expected_edge_bps,
        expected_holding_minutes=5,
        signal_fingerprint="candidate-fingerprint",
        condition_payload={},
        lot_size=1,
        min_price_increment=Decimal("0.02"),
    )


def _market_state(*, spread_bps: Decimal) -> MarketState:
    mid = Decimal("100")
    spread_abs = mid * spread_bps / Decimal("10000")
    return MarketState(
        instrument_id="MOEX:T",
        best_bid=PriceLevel(price=mid - (spread_abs / Decimal("2")), quantity_lots=Decimal("10")),
        best_ask=PriceLevel(price=mid + (spread_abs / Decimal("2")), quantity_lots=Decimal("10")),
        mid_price=mid,
        spread_abs=spread_abs,
        spread_bps=spread_bps,
        bid_depth_lots=Decimal("100"),
        ask_depth_lots=Decimal("100"),
        book_imbalance=Decimal("0"),
        market_quality_score=Decimal("0.95"),
        feed_freshness=FeedFreshness(age_ms=100, is_stale=False),
    )


def _blocker(blockers: tuple[RiskBlocker, ...], code: BlockerCode) -> RiskBlocker:
    return next(item for item in blockers if item.code == code)

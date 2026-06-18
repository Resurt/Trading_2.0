from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    CancelOrderRequest,
    InstrumentRef,
    OrderPlacementRequest,
    OrdersRequest,
    OrderStateRequest,
)
from trade_core.market_data import Bar, FeedFreshness, MarketState, PriceLevel, Timeframe
from trade_core.session import SessionSnapshot
from trade_core.strategy import (
    BlockerCode,
    CancelReasonCode,
    ConfigDrivenStrategyConfig,
    ConfigDrivenStrategyEngine,
    DefaultExecutionEngine,
    DefaultReconciliationService,
    DefaultRiskEngine,
    OrderIntentRequest,
    PortfolioSnapshot,
    RiskAssessmentInput,
    RiskLimits,
    SignalAction,
    SignalCandidateDecision,
    SqlAlchemyStrategyEventStore,
    StrategyEvaluationContext,
    StrategyState,
    TradeSide,
)
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.base import Base
from trading_common.db.models import (
    BlockerEvent,
    CandidateStageResult,
    FillEvent,
    MarketContextSnapshot,
    OrderStateEvent,
    RiskEvent,
    StrategyStateEvent,
)
from trading_common.db.repositories import (
    AnalyticsReadRepository,
    BlockerEventRepository,
    CandidateStageResultRepository,
    MarketContextSnapshotRepository,
    OrderRepository,
    RiskEventRepository,
    SignalCandidateRepository,
    StrategyStateEventRepository,
)
from trading_common.enums import SessionPhase, SessionType

MSK = ZoneInfo("Europe/Moscow")


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def snapshot(
    *,
    session_type: SessionType = SessionType.WEEKDAY_MAIN,
    session_phase: SessionPhase = SessionPhase.CONTINUOUS_TRADING,
    allowed: bool = True,
    micro_session_id: str = "2026-06-12:weekday_main:1000",
) -> SessionSnapshot:
    now = utc(2026, 6, 12, 7)
    return SessionSnapshot(
        observed_at=now,
        calendar_date=date(2026, 6, 12),
        trading_date=date(2026, 6, 12),
        session_type=session_type,
        session_phase=session_phase,
        broker_phase=session_phase,
        broker_trading_status="normal_trading",
        broker_api_trade_available=allowed,
        schedule_phase=session_phase,
        schedule_window_start_at=now,
        schedule_window_end_at=now + timedelta(hours=1),
        micro_session_id=micro_session_id,
        is_trading_allowed=allowed,
        deny_reason_code=None if allowed else "session_forbidden",
        status_mismatch=False,
    )


def bar(timeframe: Timeframe, *, close_price: Decimal) -> Bar:
    open_ts = utc(2026, 6, 12, 7)
    close_ts = open_ts + timedelta(minutes=timeframe.minutes)
    return Bar(
        instrument_id="MOEX:SBER",
        timeframe=timeframe,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
        exchange_open_ts=open_ts.astimezone(MSK),
        exchange_close_ts=close_ts.astimezone(MSK),
        open_price=Decimal("100"),
        high_price=max(Decimal("100"), close_price),
        low_price=min(Decimal("100"), close_price),
        close_price=close_price,
        volume_lots=Decimal("10"),
        source_candle_count=timeframe.minutes,
    )


def market_state(*, spread_bps: Decimal = Decimal("5")) -> MarketState:
    mid = Decimal("100")
    spread_abs = mid * spread_bps / Decimal("10000")
    return MarketState(
        instrument_id="MOEX:SBER",
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


def instrument() -> InstrumentRef:
    return InstrumentRef(
        instrument_id="MOEX:SBER",
        instrument_uid="uid-sber",
        class_code="TQBR",
        ticker="SBER",
    )


def candidate(
    *,
    side: TradeSide = TradeSide.BUY,
    expected_edge_bps: Decimal = Decimal("25"),
    lot_qty: int = 1,
) -> SignalCandidateDecision:
    return SignalCandidateDecision(
        strategy_id="baseline_config_stub",
        strategy_version=1,
        instrument=instrument(),
        timeframe=Timeframe.M5,
        action=SignalAction.ENTRY,
        side=side,
        order_type="limit",
        lot_qty=lot_qty,
        intended_price=Decimal("100.00"),
        time_in_force="day",
        expected_edge_bps=expected_edge_bps,
        expected_holding_minutes=5,
        signal_fingerprint="candidate-fingerprint",
        condition_payload={"test": True},
        candidate_id=uuid4(),
    )


def test_config_strategy_emits_candidates_for_5m_10m_15m_closed_bars() -> None:
    engine = ConfigDrivenStrategyEngine(ConfigDrivenStrategyConfig.conservative_default())
    decision = engine.evaluate(
        StrategyEvaluationContext(
            instrument=instrument(),
            session_snapshot=snapshot(),
            latest_closed_bars={
                Timeframe.M5: bar(Timeframe.M5, close_price=Decimal("100.20")),
                Timeframe.M10: bar(Timeframe.M10, close_price=Decimal("100.30")),
                Timeframe.M15: bar(Timeframe.M15, close_price=Decimal("100.40")),
            },
            market_state=market_state(),
            current_state=StrategyState.WAIT,
        )
    )

    assert decision.next_state is StrategyState.CANDIDATE
    assert [item.timeframe for item in decision.candidates] == [
        Timeframe.M5,
        Timeframe.M10,
        Timeframe.M15,
    ]
    assert {item.action for item in decision.candidates} == {SignalAction.ENTRY}


def test_config_strategy_disables_weekend_template() -> None:
    engine = ConfigDrivenStrategyEngine(ConfigDrivenStrategyConfig.conservative_default())
    decision = engine.evaluate(
        StrategyEvaluationContext(
            instrument=instrument(),
            session_snapshot=snapshot(session_type=SessionType.WEEKEND),
            latest_closed_bars={Timeframe.M5: bar(Timeframe.M5, close_price=Decimal("101"))},
            market_state=market_state(),
            current_state=StrategyState.IDLE,
        )
    )

    assert decision.candidates == ()
    assert decision.next_state is StrategyState.WAIT
    assert decision.reason_code == "strategy_disabled"


def test_risk_engine_uses_explicit_blocker_catalog_and_final_blocker() -> None:
    risk = DefaultRiskEngine()
    assert set(risk.blocker_catalog()) == set(BlockerCode)

    decision = risk.evaluate(
        RiskAssessmentInput(
            candidate=candidate(),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("35")),
            limits=RiskLimits(max_spread_bps=Decimal("10")),
            portfolio=PortfolioSnapshot(),
        )
    )

    assert not decision.allowed
    assert decision.final_blocker is not None
    assert decision.final_blocker.code is BlockerCode.SPREAD_TOO_WIDE
    assert {blocker.code for blocker in decision.blockers} >= {
        BlockerCode.SPREAD_TOO_WIDE,
        BlockerCode.MARKET_QUALITY_LOW,
        BlockerCode.STALE_MARKET_DATA,
        BlockerCode.NO_EDGE_AFTER_COSTS,
        BlockerCode.RISK_BUDGET_EXCEEDED,
        BlockerCode.OPEN_ORDER_CONFLICT,
        BlockerCode.POSITION_LIMIT_REACHED,
    }


def test_long_candidate_passes_when_long_is_allowed() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.BUY),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(
                allow_long=True,
                max_long_lots=5,
                max_gross_exposure_rub=Decimal("1000000"),
                max_net_exposure_rub=Decimal("1000000"),
            ),
            portfolio=PortfolioSnapshot(),
        )
    )

    assert decision.allowed
    assert decision.final_blocker is None
    assert {
        blocker.gate_name
        for blocker in decision.blockers
        if blocker.code
        in {
            BlockerCode.TOTAL_COSTS_EXCEED_EDGE,
            BlockerCode.MAX_LONG_EXPOSURE_REACHED,
        }
    } == {"total_expected_costs", "max_gross_exposure", "max_net_exposure"}


def test_short_candidate_is_blocked_when_disabled_by_config() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.SELL),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(allow_short=False, max_short_lots=5),
            portfolio=PortfolioSnapshot(),
        )
    )

    assert not decision.allowed
    assert decision.final_blocker is not None
    assert decision.final_blocker.code is BlockerCode.SHORT_NOT_ALLOWED_BY_CONFIG
    assert decision.final_blocker.gate_name == "short_allowed_by_config"


def test_short_candidate_is_blocked_when_broker_or_account_disallows_short() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.SELL),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(
                allow_short=True,
                max_short_lots=5,
                short_allowed_by_account=False,
            ),
            portfolio=PortfolioSnapshot(),
        )
    )

    assert not decision.allowed
    assert decision.final_blocker is not None
    assert decision.final_blocker.code is BlockerCode.SHORT_NOT_ALLOWED_BY_BROKER
    assert decision.final_blocker.gate_name == "short_allowed_by_account"


def test_special_day_risk_blockers_are_machine_readable() -> None:
    dividend = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.BUY),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(),
            corporate_action_flag=True,
            dividend_gap_day=True,
            special_day_type="dividend_gap_day",
            special_day_trade_policy="shadow_only",
        )
    )
    corporate = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.BUY),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(),
            corporate_action_flag=True,
            special_day_type="corporate_action_day",
            special_day_trade_policy="shadow_only",
        )
    )
    short_shadow_only = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.SELL),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(allow_short=True, max_short_lots=5, max_position_lots=5),
            special_day_type="abnormal_gap_day",
            special_day_trade_policy="shadow_only",
        )
    )

    assert dividend.final_blocker is not None
    assert dividend.final_blocker.code is BlockerCode.DIVIDEND_GAP_RISK
    assert dividend.final_blocker.reason_payload["special_day_type"] == "dividend_gap_day"
    assert corporate.final_blocker is not None
    assert corporate.final_blocker.code is BlockerCode.CORPORATE_ACTION_WINDOW
    assert corporate.final_blocker.reason_payload["special_day_type"] == "corporate_action_day"
    assert short_shadow_only.final_blocker is not None
    assert short_shadow_only.final_blocker.code is BlockerCode.SPECIAL_DAY_SHADOW_ONLY
    assert short_shadow_only.final_blocker.reason_payload["special_day_trade_policy"] == (
        "shadow_only"
    )


def test_entry_is_blocked_when_position_state_is_stale() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.BUY),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(),
            portfolio=PortfolioSnapshot(
                position_state_fresh=False,
                position_reconciliation_matched=False,
                position_state_age_ms=45_000,
                position_reason_code="position_state_stale",
            ),
        )
    )

    assert not decision.allowed
    assert decision.final_blocker is not None
    assert decision.final_blocker.code is BlockerCode.POSITION_STATE_STALE
    assert decision.final_blocker.gate_name == "position_state_freshness"
    assert decision.final_blocker.reason_payload["position_reason_code"] == "position_state_stale"


def test_short_candidate_is_blocked_when_short_exposure_limit_is_reached() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(side=TradeSide.SELL),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(
                allow_short=True,
                max_short_lots=10,
                max_position_lots=10,
                max_gross_exposure_rub=Decimal("1000"),
                max_net_exposure_rub=Decimal("2000"),
            ),
            portfolio=PortfolioSnapshot(
                open_position_lots=-9,
                short_position_lots=9,
                gross_exposure_rub=Decimal("950"),
                net_exposure_rub=Decimal("-950"),
            ),
        )
    )

    assert not decision.allowed
    assert decision.final_blocker is not None
    assert decision.final_blocker.code is BlockerCode.MAX_SHORT_EXPOSURE_REACHED
    assert decision.final_blocker.gate_name == "max_gross_exposure"


def test_cost_gate_blocks_when_total_costs_exceed_expected_edge() -> None:
    decision = DefaultRiskEngine().evaluate(
        RiskAssessmentInput(
            candidate=candidate(expected_edge_bps=Decimal("12")),
            session_snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("5")),
            limits=RiskLimits(
                assumed_commission_bps_per_side=Decimal("5"),
                assumed_slippage_bps=Decimal("1"),
                min_edge_after_total_costs_bps=Decimal("0"),
            ),
            portfolio=PortfolioSnapshot(),
        )
    )

    assert not decision.allowed
    assert decision.final_blocker is not None
    assert decision.final_blocker.code is BlockerCode.TOTAL_COSTS_EXCEED_EDGE
    assert decision.final_blocker.reason_payload["total_expected_costs_bps"] == "16"


def test_production_mode_without_confirmation_raises_before_startup() -> None:
    with pytest.raises(RuntimeError, match="production mode requires"):
        LaunchModePolicy.from_env({"TRADING_RUNTIME_MODE": "production"})


class FakeBrokerGateway:
    def __init__(self) -> None:
        self.posted: list[OrderPlacementRequest] = []
        self.cancelled: list[CancelOrderRequest] = []

    async def post_order(
        self,
        request: OrderPlacementRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.posted.append(request)
        return BrokerUnaryResponse(
            method_name="PostOrder",
            data={"exchange_order_id": "exchange-1", "broker_status": "posted"},
            headers={
                "x-tracking-id": "tracking-post",
                "x-ratelimit-limit": "100",
                "x-ratelimit-remaining": "99",
            },
        )

    async def cancel_order(
        self,
        request: CancelOrderRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.cancelled.append(request)
        return BrokerUnaryResponse(
            method_name="CancelOrder",
            data={"exchange_order_id": request.exchange_order_id, "broker_status": "cancelled"},
            headers={"x-tracking-id": "tracking-cancel"},
        )

    async def reconcile_order_state(
        self,
        request: OrderStateRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        return BrokerUnaryResponse(method_name="GetOrderState", data={"broker_status": "posted"})

    async def reconcile_open_orders(
        self,
        request: OrdersRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        return BrokerUnaryResponse(method_name="GetOrders", data={"orders": []})


class RejectingBrokerGateway(FakeBrokerGateway):
    async def post_order(
        self,
        request: OrderPlacementRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        self.posted.append(request)
        return BrokerUnaryResponse(
            method_name="PostOrder",
            data={
                "exchange_order_id": "exchange-rejected",
                "broker_status": "rejected",
                "reject_reason_code": "insufficient_balance",
            },
            headers={"x-tracking-id": "tracking-reject"},
        )


class PartialFillReconciliationGateway(FakeBrokerGateway):
    async def reconcile_order_state(
        self,
        request: OrderStateRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        return BrokerUnaryResponse(
            method_name="GetOrderState",
            data={
                "exchange_order_id": "exchange-partial",
                "broker_status": "partially_filled",
                "fills": [
                    {
                        "broker_fill_id": "fill-1",
                        "exchange_order_id": "exchange-partial",
                        "side": "buy",
                        "lot_qty": 1,
                        "price": "100.10",
                        "commission": "0.30",
                        "commission_gross": "0.30",
                        "commission_net": "0.30",
                        "slippage_bp": "1.20",
                        "pnl_gross": "2.00",
                        "pnl_net": "1.70",
                        "exchange_ts": utc(2026, 6, 12, 7, 1).isoformat(),
                    }
                ],
            },
            headers={"x-tracking-id": "tracking-partial"},
        )


def test_execution_engine_posts_and_cancels_with_explicit_reason_code() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = FakeBrokerGateway()

    with Session(engine) as session:
        execution = DefaultExecutionEngine(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=OrderRepository(session),
            launch_policy=LaunchModePolicy.from_mode(
                RuntimeMode.SANDBOX,
                sandbox_orders_confirmed=True,
            ),
        )
        intent = execution.create_order_intent(
            OrderIntentRequest(
                candidate=candidate(),
                session_snapshot=snapshot(),
                account_id="account-1",
            )
        )

        post_result = asyncio.run(execution.post_order(intent))
        cancel_result = asyncio.run(
            execution.cancel_order(
                intent,
                account_id="account-1",
                cancel_reason_code=CancelReasonCode.STALE_ORDER,
                cancel_payload={"source": "deterministic_test"},
                exchange_order_id="exchange-1",
            )
        )

        assert post_result.broker_status == "posted"
        assert cancel_result.broker_status == "cancelled"
        assert intent.status == "cancelled"
        assert intent.cancel_reason_code == CancelReasonCode.STALE_ORDER.value
        assert fake_gateway.posted[0].request_order_id == intent.request_order_id
        assert fake_gateway.cancelled[0].payload["cancel_reason_code"] == "stale_order"
        state_events = list(
            session.execute(select(OrderStateEvent).order_by(OrderStateEvent.state_seq)).scalars()
        )
        assert [event.new_state for event in state_events] == ["posted", "cancelled"]
        assert state_events[0].tracking_id == "tracking-post"
        assert state_events[1].cancel_reason_code == CancelReasonCode.STALE_ORDER.value
        assert state_events[0].latency_ms is not None

    engine.dispose()


def test_shadow_execution_writes_pseudo_order_and_skips_broker_post() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = FakeBrokerGateway()

    with Session(engine) as session:
        execution = DefaultExecutionEngine(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=OrderRepository(session),
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
        )
        intent = execution.create_order_intent(
            OrderIntentRequest(
                candidate=candidate(),
                session_snapshot=snapshot(),
                account_id="account-1",
            )
        )

        result = asyncio.run(execution.post_order(intent))

        assert result.broker_status == "pseudo_posted"
        assert intent.status == "pseudo_submitted"
        assert intent.intent_payload["order_submission_mode"] == "shadow_pseudo_order"
        assert fake_gateway.posted == []

    engine.dispose()


def test_reconciliation_records_partial_fill_as_source_of_truth_execution_event() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = PartialFillReconciliationGateway()

    with Session(engine) as session:
        orders = OrderRepository(session)
        execution = DefaultExecutionEngine(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=orders,
            launch_policy=LaunchModePolicy.from_mode(RuntimeMode.HISTORICAL_REPLAY),
        )
        candidate_decision = candidate()
        intent = execution.create_order_intent(
            OrderIntentRequest(
                candidate=candidate_decision,
                session_snapshot=snapshot(),
                account_id="account-1",
            )
        )
        reconciliation = DefaultReconciliationService(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=orders,
        )

        result = asyncio.run(
            reconciliation.reconcile_order(
                account_id="account-1",
                request_order_id=intent.request_order_id,
            )
        )
        fill = session.execute(select(FillEvent)).scalar_one()
        state_event = session.execute(select(OrderStateEvent)).scalar_one()
        assert candidate_decision.candidate_id is not None
        journey = AnalyticsReadRepository(session).get_candidate_journey(
            candidate_decision.candidate_id
        )

        assert result.updated_order_count == 1
        assert result.payload["fill_count"] == 1
        assert intent.status == "partially_filled"
        assert fill.broker_fill_id == "fill-1"
        assert fill.pnl_net == Decimal("1.700000")
        assert state_event.new_state == "partially_filled"
        assert state_event.tracking_id == "tracking-partial"
        assert journey.fills[0].broker_fill_id == "fill-1"

    engine.dispose()


def test_execution_engine_records_rejected_order_reason() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = RejectingBrokerGateway()

    with Session(engine) as session:
        execution = DefaultExecutionEngine(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=OrderRepository(session),
            launch_policy=LaunchModePolicy.from_mode(
                RuntimeMode.SANDBOX,
                sandbox_orders_confirmed=True,
            ),
        )
        intent = execution.create_order_intent(
            OrderIntentRequest(
                candidate=candidate(),
                session_snapshot=snapshot(),
                account_id="account-1",
            )
        )

        result = asyncio.run(execution.post_order(intent))
        state_event = session.execute(select(OrderStateEvent)).scalar_one()

        assert result.broker_status == "rejected"
        assert intent.status == "rejected"
        assert intent.reject_reason_code == "insufficient_balance"
        assert state_event.new_state == "rejected"
        assert state_event.reject_reason_code == "insufficient_balance"
        assert state_event.tracking_id == "tracking-reject"

    engine.dispose()


def test_deterministic_blocked_candidate_persists_causal_events() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = SqlAlchemyStrategyEventStore(
            candidates=SignalCandidateRepository(session),
            blockers=BlockerEventRepository(session),
            risk_events=RiskEventRepository(session),
            state_events=StrategyStateEventRepository(session),
            candidate_stages=CandidateStageResultRepository(session),
            market_contexts=MarketContextSnapshotRepository(session),
        )
        risk = DefaultRiskEngine()
        raw_candidate = candidate()
        persisted_candidate = store.record_candidate(
            decision=raw_candidate,
            snapshot=snapshot(),
            market_state=market_state(spread_bps=Decimal("35")),
            ts_utc=utc(2026, 6, 12, 7),
        )
        decision = risk.evaluate(
            RiskAssessmentInput(
                candidate=raw_candidate,
                session_snapshot=snapshot(),
                market_state=market_state(spread_bps=Decimal("35")),
                limits=RiskLimits(max_spread_bps=Decimal("10")),
            )
        )
        blocker_rows = store.record_blockers(
            candidate=persisted_candidate,
            decision=decision,
            market_state=market_state(spread_bps=Decimal("35")),
            ts_utc=utc(2026, 6, 12, 7),
        )
        risk_rows = store.record_risk_events(
            candidate=persisted_candidate,
            decision=decision,
            ts_utc=utc(2026, 6, 12, 7),
        )
        state_row = store.record_state_transition(
            snapshot=snapshot(),
            strategy_id=raw_candidate.strategy_id,
            strategy_version=raw_candidate.strategy_version,
            previous_state=StrategyState.CANDIDATE,
            new_state=StrategyState.BLOCKED,
            event_type="strategy_state_changed",
            reason_code=decision.final_blocker.code.value if decision.final_blocker else None,
            instrument_id=raw_candidate.instrument.instrument_id,
            payload={"candidate_id": str(persisted_candidate.candidate_id)},
            ts_utc=utc(2026, 6, 12, 7),
        )

        final_blocker = next(row for row in blocker_rows if row.is_final_blocker)
        stored_blocker_count = session.scalar(select(func.count()).select_from(BlockerEvent))
        stored_stage_count = session.scalar(select(func.count()).select_from(CandidateStageResult))
        stored_context_count = session.scalar(
            select(func.count()).select_from(MarketContextSnapshot)
        )
        stored_risk_count = session.scalar(select(func.count()).select_from(RiskEvent))
        stored_state_count = session.scalar(select(func.count()).select_from(StrategyStateEvent))
        journey = AnalyticsReadRepository(session).get_candidate_journey(
            persisted_candidate.candidate_id
        )

        assert persisted_candidate.candidate_status == "blocked"
        assert final_blocker.reason_code == BlockerCode.SPREAD_TOO_WIDE.value
        assert final_blocker.blocker_code == BlockerCode.SPREAD_TOO_WIDE.value
        assert final_blocker.measured_value == Decimal("35.00000000")
        assert stored_stage_count == len(decision.blockers)
        assert stored_context_count == 2
        assert {row.snapshot_kind for row in journey.market_context} == {
            "signal_candidate_created",
            "counterfactual_seed_snapshot",
        }
        assert {row.reason_code for row in risk_rows} >= {BlockerCode.SPREAD_TOO_WIDE.value}
        assert state_row.new_state == StrategyState.BLOCKED.value
        assert stored_blocker_count == len(blocker_rows)
        assert stored_risk_count == len(risk_rows)
        assert stored_state_count == 1
        assert journey.candidate is not None
        spread_stage = next(
            row
            for row in journey.stage_results
            if row.blocker_code == BlockerCode.SPREAD_TOO_WIDE.value
        )
        assert spread_stage.stage_name == "spread_limit"
        assert journey.blockers[0].is_final_blocker

    engine.dispose()

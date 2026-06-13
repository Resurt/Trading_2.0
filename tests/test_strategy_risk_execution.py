from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo

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
from trading_common.db.base import Base
from trading_common.db.models import BlockerEvent, RiskEvent, StrategyStateEvent
from trading_common.db.repositories import (
    BlockerEventRepository,
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


def candidate() -> SignalCandidateDecision:
    return SignalCandidateDecision(
        strategy_id="baseline_config_stub",
        strategy_version=1,
        instrument=instrument(),
        timeframe=Timeframe.M5,
        action=SignalAction.ENTRY,
        side=TradeSide.BUY,
        order_type="limit",
        lot_qty=1,
        intended_price=Decimal("100.00"),
        time_in_force="day",
        expected_edge_bps=Decimal("25"),
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
            headers={"x-tracking-id": "tracking-post"},
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


def test_execution_engine_posts_and_cancels_with_explicit_reason_code() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake_gateway = FakeBrokerGateway()

    with Session(engine) as session:
        execution = DefaultExecutionEngine(
            broker_gateway=cast(BrokerGateway, fake_gateway),
            orders=OrderRepository(session),
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
        stored_risk_count = session.scalar(select(func.count()).select_from(RiskEvent))
        stored_state_count = session.scalar(select(func.count()).select_from(StrategyStateEvent))

        assert persisted_candidate.candidate_status == "blocked"
        assert final_blocker.reason_code == BlockerCode.SPREAD_TOO_WIDE.value
        assert len(risk_rows) == 1
        assert state_row.new_state == StrategyState.BLOCKED.value
        assert stored_blocker_count == len(blocker_rows)
        assert stored_risk_count == len(risk_rows)
        assert stored_state_count == 1

    engine.dispose()

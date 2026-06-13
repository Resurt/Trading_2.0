from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    CounterfactualResult,
    FillEvent,
    InstrumentRegistry,
    MarketContextSnapshot,
    MicroSession,
    OrderIntent,
    OrderStateEvent,
    SessionRun,
    SignalCandidate,
)


@dataclass(frozen=True, slots=True)
class AnalyticsSeedIds:
    candidate_id: UUID
    micro_session_id: str
    order_intent_id: UUID
    request_order_id: UUID


def analytics_context() -> dict[str, object]:
    return {
        "calendar_date": date(2026, 6, 13),
        "trading_date": date(2026, 6, 13),
        "session_type": "weekday_main",
        "session_phase": "continuous_trading",
        "micro_session_id": "2026-06-13:weekday_main:10",
        "broker_trading_status": "normal_trading",
    }


def seed_candidate_journey(session: Session) -> AnalyticsSeedIds:
    now = datetime(2026, 6, 13, 10, 5, tzinfo=UTC)
    candidate_id = uuid4()
    run_id = uuid4()
    order_intent_id = uuid4()
    request_order_id = uuid4()
    broker_order_id = uuid4()
    context = analytics_context()

    session.add(
        InstrumentRegistry(
            instrument_id="MOEX:SBER",
            ticker="SBER",
            class_code="TQBR",
            figi=None,
            instrument_uid=None,
            name="Sberbank ordinary shares",
            lot_size=10,
            min_price_increment=Decimal("0.01"),
            currency="RUB",
            is_enabled=True,
            supports_morning=True,
            supports_evening=True,
            supports_weekend=False,
            instrument_payload={"fixture": True},
        )
    )
    session.add(
        SessionRun(
            **context,
            run_id=run_id,
            strategy_id="baseline",
            strategy_version=1,
            status="open",
            started_at=now,
            ended_at=None,
            freeze_started_at=None,
            report_requested_at=None,
            close_reason_code=None,
            run_payload={"fixture": True},
        )
    )
    session.add(
        MicroSession(
            **context,
            run_id=run_id,
            strategy_id="baseline",
            strategy_version=1,
            instrument_id="MOEX:SBER",
            timeframe="5m",
            status="open",
            started_at=now,
            ended_at=None,
            freeze_started_at=None,
            rollover_reason_code=None,
            snapshot_payload={"orders": 0},
        )
    )
    session.add(
        SignalCandidate(
            **context,
            candidate_id=candidate_id,
            run_id=run_id,
            ts_utc=now,
            exchange_ts=now,
            received_ts=now,
            instrument_id="MOEX:SBER",
            strategy_id="baseline",
            strategy_version=1,
            timeframe="5m",
            side="buy",
            signal_type="baseline_placeholder",
            candidate_status="blocked",
            expected_edge_bps=Decimal("12.50"),
            expected_holding_minutes=15,
            last_price=Decimal("300.00"),
            mid_price=Decimal("300.05"),
            spread_abs=Decimal("0.10"),
            spread_bps=Decimal("3.33"),
            market_quality_score=Decimal("0.9000"),
            book_imbalance=Decimal("0.1200"),
            candle_age_ms=1000,
            data_freshness_ms=250,
            signal_fingerprint="fixture-signal",
            signal_payload={"source": "fixture"},
        )
    )
    session.add(
        MarketContextSnapshot(
            **context,
            ts_utc=now,
            exchange_ts=now,
            received_ts=now,
            candidate_id=candidate_id,
            instrument_id="MOEX:SBER",
            timeframe="5m",
            snapshot_kind="candidate_created",
            last_price=Decimal("300.00"),
            mid_price=Decimal("300.05"),
            best_bid_price=Decimal("300.00"),
            best_ask_price=Decimal("300.10"),
            spread_abs=Decimal("0.10"),
            spread_bps=Decimal("3.33"),
            bid_depth_lots=Decimal("120"),
            ask_depth_lots=Decimal("90"),
            book_imbalance=Decimal("0.1429"),
            market_quality_score=Decimal("0.9000"),
            candle_age_ms=1000,
            data_freshness_ms=250,
            feature_snapshot={"spread_bps": "3.33"},
            explanation_payload={"reason": "fixture"},
        )
    )
    session.add(
        CandidateStageResult(
            **context,
            ts_utc=now,
            exchange_ts=now,
            received_ts=now,
            candidate_id=candidate_id,
            instrument_id="MOEX:SBER",
            timeframe="5m",
            strategy_id="baseline",
            strategy_version=1,
            stage_seq=1,
            stage_name="risk_gate",
            stage_outcome="blocked",
            passed=False,
            blocker_code="spread_too_wide",
            blocker_family="market_quality",
            measured_value=Decimal("3.33"),
            threshold_value=Decimal("2.00"),
            explanation_payload={"metric": "spread_bps"},
        )
    )
    session.add(
        BlockerEvent(
            **context,
            ts_utc=now,
            exchange_ts=now,
            received_ts=now,
            candidate_id=candidate_id,
            instrument_id="MOEX:SBER",
            timeframe="5m",
            strategy_id="baseline",
            gate_name="risk_gate",
            gate_rank=1,
            stage_seq=1,
            stage_name="risk_gate",
            stage_outcome="blocked",
            passed=False,
            reason_code="spread_too_wide",
            blocker_code="spread_too_wide",
            blocker_family="market_quality",
            measured_value=Decimal("3.33"),
            threshold_value=Decimal("2.00"),
            reason_payload={"spread_bps": "3.33"},
            explanation_payload={"threshold_bps": "2.00"},
            is_final_blocker=True,
            blocker_rank=1,
            market_quality_score=Decimal("0.9000"),
            spread_bps=Decimal("3.33"),
            expected_edge_bps=Decimal("12.50"),
        )
    )
    session.add(
        OrderIntent(
            **context,
            order_intent_id=order_intent_id,
            candidate_id=candidate_id,
            instrument_id="MOEX:SBER",
            timeframe="5m",
            strategy_id="baseline",
            strategy_version=1,
            side="buy",
            order_action="skip",
            order_type="limit",
            lot_qty=1,
            intended_price=Decimal("300.10"),
            time_in_force="day",
            request_order_id=request_order_id,
            tracking_id="tracking-fixture",
            idempotency_key=f"baseline:{request_order_id}",
            execution_policy_version=1,
            status="blocked",
            cancel_reason_code=None,
            reject_reason_code="spread_too_wide",
            created_ts=now,
            submitted_ts=None,
            terminal_ts=now,
            intent_payload={"blocked": True},
        )
    )
    session.add(
        BrokerOrder(
            **context,
            broker_order_id=broker_order_id,
            order_intent_id=order_intent_id,
            candidate_id=candidate_id,
            instrument_id="MOEX:SBER",
            timeframe="5m",
            request_order_id=request_order_id,
            exchange_order_id="fixture-exchange-order",
            tracking_id="tracking-fixture",
            broker_status="cancelled",
            lifecycle_seq=2,
            posted_at=now,
            cancelled_at=now,
            rejected_at=None,
            reject_reason_code=None,
            broker_tracking_id="tracking-fixture",
            last_observed_at=now,
            broker_payload={"fixture": True},
        )
    )
    session.add(
        OrderStateEvent(
            **context,
            ts_utc=now,
            exchange_ts=now,
            received_ts=now,
            candidate_id=candidate_id,
            order_intent_id=order_intent_id,
            broker_order_id=broker_order_id,
            instrument_id="MOEX:SBER",
            timeframe="5m",
            request_order_id=request_order_id,
            exchange_order_id="fixture-exchange-order",
            tracking_id="tracking-fixture",
            state_seq=1,
            previous_state=None,
            new_state="cancelled",
            event_type="broker_order_cancelled",
            reason_code="spread_too_wide",
            cancel_reason_code="spread_too_wide",
            reject_reason_code=None,
            latency_ms=Decimal("12.50"),
            state_payload={"source": "fixture"},
        )
    )
    session.add(
        FillEvent(
            **context,
            ts_utc=now,
            exchange_ts=now,
            received_ts=now,
            candidate_id=candidate_id,
            order_intent_id=order_intent_id,
            request_order_id=request_order_id,
            exchange_order_id="fixture-exchange-order",
            tracking_id="tracking-fixture",
            broker_fill_id="fixture-fill",
            instrument_id="MOEX:SBER",
            timeframe="5m",
            side="buy",
            lot_qty=1,
            price=Decimal("300.10"),
            commission=Decimal("0.30"),
            commission_gross=Decimal("0.30"),
            commission_net=Decimal("0.30"),
            slippage_bp=Decimal("1.10"),
            pnl_gross=Decimal("4.20"),
            pnl_net=Decimal("3.90"),
            liquidity_flag="taker",
            fill_payload={"fixture": True},
        )
    )
    session.add(
        CounterfactualResult(
            **context,
            candidate_id=candidate_id,
            order_intent_id=order_intent_id,
            source_event_type="blocked",
            instrument_id="MOEX:SBER",
            timeframe="5m",
            strategy_id="baseline",
            blocker_code="spread_too_wide",
            cancel_reason_code=None,
            fee_bps_assumed=Decimal("1.00"),
            slippage_bps_assumed=Decimal("1.50"),
            slippage_bp=Decimal("1.50"),
            pnl_gross=Decimal("5.00"),
            pnl_net=Decimal("3.50"),
            mfe_5m_bps=Decimal("8.00"),
            mae_5m_bps=Decimal("-3.00"),
            mfe_10m_bps=Decimal("12.00"),
            mae_10m_bps=Decimal("-4.00"),
            mfe_15m_bps=Decimal("16.00"),
            mae_15m_bps=Decimal("-5.00"),
            would_profit_5m=True,
            would_profit_10m=True,
            would_profit_15m=True,
            result_payload={"fixture": True},
            generated_at=now,
        )
    )
    session.flush()
    return AnalyticsSeedIds(
        candidate_id=candidate_id,
        micro_session_id=str(context["micro_session_id"]),
        order_intent_id=order_intent_id,
        request_order_id=request_order_id,
    )

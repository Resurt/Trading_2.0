from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session

from trading_common.db.models import (
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    FillEvent,
    InstrumentRegistry,
    MarketCandle,
    MicroSession,
    OrderIntent,
    OrderStateEvent,
    SessionRun,
    SignalCandidate,
)

TRADING_DATE = date(2026, 6, 12)
WEEKEND_DATE = date(2026, 6, 13)
STRATEGY_ID = "baseline"
STRATEGY_VERSION = 1
INSTRUMENT_ID = "MOEX:SBER"
TIMEFRAME = "5m"
TRADE_CORE_INSTANCE_ID = "trade-core-acceptance-1"
SCENARIO_NAMES = (
    "blocked_candidate",
    "broker_reject",
    "canceled_limit_order",
    "partial_fill",
    "profitable_fill",
    "stream_reconnect_gap_recovery",
    "hourly_micro_session_rollover_without_restart",
    "weekend_session",
)


@dataclass(frozen=True, slots=True)
class LoggingAnalyticsFixture:
    trading_date: date
    weekend_date: date
    strategy_id: str
    scenario_names: tuple[str, ...]
    candidate_ids: dict[str, UUID]
    micro_session_ids: tuple[str, ...]


def seed_logging_analytics_acceptance_day(session: Session) -> LoggingAnalyticsFixture:
    """Seed deterministic logging/analytics scenarios for calibration acceptance."""

    _seed_instruments(session)
    run_10 = _session_run(session, hour=10, status="closed")
    run_11 = _session_run(session, hour=11, status="closed")
    _micro_session(session, run_id=run_10, hour=10, status="closed")
    _micro_session(session, run_id=run_11, hour=11, status="closed")
    _weekend_session(session)

    candidates = {
        "blocked_candidate": _trade_journey(
            session,
            scenario="blocked_candidate",
            minute=5,
            status="blocked",
            blocker_code="spread_too_wide",
            measured=Decimal("7.5000"),
            threshold=Decimal("5.0000"),
        ),
        "broker_reject": _trade_journey(
            session,
            scenario="broker_reject",
            minute=10,
            status="rejected",
            broker_status="rejected",
            reject_reason_code="broker_reject",
        ),
        "canceled_limit_order": _trade_journey(
            session,
            scenario="canceled_limit_order",
            minute=15,
            status="cancelled",
            broker_status="cancelled",
            cancel_reason_code="stale_order",
        ),
        "partial_fill": _trade_journey(
            session,
            scenario="partial_fill",
            minute=20,
            status="cancelled",
            broker_status="cancelled",
            cancel_reason_code="residual_cancelled",
            fill_lots=2,
            order_lots=5,
            pnl_net=Decimal("1.7000"),
        ),
        "profitable_fill": _trade_journey(
            session,
            scenario="profitable_fill",
            minute=25,
            status="filled",
            broker_status="filled",
            fill_lots=5,
            order_lots=5,
            pnl_net=Decimal("12.5000"),
        ),
    }
    _weekend_candidate(session)
    _stream_reconnect_events(session)
    _market_candles(session)
    session.flush()
    return LoggingAnalyticsFixture(
        trading_date=TRADING_DATE,
        weekend_date=WEEKEND_DATE,
        strategy_id=STRATEGY_ID,
        scenario_names=SCENARIO_NAMES,
        candidate_ids=candidates,
        micro_session_ids=(
            _micro_session_id(hour=10),
            _micro_session_id(hour=11),
        ),
    )


def _seed_instruments(session: Session) -> None:
    session.add_all(
        [
            InstrumentRegistry(
                instrument_id=INSTRUMENT_ID,
                ticker="SBER",
                class_code="TQBR",
                figi=None,
                instrument_uid="uid-sber-acceptance",
                name="Sberbank ordinary shares",
                lot_size=10,
                min_price_increment=Decimal("0.01"),
                currency="RUB",
                is_enabled=True,
                supports_morning=True,
                supports_evening=True,
                supports_weekend=False,
                instrument_payload={"fixture": "logging_analytics_acceptance"},
            )
        ]
    )


def _session_run(session: Session, *, hour: int, status: str) -> UUID:
    run_id = _uuid(f"session-run-{hour}")
    started_at = _ts(hour, 0)
    ended_at = _ts(hour + 1, 0)
    session.add(
        SessionRun(
            **_context(hour=hour),
            run_id=run_id,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            freeze_started_at=ended_at - timedelta(seconds=75),
            report_requested_at=ended_at,
            close_reason_code="hourly_rollover",
            run_payload={
                "trade_core_instance_id": TRADE_CORE_INSTANCE_ID,
                "physical_restart": False,
            },
        )
    )
    return run_id


def _micro_session(session: Session, *, run_id: UUID, hour: int, status: str) -> None:
    started_at = _ts(hour, 0)
    ended_at = _ts(hour + 1, 0)
    session.add(
        MicroSession(
            **_context(hour=hour),
            run_id=run_id,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            instrument_id=INSTRUMENT_ID,
            timeframe=TIMEFRAME,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            freeze_started_at=ended_at - timedelta(seconds=75),
            rollover_reason_code="hourly_rollover",
            snapshot_payload={
                "trade_core_instance_id": TRADE_CORE_INSTANCE_ID,
                "positions": 0,
                "open_orders": 0,
            },
        )
    )


def _weekend_session(session: Session) -> None:
    session.add(
        SessionRun(
            calendar_date=WEEKEND_DATE,
            trading_date=WEEKEND_DATE,
            session_type="weekend",
            session_phase="closed",
            micro_session_id="2026-06-13:weekend:closed",
            broker_trading_status="weekend_broker_mode",
            run_id=_uuid("weekend-session"),
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            status="closed",
            started_at=datetime(2026, 6, 13, 7, 0, tzinfo=UTC),
            ended_at=datetime(2026, 6, 13, 7, 1, tzinfo=UTC),
            freeze_started_at=None,
            report_requested_at=None,
            close_reason_code="weekend_broker_mode",
            run_payload={"trade_core_instance_id": TRADE_CORE_INSTANCE_ID},
        )
    )


def _weekend_candidate(session: Session) -> None:
    candidate_id = _uuid("weekend-candidate")
    ts_utc = datetime(2026, 6, 13, 7, 0, tzinfo=UTC)
    context = {
        "calendar_date": WEEKEND_DATE,
        "trading_date": WEEKEND_DATE,
        "session_type": "weekend",
        "session_phase": "closed",
        "micro_session_id": "2026-06-13:weekend:closed",
        "broker_trading_status": "weekend_broker_mode",
    }
    session.add(
        SignalCandidate(
            **context,
            candidate_id=candidate_id,
            run_id=_uuid("weekend-session"),
            ts_utc=ts_utc,
            exchange_ts=ts_utc,
            received_ts=ts_utc,
            instrument_id=INSTRUMENT_ID,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            timeframe=TIMEFRAME,
            side="buy",
            signal_type="entry",
            candidate_status="blocked",
            expected_edge_bps=Decimal("0"),
            expected_holding_minutes=0,
            last_price=Decimal("100"),
            mid_price=Decimal("100"),
            spread_abs=Decimal("0"),
            spread_bps=Decimal("0"),
            market_quality_score=Decimal("0"),
            book_imbalance=Decimal("0"),
            candle_age_ms=0,
            data_freshness_ms=0,
            signal_fingerprint="acceptance-weekend",
            signal_payload={"scenario": "weekend_session"},
        )
    )
    session.add(
        BlockerEvent(
            **context,
            ts_utc=ts_utc,
            exchange_ts=ts_utc,
            received_ts=ts_utc,
            candidate_id=candidate_id,
            instrument_id=INSTRUMENT_ID,
            timeframe=TIMEFRAME,
            strategy_id=STRATEGY_ID,
            gate_name="session_policy",
            gate_rank=1,
            stage_seq=1,
            stage_name="session_policy",
            stage_outcome="blocked",
            passed=False,
            reason_code="weekend_broker_mode",
            blocker_code="weekend_broker_mode",
            blocker_family="session",
            measured_value=Decimal("0"),
            threshold_value=Decimal("1"),
            reason_payload={"session_type": "weekend"},
            explanation_payload={"summary": "weekend session blocks new entries"},
            is_final_blocker=True,
            blocker_rank=1,
            market_quality_score=Decimal("0"),
            spread_bps=Decimal("0"),
            expected_edge_bps=Decimal("0"),
        )
    )


def _trade_journey(
    session: Session,
    *,
    scenario: str,
    minute: int,
    status: str,
    blocker_code: str | None = None,
    measured: Decimal | None = None,
    threshold: Decimal | None = None,
    broker_status: str | None = None,
    reject_reason_code: str | None = None,
    cancel_reason_code: str | None = None,
    fill_lots: int = 0,
    order_lots: int = 1,
    pnl_net: Decimal | None = None,
) -> UUID:
    candidate_id = _uuid(f"{scenario}-candidate")
    order_intent_id = _uuid(f"{scenario}-intent")
    broker_order_id = _uuid(f"{scenario}-broker-order")
    request_order_id = _uuid(f"{scenario}-request-order")
    exchange_order_id = f"exchange-{scenario}"
    tracking_id = f"tracking-{scenario}"
    ts_utc = _ts(10, minute)
    context = _context(hour=10)
    session.add(
        SignalCandidate(
            **context,
            candidate_id=candidate_id,
            run_id=_uuid("session-run-10"),
            ts_utc=ts_utc,
            exchange_ts=ts_utc,
            received_ts=ts_utc,
            instrument_id=INSTRUMENT_ID,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            timeframe=TIMEFRAME,
            side="buy",
            signal_type="entry",
            candidate_status=status,
            expected_edge_bps=Decimal("20"),
            expected_holding_minutes=15,
            last_price=Decimal("100"),
            mid_price=Decimal("100"),
            spread_abs=Decimal("0.10"),
            spread_bps=Decimal("5"),
            market_quality_score=Decimal("0.9000"),
            book_imbalance=Decimal("0.1000"),
            candle_age_ms=1000,
            data_freshness_ms=250,
            signal_fingerprint=f"acceptance-{scenario}",
            signal_payload={"scenario": scenario, "lot_qty": order_lots},
        )
    )
    _stage_result(
        session,
        context=context,
        candidate_id=candidate_id,
        ts_utc=ts_utc,
        scenario=scenario,
        blocker_code=blocker_code,
        measured=measured,
        threshold=threshold,
    )
    if blocker_code is not None:
        _blocker(
            session,
            context=context,
            candidate_id=candidate_id,
            ts_utc=ts_utc,
            blocker_code=blocker_code,
            measured=measured or Decimal("0"),
            threshold=threshold or Decimal("0"),
        )

    should_create_order = (
        broker_status is not None
        or cancel_reason_code is not None
        or reject_reason_code is not None
    )
    if should_create_order:
        session.add(
            OrderIntent(
                **context,
                order_intent_id=order_intent_id,
                candidate_id=candidate_id,
                instrument_id=INSTRUMENT_ID,
                timeframe=TIMEFRAME,
                strategy_id=STRATEGY_ID,
                strategy_version=STRATEGY_VERSION,
                side="buy",
                order_action="cancel" if cancel_reason_code else "place",
                order_type="limit",
                lot_qty=order_lots,
                intended_price=Decimal("100"),
                time_in_force="day",
                request_order_id=request_order_id,
                tracking_id=tracking_id,
                idempotency_key=f"acceptance:{scenario}",
                execution_policy_version=1,
                status=_intent_status(
                    broker_status=broker_status,
                    cancel_reason_code=cancel_reason_code,
                    reject_reason_code=reject_reason_code,
                ),
                cancel_reason_code=cancel_reason_code,
                reject_reason_code=reject_reason_code,
                created_ts=ts_utc,
                submitted_ts=ts_utc + timedelta(seconds=1),
                terminal_ts=ts_utc + timedelta(seconds=2),
                intent_payload={"scenario": scenario},
            )
        )
        session.add(
            BrokerOrder(
                **context,
                broker_order_id=broker_order_id,
                order_intent_id=order_intent_id,
                candidate_id=candidate_id,
                instrument_id=INSTRUMENT_ID,
                timeframe=TIMEFRAME,
                request_order_id=request_order_id,
                exchange_order_id=exchange_order_id,
                tracking_id=tracking_id,
                broker_status=broker_status or "posted",
                lifecycle_seq=2,
                latency_ms=Decimal("12.5"),
                posted_at=ts_utc + timedelta(seconds=1),
                cancelled_at=ts_utc + timedelta(seconds=2) if cancel_reason_code else None,
                rejected_at=ts_utc + timedelta(seconds=2) if reject_reason_code else None,
                reject_reason_code=reject_reason_code,
                broker_tracking_id=tracking_id,
                last_observed_at=ts_utc + timedelta(seconds=2),
                broker_payload={"scenario": scenario},
            )
        )
        session.add(
            OrderStateEvent(
                **context,
                ts_utc=ts_utc + timedelta(seconds=2),
                exchange_ts=ts_utc + timedelta(seconds=2),
                received_ts=ts_utc + timedelta(seconds=2),
                candidate_id=candidate_id,
                order_intent_id=order_intent_id,
                broker_order_id=broker_order_id,
                instrument_id=INSTRUMENT_ID,
                timeframe=TIMEFRAME,
                request_order_id=request_order_id,
                exchange_order_id=exchange_order_id,
                tracking_id=tracking_id,
                state_seq=1,
                previous_state="posted",
                new_state=broker_status or "posted",
                event_type=f"broker_order_{broker_status or 'posted'}",
                reason_code=cancel_reason_code or reject_reason_code,
                cancel_reason_code=cancel_reason_code,
                reject_reason_code=reject_reason_code,
                latency_ms=Decimal("12.5"),
                state_payload={"scenario": scenario},
            )
        )

    if fill_lots:
        session.add(
            FillEvent(
                **context,
                ts_utc=ts_utc + timedelta(seconds=3),
                exchange_ts=ts_utc + timedelta(seconds=3),
                received_ts=ts_utc + timedelta(seconds=3),
                candidate_id=candidate_id,
                order_intent_id=order_intent_id,
                request_order_id=request_order_id,
                exchange_order_id=exchange_order_id,
                tracking_id=tracking_id,
                broker_fill_id=f"fill-{scenario}",
                instrument_id=INSTRUMENT_ID,
                timeframe=TIMEFRAME,
                side="sell",
                lot_qty=fill_lots,
                price=Decimal("101"),
                commission=Decimal("0.10"),
                commission_gross=Decimal("0.10"),
                commission_net=Decimal("0.10"),
                slippage_bp=Decimal("1.0"),
                pnl_gross=(pnl_net or Decimal("1")) + Decimal("0.10"),
                pnl_net=pnl_net or Decimal("1"),
                liquidity_flag="taker",
                fill_payload={"scenario": scenario, "partial": fill_lots < order_lots},
            )
        )
    return candidate_id


def _stage_result(
    session: Session,
    *,
    context: dict[str, object],
    candidate_id: UUID,
    ts_utc: datetime,
    scenario: str,
    blocker_code: str | None,
    measured: Decimal | None,
    threshold: Decimal | None,
) -> None:
    session.add(
        CandidateStageResult(
            **context,
            ts_utc=ts_utc,
            exchange_ts=ts_utc,
            received_ts=ts_utc,
            candidate_id=candidate_id,
            instrument_id=INSTRUMENT_ID,
            timeframe=TIMEFRAME,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            stage_seq=1,
            stage_name="risk_gate" if blocker_code else "entry_setup",
            stage_outcome="blocked" if blocker_code else "passed",
            passed=blocker_code is None,
            blocker_code=blocker_code,
            blocker_family="market_quality" if blocker_code else None,
            measured_value=measured,
            threshold_value=threshold,
            explanation_payload={"scenario": scenario},
        )
    )


def _blocker(
    session: Session,
    *,
    context: dict[str, object],
    candidate_id: UUID,
    ts_utc: datetime,
    blocker_code: str,
    measured: Decimal,
    threshold: Decimal,
) -> None:
    session.add(
        BlockerEvent(
            **context,
            ts_utc=ts_utc,
            exchange_ts=ts_utc,
            received_ts=ts_utc,
            candidate_id=candidate_id,
            instrument_id=INSTRUMENT_ID,
            timeframe=TIMEFRAME,
            strategy_id=STRATEGY_ID,
            gate_name="risk_gate",
            gate_rank=1,
            stage_seq=1,
            stage_name="risk_gate",
            stage_outcome="blocked",
            passed=False,
            reason_code=blocker_code,
            blocker_code=blocker_code,
            blocker_family="market_quality",
            measured_value=measured,
            threshold_value=threshold,
            reason_payload={"measured_value": str(measured), "threshold_value": str(threshold)},
            explanation_payload={"summary": f"{blocker_code} acceptance fixture"},
            is_final_blocker=True,
            blocker_rank=1,
            market_quality_score=Decimal("0.9000"),
            spread_bps=measured,
            expected_edge_bps=Decimal("20"),
        )
    )


def _stream_reconnect_events(session: Session) -> None:
    context = _context(hour=10)
    for index, action in enumerate(("stream_reconnect", "gap_recovery_completed"), start=1):
        ts_utc = _ts(10, 40 + index)
        session.add(
            AuditEvent(
                **context,
                ts_utc=ts_utc,
                exchange_ts=ts_utc,
                received_ts=ts_utc,
                service="trade-core",
                actor="system",
                action=action,
                entity_type="market_stream",
                entity_id="candles:MOEX:SBER",
                severity="info",
                correlation_id=f"gap-recovery-{index}",
                audit_payload={
                    "stream_type": "candles",
                    "gap_recovered": action == "gap_recovery_completed",
                    "authorization": "[REDACTED]",
                },
            )
        )


def _market_candles(session: Session) -> None:
    start = _ts(10, 0)
    for index in range(24):
        open_ts = start + timedelta(minutes=5 * index)
        close_ts = open_ts + timedelta(minutes=5)
        open_price = Decimal("100") + Decimal(index) / Decimal("10")
        close_price = open_price + Decimal("0.60")
        session.add(
            MarketCandle(
                **_context(hour=10 if index < 12 else 11),
                instrument_id=INSTRUMENT_ID,
                timeframe=TIMEFRAME,
                open_ts_utc=open_ts,
                close_ts_utc=close_ts,
                exchange_open_ts=open_ts,
                exchange_close_ts=close_ts,
                open_price=open_price,
                high_price=close_price + Decimal("0.20"),
                low_price=open_price - Decimal("0.20"),
                close_price=close_price,
                volume_lots=Decimal("100"),
                is_closed=True,
                source="acceptance_fixture",
                candle_payload={"fixture": "logging_analytics_acceptance"},
            )
        )


def _context(*, hour: int) -> dict[str, object]:
    return {
        "calendar_date": TRADING_DATE,
        "trading_date": TRADING_DATE,
        "session_type": "weekday_main",
        "session_phase": "continuous_trading",
        "micro_session_id": _micro_session_id(hour=hour),
        "broker_trading_status": "normal_trading",
    }


def _micro_session_id(*, hour: int) -> str:
    return f"2026-06-12:weekday_main:{hour:02d}00"


def _ts(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 12, hour, minute, tzinfo=UTC)


def _uuid(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"trading-2.0.logging-analytics.acceptance:{name}")


def _intent_status(
    *,
    broker_status: str | None,
    cancel_reason_code: str | None,
    reject_reason_code: str | None,
) -> str:
    if cancel_reason_code:
        return "cancelled"
    if reject_reason_code or broker_status == "rejected":
        return "rejected"
    if broker_status == "filled":
        return "filled"
    return "submitted"

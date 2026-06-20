from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from report_worker.analytics import (
    CalibrationDiagnosticService,
    IntradayAnalyticsService,
    RollingPerformanceCubeService,
    StrategyConfigProposalService,
)
from trading_api.read_service import BffReadService
from trading_common.db.base import Base
from trading_common.db.models import (
    BlockerEvent,
    InstrumentRegistry,
    IntradaySessionAnalytics,
    MarketCandle,
    MarketMicrostructureSnapshot,
    SessionRun,
    SignalCandidate,
    StrategyConfig,
    StrategyConfigCandidate,
)


def utc_now() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def session_context(
    ts: datetime,
    *,
    session_type: str = "weekday_main",
    micro_session_id: str | None = None,
) -> dict[str, object]:
    return {
        "calendar_date": ts.date(),
        "trading_date": ts.date(),
        "session_type": session_type,
        "session_phase": "continuous_trading",
        "micro_session_id": micro_session_id or f"{ts.date()}:{session_type}:1000",
        "broker_trading_status": "normal_trading",
    }


def make_session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_instruments(session)
        yield session
    engine.dispose()


def seed_instruments(session: Session) -> None:
    for ticker in ("SBER", "GAZP"):
        session.add(
            InstrumentRegistry(
                instrument_id=f"MOEX:{ticker}",
                ticker=ticker,
                class_code="TQBR",
                figi=None,
                instrument_uid=f"uid-{ticker.lower()}",
                name=ticker,
                lot_size=10,
                min_price_increment=Decimal("0.01"),
                currency="RUB",
                is_enabled=True,
                supports_morning=True,
                supports_evening=True,
                supports_weekend=False,
                source="test",
                resolved_at=utc_now(),
                resolution_status="resolved",
                broker_payload={},
                instrument_payload={},
            )
        )


def add_session_run(
    session: Session,
    ts: datetime,
    *,
    status: str,
    session_type: str = "weekday_main",
) -> None:
    session.add(
        SessionRun(
            **session_context(ts, session_type=session_type),
            strategy_id="baseline",
            strategy_version=1,
            status=status,
            started_at=ts,
            ended_at=ts + timedelta(hours=1) if status == "closed" else None,
            freeze_started_at=None,
            report_requested_at=None,
            close_reason_code="hourly_rollover" if status == "closed" else None,
            run_payload={},
        )
    )


def add_microstructure(
    session: Session,
    ts: datetime,
    *,
    count: int,
    instrument_id: str = "MOEX:SBER",
    session_type: str = "weekday_main",
    spread_bps: Decimal = Decimal("5"),
    depth: Decimal = Decimal("40"),
    stale_count: int = 0,
    source: str = "data_only_shadow",
) -> None:
    for index in range(count):
        row_ts = ts + timedelta(minutes=index)
        session.add(
            MarketMicrostructureSnapshot(
                **session_context(row_ts, session_type=session_type),
                ts_utc=row_ts,
                exchange_ts=row_ts,
                received_ts=row_ts,
                instrument_id=instrument_id,
                best_bid=Decimal("100"),
                best_ask=Decimal("100.05"),
                mid_price=Decimal("100.025"),
                spread_abs=Decimal("0.05"),
                spread_bps=spread_bps,
                bid_depth_lots=depth,
                ask_depth_lots=depth,
                book_imbalance=Decimal("0.10"),
                market_quality_score=Decimal("0.90"),
                feed_freshness_age_ms=100,
                is_stale=index < stale_count,
                source=source,
                snapshot_payload={"source": source},
            )
        )


def add_candle(
    session: Session,
    ts: datetime,
    *,
    instrument_id: str = "MOEX:SBER",
    timeframe: str = "5m",
    open_price: Decimal = Decimal("100"),
    close_price: Decimal = Decimal("101"),
    volume_lots: Decimal = Decimal("100"),
    session_type: str = "weekday_main",
    source: str = "historical_replay",
) -> None:
    session.add(
        MarketCandle(
            **session_context(ts, session_type=session_type),
            instrument_id=instrument_id,
            timeframe=timeframe,
            open_ts_utc=ts,
            close_ts_utc=ts + timedelta(minutes=5),
            exchange_open_ts=ts,
            exchange_close_ts=ts + timedelta(minutes=5),
            open_price=open_price,
            high_price=max(open_price, close_price),
            low_price=min(open_price, close_price),
            close_price=close_price,
            volume_lots=volume_lots,
            is_closed=True,
            source=source,
            candle_payload={"source": source},
        )
    )


def add_candidate(
    session: Session,
    ts: datetime,
    *,
    index: int,
    instrument_id: str = "MOEX:SBER",
    timeframe: str = "5m",
    side: str = "buy",
    status: str = "approved",
    session_type: str = "weekday_main",
    source: str = "historical_replay",
) -> UUID:
    candidate_id = uuid4()
    session.add(
        SignalCandidate(
            **session_context(ts, session_type=session_type),
            candidate_id=candidate_id,
            ts_utc=ts,
            exchange_ts=None,
            received_ts=None,
            run_id=None,
            instrument_id=instrument_id,
            strategy_id="baseline",
            strategy_version=1,
            timeframe=timeframe,
            side=side,
            signal_type="entry",
            candidate_status=status,
            expected_edge_bps=Decimal("20"),
            expected_holding_minutes=5,
            last_price=Decimal("100"),
            mid_price=Decimal("100"),
            spread_abs=Decimal("0.05"),
            spread_bps=Decimal("5"),
            market_quality_score=Decimal("0.90"),
            book_imbalance=Decimal("0.10"),
            candle_age_ms=100,
            data_freshness_ms=100,
            signal_fingerprint=f"sig-{source}-{session_type}-{index}-{uuid4()}",
            signal_payload={"source": source},
        )
    )
    return candidate_id


def add_blocker(
    session: Session,
    ts: datetime,
    *,
    candidate_id: UUID,
    passed: bool,
    index: int,
    instrument_id: str = "MOEX:SBER",
    timeframe: str = "5m",
    source: str = "historical_replay",
) -> None:
    session.add(
        BlockerEvent(
            **session_context(ts),
            ts_utc=ts,
            exchange_ts=None,
            received_ts=None,
            candidate_id=candidate_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            strategy_id="baseline",
            gate_name="spread_limit",
            gate_rank=index,
            stage_seq=1,
            stage_name="spread_gate",
            stage_outcome="passed" if passed else "blocked",
            passed=passed,
            reason_code="spread_too_wide",
            blocker_code="spread_too_wide",
            blocker_family="market_quality",
            measured_value=Decimal("10"),
            threshold_value=Decimal("5"),
            reason_payload={"source": source},
            explanation_payload={},
            is_final_blocker=not passed,
            blocker_rank=1,
            market_quality_score=Decimal("0.90"),
            spread_bps=Decimal("10"),
            expected_edge_bps=Decimal("20"),
        )
    )


def test_intraday_builds_completed_session_summary() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_session_run(session, now, status="closed")
        add_microstructure(session, now, count=12)
        add_candle(session, now, open_price=Decimal("100"), close_price=Decimal("101"))
        add_candidate(session, now, index=1, status="approved")

        payload = IntradayAnalyticsService(session).build_for_session(now.date(), "weekday_main")
        summary = payload["session_summaries"][0]

        assert summary["analytics_payload"]["session_status"] == "completed"
        assert summary["market_bias"] == "long_bias"
        assert summary["candidate_count"] == 1


def test_intraday_running_session_does_not_overwrite_completed_session() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=3)
        morning = now.replace(hour=7, minute=0)
        main = now.replace(hour=10, minute=0)
        add_session_run(session, morning, status="closed", session_type="weekday_morning")
        add_session_run(session, main, status="open", session_type="weekday_main")
        add_microstructure(session, morning, count=6, session_type="weekday_morning")
        add_microstructure(session, main, count=6, session_type="weekday_main")

        service = IntradayAnalyticsService(session)
        service.build_for_session(morning.date(), "weekday_morning")
        service.build_for_session(main.date(), "weekday_main")

        rows = session.execute(select(IntradaySessionAnalytics)).scalars().all()
        statuses = {
            (row.session_type, row.analytics_payload["session_status"])
            for row in rows
            if row.instrument_id is None and row.hour_bucket is None
        }
        assert ("weekday_morning", "completed") in statuses
        assert ("weekday_main", "running") in statuses


def test_intraday_instrument_timeframe_side_aggregation_and_small_sample_warning() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_microstructure(session, now, count=5)
        add_candle(session, now, open_price=Decimal("100"), close_price=Decimal("100.10"))
        add_candidate(session, now, index=1, side="buy", status="blocked")
        add_candidate(session, now + timedelta(minutes=1), index=2, side="buy", status="blocked")

        payload = IntradayAnalyticsService(session).build_for_session(now.date(), "weekday_main")
        row = next(
            item
            for item in payload["rows"]
            if item["instrument_id"] == "MOEX:SBER"
            and item["timeframe"] == "5m"
            and item["side"] == "long"
        )

        assert row["candidate_count"] == 2
        assert row["market_bias"] == "long_bias"
        assert "small_sample_is_early_evidence_not_final_truth" in row["warnings"]
        assert "disabled" not in row["analytics_payload"]


def test_intraday_market_bias_values() -> None:
    cases = [
        (Decimal("100"), Decimal("101"), "long_bias"),
        (Decimal("100"), Decimal("99"), "short_bias"),
        (Decimal("100"), Decimal("100.10"), "sideways"),
    ]
    for open_price, close_price, expected in cases:
        for session in make_session():
            now = utc_now() - timedelta(hours=2)
            add_candle(session, now, open_price=open_price, close_price=close_price)
            payload = IntradayAnalyticsService(session).build_for_session(
                now.date(),
                "weekday_main",
            )
            assert payload["market_bias"] == expected

    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_candle(session, now, instrument_id="MOEX:SBER", close_price=Decimal("101"))
        add_candle(
            session,
            now + timedelta(minutes=10),
            instrument_id="MOEX:GAZP",
            close_price=Decimal("99"),
        )
        mixed = IntradayAnalyticsService(session).build_for_session(now.date(), "weekday_main")
        assert mixed["market_bias"] == "mixed"

    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        unknown = IntradayAnalyticsService(session).build_for_session(now.date(), "weekday_main")
        assert unknown["market_bias"] == "unknown"


def test_rolling_cube_aggregates_and_keeps_low_sample_active_as_warning() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_microstructure(session, now, count=8)
        candidate = add_candidate(session, now, index=1, status="blocked")
        add_blocker(session, now, candidate_id=candidate, passed=False, index=1)
        add_candidate(session, now + timedelta(minutes=1), index=2, status="approved")

        rows = RollingPerformanceCubeService(session).build_rolling_cube(
            window_names=("7d",),
            universe=("SBER",),
            mode="historical",
        )
        row = next(item for item in rows if item["timeframe"] == "5m" and item["side"] == "long")

        assert row["candidate_count"] == 2
        assert row["blocked_count"] == 1
        assert row["sample_warning"] is not None
        assert row["contour_status"] == "research_only"


def test_rolling_cube_data_shadow_microstructure_contributes_spread_depth_metrics() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_microstructure(session, now, count=12, spread_bps=Decimal("7"), depth=Decimal("55"))

        rows = RollingPerformanceCubeService(session).build_rolling_cube(
            window_names=("7d",),
            universe=("SBER",),
            mode="data_shadow",
        )
        row = rows[0]

        assert row["mode"] == "data_shadow"
        assert row["timeframe"] == "all"
        assert row["avg_spread_bps"] == "7.0000"
        assert row["avg_depth"] == "55.0000"


def test_diagnostics_no_trade_low_activity_is_market_dead() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_microstructure(session, now, count=3)

        payload = CalibrationDiagnosticService(session).diagnose_no_trade_period(("SBER",), 7)

        assert payload["diagnosis"] == "market_dead"


def test_diagnostics_normal_activity_with_blocker_drift_is_robot_too_strict() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_microstructure(session, now, count=25)
        for index in range(10):
            candidate = add_candidate(
                session,
                now + timedelta(minutes=index),
                index=index,
                status="blocked" if index >= 5 else "approved",
            )
            add_blocker(
                session,
                now + timedelta(minutes=index),
                candidate_id=candidate,
                passed=index < 5,
                index=index + 1,
            )

        payload = CalibrationDiagnosticService(session).diagnose_robot_health(("SBER",), 7)

        assert payload["diagnosis"] == "robot_too_strict"
        assert "blocker_drift_material" in payload["blocking_issues"]


def test_diagnostics_missing_or_stale_data_paths() -> None:
    for session in make_session():
        payload = CalibrationDiagnosticService(session).diagnose_no_trade_period(("SBER",), 7)
        assert payload["diagnosis"] == "not_enough_data"

    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_microstructure(session, now, count=20, stale_count=8)
        payload = CalibrationDiagnosticService(session).diagnose_no_trade_period(("SBER",), 7)
        assert payload["diagnosis"] == "data_quality_problem"


def test_diagnostics_normal_market_no_issue() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        add_microstructure(session, now, count=25)
        for index in range(6):
            add_candidate(session, now + timedelta(minutes=index), index=index)

        payload = CalibrationDiagnosticService(session).diagnose_robot_health(("SBER",), 7)

        assert payload["diagnosis"] == "normal_no_action_needed"


def test_candidate_config_created_as_draft_and_approval_only_changes_status() -> None:
    for session in make_session():
        now = utc_now() - timedelta(hours=2)
        session.add(
            StrategyConfig(
                strategy_id="baseline",
                version=1,
                session_template="weekday_main",
                is_active=True,
                valid_from=now,
                valid_to=None,
                config_payload={"enabled": True},
                risk_limits={"max_position_lots": 10},
            )
        )
        proposal = StrategyConfigProposalService(session).create_strategy_config_candidate(
            base_strategy_id="baseline",
            proposal_payload={"threshold_delta": {"spread_bps": "-1"}},
        )
        candidate_id = UUID(str(proposal["candidate_config_id"]))
        row = session.get(StrategyConfigCandidate, candidate_id)

        assert row is not None
        assert row.status == "draft"
        assert row.proposal_payload["apply_automatically"] is False
        assert row.proposal_payload["runtime_config_changed"] is False
        assert session.execute(select(StrategyConfig)).scalars().one().version == 1

        approved = BffReadService(session).approve_config_candidate_for_shadow(
            candidate_id,
            approved_by="admin",
        )

        assert approved.status == "approved_for_shadow"
        assert approved.validation_payload["runtime_config_changed"] is False
        assert session.execute(select(StrategyConfig)).scalars().one().version == 1

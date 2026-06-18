from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from report_worker.analytics.calibration import CalibrationReportConfig, CalibrationReportService
from report_worker.analytics.historical_counterfactual import (
    HistoricalCounterfactualConfig,
    HistoricalCounterfactualService,
)
from report_worker.analytics.historical_reports import (
    HistoricalReportRebuildConfig,
    HistoricalReportRebuildService,
)
from trade_core.corporate_actions import (
    CorporateActionEvent,
    CorporateActionImportConfig,
    CorporateActionService,
    MarketSpecialDayClassifier,
)
from trade_core.market_data.events import Timeframe
from trade_core.market_data.historical_backfill import classify_historical_exchange_ts
from trade_core.market_data.quality import HistoricalDataQualityConfig, HistoricalDataQualityService
from trade_core.replay import HistoricalDbReplayConfig, HistoricalDbReplayService
from trading_common.db.base import Base
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CalibrationReport,
    CandidateStageResult,
    CounterfactualResult,
    HistoricalDataQualityReport,
    InstrumentRegistry,
    MarketCandle,
    MarketSpecialDay,
    OrderIntent,
    OrderStateEvent,
    RiskEvent,
    SignalCandidate,
    StrategyConfig,
)
from trading_common.db.models import (
    CorporateActionEvent as CorporateActionEventRow,
)

MSK = ZoneInfo("Europe/Moscow")


def test_historical_quality_catches_missing_duplicate_invalid_and_session_split() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_instrument(session)
        _add_candle(
            session,
            exchange_open=datetime(2026, 6, 18, 7, 0, tzinfo=MSK),
            timeframe="1m",
            open_price=Decimal("100"),
            close_price=Decimal("100.01"),
        )
        _add_candle(
            session,
            exchange_open=datetime(2026, 6, 18, 10, 0, tzinfo=MSK),
            timeframe="1m",
            open_price=Decimal("100"),
            high_price=Decimal("99"),
            low_price=Decimal("101"),
            close_price=Decimal("100"),
        )
        duplicate_open = datetime(2026, 6, 18, 10, 1, tzinfo=MSK)
        _add_candle(session, exchange_open=duplicate_open, timeframe="1m")
        _add_candle(
            session,
            exchange_open=duplicate_open,
            timeframe="1m",
            trading_date=date(2026, 6, 19),
        )
        _add_candle(
            session,
            exchange_open=datetime(2026, 6, 20, 10, 0, tzinfo=MSK),
            timeframe="1m",
        )
        report = HistoricalDataQualityService(session).build_report(
            HistoricalDataQualityConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 20),
                instruments=("SBER",),
                timeframes=(Timeframe.M1,),
            )
        )
        assert report.missing_intervals > 0
        assert report.duplicate_count == 1
        assert report.invalid_ohlc_count >= 1
        assert report.session_type_distribution["weekday_morning"] == 1
        assert report.session_type_distribution["weekday_main"] >= 3
        assert report.session_type_distribution["weekend"] == 1
        assert session.execute(select(HistoricalDataQualityReport)).scalars().first() is not None
    engine.dispose()


def test_corporate_action_import_is_idempotent_and_classifies_dividend_gap(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_instrument(session)
        csv_path = tmp_path / "dividends.csv"
        csv_path.write_text(
            "ticker,action_type,ex_date,amount_per_share,currency\n"
            "SBER,dividend,2026-06-18,34.84,RUB\n",
            encoding="utf-8",
        )
        service = CorporateActionService(session)
        first = service.import_csv(
            csv_path,
            config=CorporateActionImportConfig(source="manual"),
        )
        second = service.import_csv(
            csv_path,
            config=CorporateActionImportConfig(source="manual"),
        )
        result = MarketSpecialDayClassifier(session).classify(
            from_date=date(2026, 6, 18),
            to_date=date(2026, 6, 18),
            instruments=("SBER",),
        )

        assert len(first) == 1
        assert len(second) == 1
        assert session.execute(select(CorporateActionEventRow)).scalars().all() == list(first)
        special_day = session.execute(select(MarketSpecialDay)).scalars().one()
        assert special_day.special_day_type == "dividend_gap_day"
        assert special_day.exclude_from_primary_calibration is True
        assert special_day.trade_policy == "shadow_only"
        assert result.dividend_gap_days == 1
    engine.dispose()


def test_quality_report_requires_special_day_classification_when_final() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_instrument(session)
        _add_candle(
            session,
            exchange_open=datetime(2026, 6, 18, 10, 0, tzinfo=MSK),
            timeframe="1m",
        )
        config = HistoricalDataQualityConfig(
            from_date=date(2026, 6, 18),
            to_date=date(2026, 6, 18),
            instruments=("SBER",),
            timeframes=(Timeframe.M1,),
            require_special_day_classification=True,
        )
        with pytest.raises(SystemExit) as raised:
            HistoricalDataQualityService(session).assert_passes(config)
        assert raised.value.code == 5

        MarketSpecialDayClassifier(session).classify(
            from_date=date(2026, 6, 18),
            to_date=date(2026, 6, 18),
            instruments=("SBER",),
        )
        report = HistoricalDataQualityService(session).assert_passes(config)
        assert report.corporate_action_classification_status == "completed"
        assert report.quality_warnings == ()
    engine.dispose()


def test_historical_session_classification_micro_session_ids() -> None:
    morning = classify_historical_exchange_ts(datetime(2026, 6, 18, 7, 30, tzinfo=MSK))
    main = classify_historical_exchange_ts(datetime(2026, 6, 18, 10, 30, tzinfo=MSK))
    evening = classify_historical_exchange_ts(datetime(2026, 6, 18, 19, 30, tzinfo=MSK))
    weekend = classify_historical_exchange_ts(datetime(2026, 6, 20, 10, 30, tzinfo=MSK))
    closed = classify_historical_exchange_ts(datetime(2026, 6, 18, 6, 30, tzinfo=MSK))

    assert morning.micro_session_id == "historical:2026-06-18:weekday_morning:07"
    assert main.micro_session_id == "historical:2026-06-18:weekday_main:10"
    assert evening.micro_session_id == "historical:2026-06-18:weekday_evening:19"
    assert weekend.micro_session_id == "historical:2026-06-20:weekend:10"
    assert closed.session_phase.value == "closed"
    assert main.source == "fallback_moex_session_windows"


def test_historical_replay_creates_full_path_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_replay_candles(session)
        config = HistoricalDbReplayConfig(
            from_date=date(2026, 6, 18),
            to_date=date(2026, 6, 18),
            instruments=("SBER",),
            timeframes=(Timeframe.M5,),
            strategy_id="baseline",
        )
        first = asyncio.run(HistoricalDbReplayService(session).run(config))
        second = asyncio.run(HistoricalDbReplayService(session).run(config))
        reset = asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=config.from_date,
                    to_date=config.to_date,
                    instruments=config.instruments,
                    timeframes=config.timeframes,
                    strategy_id=config.strategy_id,
                    reset_derived_events=True,
                )
            )
        )

        assert first.candidates_created >= 2
        assert first.order_intents_created >= 1
        assert first.pseudo_orders_created >= 1
        assert first.blockers_created >= 1
        assert first.risk_events_created >= 1
        assert second.candidates_created == 0
        assert second.skipped_existing_events >= first.candidates_created
        assert reset.candidates_created == first.candidates_created
        assert reset.deterministic_fingerprint == first.deterministic_fingerprint
        assert session.execute(select(SignalCandidate)).scalars().first() is not None
        assert session.execute(select(CandidateStageResult)).scalars().first() is not None
        assert session.execute(select(BlockerEvent)).scalars().first() is not None
        assert session.execute(select(RiskEvent)).scalars().first() is not None
        assert session.execute(select(OrderIntent)).scalars().first() is not None
        broker_order = session.execute(select(BrokerOrder)).scalars().first()
        assert broker_order is not None
        assert broker_order.broker_status == "pseudo_posted"
        assert session.execute(select(OrderStateEvent)).scalars().first() is not None
    engine.dispose()


def test_replay_excludes_and_flags_special_days() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_replay_candles(session)
        _seed_dividend_event(session)
        MarketSpecialDayClassifier(session).classify(
            from_date=date(2026, 6, 18),
            to_date=date(2026, 6, 18),
            instruments=("SBER",),
        )

        excluded = asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=date(2026, 6, 18),
                    to_date=date(2026, 6, 18),
                    instruments=("SBER",),
                    timeframes=(Timeframe.M5,),
                    strategy_id="baseline",
                    require_special_day_classification=True,
                )
            )
        )
        flagged = asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=date(2026, 6, 18),
                    to_date=date(2026, 6, 18),
                    instruments=("SBER",),
                    timeframes=(Timeframe.M5,),
                    strategy_id="baseline",
                    include_special_days=True,
                    special_day_policy="include_with_flags",
                    require_special_day_classification=True,
                    reset_derived_events=True,
                )
            )
        )

        assert excluded.candidates_created == 0
        assert excluded.bars_skipped_special_day >= 1
        assert excluded.skipped_dividend_gap_days >= 1
        assert flagged.candidates_created >= 1
        flagged_candidate = session.execute(select(SignalCandidate)).scalars().first()
        assert flagged_candidate is not None
        flagged_payload = _candidate_condition_payload(flagged_candidate)
        assert flagged_payload["dividend_gap_day"] is True
        assert flagged_payload["special_day_policy"] == "include_with_flags"
        shadow_only = asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=date(2026, 6, 18),
                    to_date=date(2026, 6, 18),
                    instruments=("SBER",),
                    timeframes=(Timeframe.M5,),
                    strategy_id="baseline",
                    include_special_days=True,
                    special_day_policy="shadow_only",
                    require_special_day_classification=True,
                    reset_derived_events=True,
                )
            )
        )
        assert shadow_only.candidates_created >= 1
        shadow_candidate = session.execute(select(SignalCandidate)).scalars().first()
        assert shadow_candidate is not None
        shadow_payload = _candidate_condition_payload(shadow_candidate)
        assert shadow_payload["eligible_for_live_calibration"] is False
    engine.dispose()


def test_replay_requires_db_strategy_config_unless_fallback_is_explicit() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_instrument(session)
        _add_candle(
            session,
            exchange_open=datetime(2026, 6, 18, 10, 0, tzinfo=MSK),
            timeframe="5m",
            open_price=Decimal("100"),
            close_price=Decimal("100.30"),
        )
        config = HistoricalDbReplayConfig(
            from_date=date(2026, 6, 18),
            to_date=date(2026, 6, 18),
            instruments=("SBER",),
            timeframes=(Timeframe.M5,),
            strategy_id="baseline",
        )

        with pytest.raises(RuntimeError, match="requires active strategy_config"):
            asyncio.run(HistoricalDbReplayService(session).run(config))

        fallback = asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=config.from_date,
                    to_date=config.to_date,
                    instruments=config.instruments,
                    timeframes=config.timeframes,
                    strategy_id=config.strategy_id,
                    allow_default_strategy_config=True,
                    dry_run=True,
                )
            )
        )
        assert fallback.strategy_config_source == "fallback_conservative_default"
        assert fallback.allow_default_strategy_config is True
    engine.dispose()


def test_calibration_primary_scope_excludes_special_days_and_warns_when_missing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_replay_candles(session)
        _seed_dividend_event(session)
        MarketSpecialDayClassifier(session).classify(
            from_date=date(2026, 6, 18),
            to_date=date(2026, 6, 18),
            instruments=("SBER",),
        )
        asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=date(2026, 6, 18),
                    to_date=date(2026, 6, 18),
                    instruments=("SBER",),
                    timeframes=(Timeframe.M5,),
                    strategy_id="baseline",
                    include_special_days=True,
                    special_day_policy="include_with_flags",
                )
            )
        )
        primary = CalibrationReportService(session).build(
            CalibrationReportConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 18),
                strategy_id="baseline",
                instruments=("SBER",),
                timeframes=("5m",),
                group_by=("session_type", "instrument_id", "timeframe", "blocker_code"),
                require_special_day_classification=True,
            )
        )
        special_only = CalibrationReportService(session).build(
            CalibrationReportConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 18),
                strategy_id="baseline",
                instruments=("SBER",),
                timeframes=("5m",),
                group_by=("session_type", "instrument_id", "timeframe", "blocker_code"),
                calibration_scope="special_days_only",
            )
        )

        assert primary.report_payload["calibration_clean"] is True
        assert primary.report_payload["candidate_count"] == 0
        assert primary.report_payload["special_days_count"] >= 1
        assert primary.report_payload["calibration_data_mode"] == "historical_candles_only"
        assert primary.report_payload["requires_shadow_live_calibration"] is True
        recommendations = primary.report_payload["recommendations"]
        assert isinstance(recommendations, dict)
        assert "safe_from_historical_candles" in recommendations
        assert "requires_shadow_confirmation" in recommendations
        assert special_only.report_payload["calibration_clean"] is False
        assert special_only.report_payload["candidate_count"] >= 1
        assert "non_primary_calibration_scope" in special_only.report_payload[
            "calibration_warnings"
        ]
    engine.dispose()


def test_calibration_is_not_clean_when_special_classification_is_missing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_replay_candles(session)
        asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=date(2026, 6, 18),
                    to_date=date(2026, 6, 18),
                    instruments=("SBER",),
                    timeframes=(Timeframe.M5,),
                    strategy_id="baseline",
                )
            )
        )
        calibration = CalibrationReportService(session).build(
            CalibrationReportConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 18),
                strategy_id="baseline",
                instruments=("SBER",),
                timeframes=("5m",),
                group_by=("session_type", "instrument_id", "timeframe", "blocker_code"),
            )
        )

        assert calibration.report_payload["calibration_clean"] is False
        assert "corporate_action_classification_missing" in calibration.report_payload[
            "calibration_warnings"
        ]
        with pytest.raises(RuntimeError, match="special day classification"):
            CalibrationReportService(session).build(
                CalibrationReportConfig(
                    from_date=date(2026, 6, 18),
                    to_date=date(2026, 6, 18),
                    strategy_id="baseline",
                    instruments=("SBER",),
                    timeframes=("5m",),
                    group_by=("session_type",),
                    require_special_day_classification=True,
                )
            )
    engine.dispose()


def test_historical_counterfactual_reports_and_calibration() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_replay_candles(session)
        asyncio.run(
            HistoricalDbReplayService(session).run(
                HistoricalDbReplayConfig(
                    from_date=date(2026, 6, 18),
                    to_date=date(2026, 6, 18),
                    instruments=("SBER",),
                    timeframes=(Timeframe.M5,),
                    strategy_id="baseline",
                )
            )
        )
        counterfactual = HistoricalCounterfactualService(session).rebuild(
            HistoricalCounterfactualConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 18),
                strategy_id="baseline",
                instruments=("SBER",),
                timeframes=("5m",),
                force_rebuild=True,
            )
        )
        repeat = HistoricalCounterfactualService(session).rebuild(
            HistoricalCounterfactualConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 18),
                strategy_id="baseline",
                instruments=("SBER",),
                timeframes=("5m",),
            )
        )
        reports = HistoricalReportRebuildService(session).rebuild(
            HistoricalReportRebuildConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 18),
                strategy_id="baseline",
                include_counterfactual=False,
            )
        )
        calibration = CalibrationReportService(session).build(
            CalibrationReportConfig(
                from_date=date(2026, 6, 18),
                to_date=date(2026, 6, 18),
                strategy_id="baseline",
                instruments=("SBER",),
                timeframes=("5m",),
                group_by=("session_type", "instrument_id", "timeframe", "blocker_code"),
            )
        )

        assert counterfactual.results_created >= 1
        assert repeat.results_existing >= counterfactual.results_created
        result = session.execute(select(CounterfactualResult)).scalars().first()
        assert result is not None
        assert result.mfe_5m_bps is not None
        assert result.mfe_10m_bps is not None
        assert result.mfe_15m_bps is not None
        assumptions = result.result_payload["assumptions"]
        assert isinstance(assumptions, dict)
        assert assumptions["fee_bps"] == "10"
        assert reports.hourly_reports_built >= 1
        assert reports.daily_reports_built == 1
        assert calibration.report_payload["blocker_ranking"]
        assert "net_simulated_pnl" in calibration.report_payload
        assert "recommended_threshold_changes" in calibration.report_payload
        assert session.execute(select(CalibrationReport)).scalars().first() is not None
    engine.dispose()


def _seed_instrument(session: Session) -> None:
    session.add(
        InstrumentRegistry(
            instrument_id="MOEX:SBER",
            ticker="SBER",
            class_code="TQBR",
            figi=None,
            instrument_uid="uid-sber",
            name="SBER",
            lot_size=10,
            min_price_increment=Decimal("0.01"),
            currency="RUB",
            is_enabled=True,
            supports_morning=True,
            supports_evening=True,
            supports_weekend=True,
            instrument_payload={},
        )
    )
    session.flush()


def _candidate_condition_payload(candidate: SignalCandidate) -> dict[str, object]:
    payload = candidate.signal_payload.get("condition_payload", {})
    assert isinstance(payload, dict)
    return payload


def _seed_replay_candles(session: Session) -> None:
    _seed_instrument(session)
    _seed_strategy_config(session)
    bars = (
        (datetime(2026, 6, 18, 10, 0, tzinfo=MSK), Decimal("100"), Decimal("100.30")),
        (datetime(2026, 6, 18, 10, 5, tzinfo=MSK), Decimal("100.30"), Decimal("99.90")),
        (datetime(2026, 6, 18, 10, 10, tzinfo=MSK), Decimal("99.90"), Decimal("99.91")),
        (datetime(2026, 6, 18, 10, 15, tzinfo=MSK), Decimal("99.91"), Decimal("99.92")),
        (datetime(2026, 6, 18, 10, 20, tzinfo=MSK), Decimal("99.92"), Decimal("99.93")),
    )
    for exchange_open, open_price, close_price in bars:
        _add_candle(
            session,
            exchange_open=exchange_open,
            timeframe="5m",
            open_price=open_price,
            close_price=close_price,
            high_price=max(open_price, close_price),
            low_price=min(open_price, close_price),
        )


def _seed_strategy_config(session: Session) -> None:
    session.add(
        StrategyConfig(
            strategy_id="baseline",
            version=1,
            session_template="weekday_main",
            is_active=True,
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            config_payload={
                "enabled": True,
                "allow_long": True,
                "allow_short": False,
                "assumed_commission_bps_per_side": "5",
                "assumed_slippage_bps": "0",
            },
            risk_limits={
                "max_spread_bps": "20",
                "min_market_quality_score": "0.70",
                "max_data_age_ms": 5000,
            },
        )
    )
    session.flush()


def _seed_dividend_event(session: Session) -> None:
    CorporateActionService(session).upsert_event(
        CorporateActionEvent(
            instrument_id="MOEX:SBER",
            ticker="SBER",
            action_type="dividend",
            ex_date=date(2026, 6, 18),
            amount_per_share=Decimal("34.84"),
            currency="RUB",
            source="synthetic_test",
            confidence="confirmed",
            action_payload={"source": "test"},
        )
    )
    session.flush()


def _add_candle(
    session: Session,
    *,
    exchange_open: datetime,
    timeframe: str,
    open_price: Decimal = Decimal("100"),
    close_price: Decimal = Decimal("100.01"),
    high_price: Decimal | None = None,
    low_price: Decimal | None = None,
    trading_date: date | None = None,
) -> MarketCandle:
    minutes = int(timeframe.removesuffix("m"))
    exchange_close = exchange_open + timedelta(minutes=minutes)
    classification = classify_historical_exchange_ts(exchange_open)
    row = MarketCandle(
        calendar_date=classification.calendar_date,
        trading_date=trading_date or classification.trading_date,
        session_type=classification.session_type.value,
        session_phase=classification.session_phase.value,
        micro_session_id=classification.micro_session_id,
        broker_trading_status="historical_backfill",
        instrument_id="MOEX:SBER",
        timeframe=timeframe,
        open_ts_utc=exchange_open.astimezone(UTC),
        close_ts_utc=exchange_close.astimezone(UTC),
        exchange_open_ts=exchange_open,
        exchange_close_ts=exchange_close,
        open_price=open_price,
        high_price=high_price if high_price is not None else max(open_price, close_price),
        low_price=low_price if low_price is not None else min(open_price, close_price),
        close_price=close_price,
        volume_lots=Decimal("10"),
        is_closed=True,
        source="historical_db_derived_bar" if timeframe != "1m" else "tbank_historical_backfill",
        candle_payload={
            "source": "historical_fixture",
            "session_classification_source": classification.source,
            "session_classification_warning": classification.warning,
        },
    )
    session.add(row)
    session.flush()
    return row

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    OrderIntent,
    OrderStateEvent,
    RiskEvent,
    SignalCandidate,
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


def _seed_replay_candles(session: Session) -> None:
    _seed_instrument(session)
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

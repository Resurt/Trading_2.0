from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from tests.fixtures.analytics_seed import seed_candidate_journey
from trading_common.db.base import Base
from trading_common.db.models import (
    BrokerOrder,
    InstrumentRegistry,
    MarketCandle,
    MarketMicrostructureSnapshot,
    OrderBookSummary,
    OrderIntent,
    ReportJobOutbox,
    RobotCommand,
    SessionRun,
    StrategyConfig,
)
from trading_common.db.repositories import (
    AnalyticsReadRepository,
    InstrumentRepository,
    MarketDataRepository,
    MicroSessionRepository,
    OrderRepository,
    ReportJobRepository,
    RobotCommandRepository,
    SessionRunRepository,
    StrategyConfigRepository,
)

ROOT = Path(__file__).resolve().parents[1]
PARTITIONED_TABLES = {
    "fill_event",
    "audit_event",
    "blocker_event",
    "strategy_state_event",
    "counterfactual_result",
    "market_candle",
    "market_status_snapshot",
    "order_book_summary",
    "market_context_snapshot",
    "candidate_stage_result",
    "order_state_event",
}


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "packages" / "common" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def context_values() -> dict[str, object]:
    return {
        "calendar_date": date(2026, 6, 13),
        "trading_date": date(2026, 6, 13),
        "session_type": "weekday_main",
        "session_phase": "continuous_trading",
        "micro_session_id": "2026-06-13:weekday_main:10",
        "broker_trading_status": "normal_trading",
    }


def test_metadata_contains_required_tables_and_partitioning() -> None:
    required_tables = {
        "instrument_registry",
        "strategy_config",
        "session_run",
        "micro_session",
        "market_context_snapshot",
        "signal_candidate",
        "candidate_stage_result",
        "blocker_event",
        "order_intent",
        "broker_order",
        "order_state_event",
        "fill_event",
        "risk_event",
        "position_snapshot",
        "strategy_state_event",
        "hourly_report",
        "daily_report",
        "report_job_outbox",
        "robot_command",
        "counterfactual_result",
        "audit_event",
        "market_candle",
        "market_status_snapshot",
        "order_book_summary",
        "market_microstructure_snapshot",
    }

    assert required_tables <= set(Base.metadata.tables)

    for table_name in PARTITIONED_TABLES:
        table = Base.metadata.tables[table_name]
        assert table.c.trading_date.primary_key
        assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (trading_date)"


def test_alembic_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-smoke.db"
    database_url = f"sqlite:///{database_path}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        tickers = connection.execute(
            text("select ticker from instrument_registry order by ticker")
        ).scalars()
        assert "instrument_registry" in table_names
        assert "order_intent" in table_names
        assert "counterfactual_result" in table_names
        assert "market_candle" in table_names
        assert "market_microstructure_snapshot" in table_names
        assert "robot_command" in table_names
        assert list(tickers) == ["GAZP", "LKOH", "SBER"]

    command.downgrade(config, "base")

    with engine.connect() as connection:
        assert "instrument_registry" not in set(inspect(connection).get_table_names())
    engine.dispose()


def test_repository_crud_and_order_idempotency() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)

    with Session(engine) as session:
        instruments = InstrumentRepository(session)
        strategy_configs = StrategyConfigRepository(session)
        session_runs = SessionRunRepository(session)
        orders = OrderRepository(session)
        report_jobs = ReportJobRepository(session)
        robot_commands = RobotCommandRepository(session)
        market_data = MarketDataRepository(session)

        instrument = instruments.upsert(
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
                instrument_payload={"test": True},
            )
        )
        assert instrument.instrument_id == "MOEX:SBER"
        assert instruments.get_by_ticker("SBER") is instrument

        strategy_config = strategy_configs.create_version(
            StrategyConfig(
                strategy_id="baseline",
                version=1,
                session_template="weekday_main",
                is_active=True,
                valid_from=now,
                valid_to=None,
                config_payload={"enabled": False},
                risk_limits={"max_position_lots": 0},
            )
        )
        assert strategy_config.version == 1
        assert strategy_configs.get_active("baseline", "weekday_main") is strategy_config

        run = session_runs.create(
            SessionRun(
                **context_values(),
                strategy_id="baseline",
                strategy_version=1,
                status="open",
                started_at=now,
                ended_at=None,
                freeze_started_at=None,
                report_requested_at=None,
                close_reason_code=None,
                run_payload={},
            )
        )
        session_runs.close(run.run_id, ended_at=now, close_reason_code="hourly_rollover")
        assert run.status == "closed"

        report_job = report_jobs.create_hourly_job_idempotent(
            micro_session_id=run.micro_session_id,
            strategy_id="baseline",
            trading_date=run.trading_date,
            requested_at=now,
            job_payload={"source": "test"},
        )
        duplicate_report_job = report_jobs.create_hourly_job_idempotent(
            micro_session_id=run.micro_session_id,
            strategy_id="baseline",
            trading_date=run.trading_date,
            requested_at=now,
            job_payload={"source": "test"},
        )
        assert duplicate_report_job is report_job
        assert report_job.status == "pending"
        assert session.get(ReportJobOutbox, report_job.report_job_id) is report_job

        command_row = robot_commands.create(
            command_type="stop",
            requested_by="operator",
            requested_role="operator",
            requested_at=now,
            payload={"reason": "test"},
        )
        robot_commands.mark_accepted(command_row, accepted_at=now)
        robot_commands.mark_applied(
            command_row,
            applied_at=now,
            reason_code="runtime_safe_stopped",
        )
        assert command_row.status == "applied"
        assert session.get(RobotCommand, command_row.command_id) is command_row

        request_order_id = uuid4()
        intent = orders.create_intent_idempotent(
            OrderIntent(
                **context_values(),
                candidate_id=None,
                instrument_id="MOEX:SBER",
                strategy_id="baseline",
                side="buy",
                order_action="place",
                order_type="limit",
                lot_qty=1,
                intended_price=Decimal("300.10"),
                time_in_force="day",
                request_order_id=request_order_id,
                idempotency_key=f"baseline:{request_order_id}",
                execution_policy_version=1,
                status="created",
                cancel_reason_code=None,
                reject_reason_code=None,
                created_ts=now,
                submitted_ts=None,
                terminal_ts=None,
                intent_payload={},
            )
        )
        duplicate = orders.create_intent_idempotent(
            OrderIntent(
                **context_values(),
                candidate_id=None,
                instrument_id="MOEX:SBER",
                strategy_id="baseline",
                side="buy",
                order_action="place",
                order_type="limit",
                lot_qty=1,
                intended_price=Decimal("300.10"),
                time_in_force="day",
                request_order_id=request_order_id,
                idempotency_key=f"baseline:{request_order_id}",
                execution_policy_version=1,
                status="created",
                cancel_reason_code=None,
                reject_reason_code=None,
                created_ts=now,
                submitted_ts=None,
                terminal_ts=None,
                intent_payload={},
            )
        )
        assert duplicate is intent

        broker_order = orders.upsert_broker_order_state(
            BrokerOrder(
                **context_values(),
                order_intent_id=intent.order_intent_id,
                request_order_id=request_order_id,
                exchange_order_id="12345",
                broker_status="posted",
                lifecycle_seq=1,
                posted_at=now,
                cancelled_at=None,
                rejected_at=None,
                reject_reason_code=None,
                broker_tracking_id="tracking-1",
                last_observed_at=now,
                broker_payload={},
            )
        )
        stale_update = orders.upsert_broker_order_state(
            BrokerOrder(
                **context_values(),
                order_intent_id=intent.order_intent_id,
                request_order_id=request_order_id,
                exchange_order_id="12345",
                broker_status="created",
                lifecycle_seq=0,
                posted_at=None,
                cancelled_at=None,
                rejected_at=None,
                reject_reason_code=None,
                broker_tracking_id="tracking-0",
                last_observed_at=now,
                broker_payload={},
            )
        )
        fresh_update = orders.upsert_broker_order_state(
            BrokerOrder(
                **context_values(),
                order_intent_id=intent.order_intent_id,
                request_order_id=request_order_id,
                exchange_order_id="12345",
                broker_status="cancelled",
                lifecycle_seq=2,
                posted_at=now,
                cancelled_at=now,
                rejected_at=None,
                reject_reason_code=None,
                broker_tracking_id="tracking-2",
                last_observed_at=now,
                broker_payload={"source": "test"},
            )
        )

        assert stale_update is broker_order
        assert fresh_update is broker_order
        assert broker_order.broker_status == "cancelled"
        assert broker_order.lifecycle_seq == 2

        candle = market_data.upsert_candle(
            MarketCandle(
                **context_values(),
                instrument_id="MOEX:SBER",
                timeframe="5m",
                open_ts_utc=now,
                close_ts_utc=now,
                exchange_open_ts=now,
                exchange_close_ts=now,
                open_price=Decimal("300.00"),
                high_price=Decimal("301.00"),
                low_price=Decimal("299.50"),
                close_price=Decimal("300.50"),
                volume_lots=Decimal("10"),
                is_closed=True,
                source="test",
                candle_payload={},
            )
        )
        duplicate_candle = market_data.upsert_candle(
            MarketCandle(
                **context_values(),
                instrument_id="MOEX:SBER",
                timeframe="5m",
                open_ts_utc=now,
                close_ts_utc=now,
                exchange_open_ts=now,
                exchange_close_ts=now,
                open_price=Decimal("300.00"),
                high_price=Decimal("302.00"),
                low_price=Decimal("299.50"),
                close_price=Decimal("301.50"),
                volume_lots=Decimal("12"),
                is_closed=True,
                source="test",
                candle_payload={"updated": True},
            )
        )
        summary = market_data.save_order_book_summary(
            OrderBookSummary(
                **context_values(),
                ts_utc=now,
                exchange_ts=now,
                received_ts=now,
                instrument_id="MOEX:SBER",
                depth_levels=2,
                best_bid_price=Decimal("300.00"),
                best_bid_qty_lots=Decimal("5"),
                best_ask_price=Decimal("300.10"),
                best_ask_qty_lots=Decimal("4"),
                mid_price=Decimal("300.05"),
                spread_abs=Decimal("0.10"),
                spread_bps=Decimal("3.3330"),
                bid_depth_lots=Decimal("10"),
                ask_depth_lots=Decimal("8"),
                book_imbalance=Decimal("0.1111"),
                market_quality_score=Decimal("0.9000"),
                summary_payload={},
            )
        )
        microstructure = market_data.save_microstructure_snapshot(
            MarketMicrostructureSnapshot(
                **context_values(),
                ts_utc=now,
                exchange_ts=now,
                received_ts=now,
                instrument_id="MOEX:SBER",
                best_bid=Decimal("300.00"),
                best_ask=Decimal("300.10"),
                mid_price=Decimal("300.05"),
                spread_abs=Decimal("0.10"),
                spread_bps=Decimal("3.3330"),
                bid_depth_lots=Decimal("10"),
                ask_depth_lots=Decimal("8"),
                book_imbalance=Decimal("0.1111"),
                market_quality_score=Decimal("0.9000"),
                feed_freshness_age_ms=120,
                is_stale=False,
                source="data_only_shadow",
                snapshot_payload={"source": "test"},
            )
        )

        assert duplicate_candle is candle
        assert candle.high_price == Decimal("302.00")
        assert summary.instrument_id == "MOEX:SBER"
        assert microstructure.spread_bps == Decimal("3.3330")

    engine.dispose()


def test_deep_analytics_candidate_journey_helpers() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        seed_ids = seed_candidate_journey(session)
        analytics = AnalyticsReadRepository(session)
        micro_sessions = MicroSessionRepository(session)
        orders = OrderRepository(session)

        journey = analytics.get_candidate_journey(seed_ids.candidate_id)

        assert journey.candidate is not None
        assert journey.candidate.candidate_status == "blocked"
        assert len(journey.market_context) == 1
        assert journey.stage_results[0].stage_name == "risk_gate"
        assert journey.stage_results[0].blocker_code == "spread_too_wide"
        assert journey.blockers[0].blocker_family == "market_quality"
        assert journey.order_intents[0].request_order_id == seed_ids.request_order_id
        assert journey.broker_orders[0].tracking_id == "tracking-fixture"
        assert journey.order_state_events[0].cancel_reason_code == "spread_too_wide"
        assert journey.fills[0].pnl_net == Decimal("3.900000")
        assert journey.counterfactuals[0].pnl_net == Decimal("3.500000")

        ranking = analytics.blocker_ranking(trading_date=date(2026, 6, 13))
        assert ranking == [("spread_too_wide", 1)]

        recent_candidates = analytics.recent_candidates(
            trading_date=date(2026, 6, 13),
            instrument_id="MOEX:SBER",
            timeframe="5m",
        )
        assert [candidate.candidate_id for candidate in recent_candidates] == [
            seed_ids.candidate_id
        ]

        open_micro_sessions = micro_sessions.list_open(date(2026, 6, 13))
        assert [item.micro_session_id for item in open_micro_sessions] == [
            seed_ids.micro_session_id
        ]
        closed_micro_session = micro_sessions.close(
            seed_ids.micro_session_id,
            ended_at=datetime(2026, 6, 13, 11, 0, tzinfo=UTC),
            rollover_reason_code="hourly_rollover",
            snapshot_payload={"orders": 1},
        )
        assert closed_micro_session.status == "closed"
        assert closed_micro_session.snapshot_payload["orders"] == 1

        order_state_events = orders.list_order_state_events(seed_ids.order_intent_id)
        assert [event.state_seq for event in order_state_events] == [1]

    engine.dispose()

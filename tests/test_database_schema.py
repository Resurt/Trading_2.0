from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from trading_common.db.base import Base
from trading_common.db.models import (
    BrokerOrder,
    InstrumentRegistry,
    OrderIntent,
    SessionRun,
    StrategyConfig,
)
from trading_common.db.repositories import (
    InstrumentRepository,
    OrderRepository,
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
        "signal_candidate",
        "blocker_event",
        "order_intent",
        "broker_order",
        "fill_event",
        "risk_event",
        "position_snapshot",
        "strategy_state_event",
        "hourly_report",
        "daily_report",
        "counterfactual_result",
        "audit_event",
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

    engine.dispose()

"""Add deep analytics decision journal schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0003"
down_revision = "20260613_0002"
branch_labels = None
depends_on = None

SESSION_TYPES = (
    "weekday_morning",
    "weekday_main",
    "weekday_evening",
    "weekend",
)
SESSION_PHASES = (
    "opening_auction",
    "continuous_trading",
    "closing_auction",
    "break",
    "dealer_mode",
    "closed",
)
PARTITIONED_TABLES = (
    "market_context_snapshot",
    "candidate_stage_result",
    "order_state_event",
)


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def session_type_check(column_name: str = "session_type", *, nullable: bool = False) -> str:
    values = ", ".join(f"'{value}'" for value in SESSION_TYPES)
    expression = f"{column_name} in ({values})"
    if nullable:
        return f"{column_name} is null or {expression}"
    return expression


def session_phase_check(column_name: str = "session_phase", *, nullable: bool = False) -> str:
    values = ", ".join(f"'{value}'" for value in SESSION_PHASES)
    expression = f"{column_name} in ({values})"
    if nullable:
        return f"{column_name} is null or {expression}"
    return expression


def session_context_columns(*, trading_date_primary_key: bool = False) -> list[sa.Column[object]]:
    return [
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("trading_date", sa.Date(), primary_key=trading_date_primary_key, nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("session_phase", sa.String(length=32), nullable=False),
        sa.Column("micro_session_id", sa.String(length=96), nullable=False),
        sa.Column("broker_trading_status", sa.String(length=64), nullable=False),
    ]


def event_timestamp_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("ts_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_ts", sa.DateTime(timezone=True), nullable=True),
    ]


def report_metric_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("timeframe", sa.String(length=16), nullable=True),
        sa.Column("commission_gross", sa.Numeric(20, 6), nullable=True),
        sa.Column("commission_net", sa.Numeric(20, 6), nullable=True),
        sa.Column("slippage_bp", sa.Numeric(12, 4), nullable=True),
        sa.Column("pnl_gross", sa.Numeric(20, 6), nullable=True),
        sa.Column("pnl_net", sa.Numeric(20, 6), nullable=True),
    ]


def session_context_constraints(*, nullable: bool = False) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(session_type_check(nullable=nullable), name="session_type_values"),
        sa.CheckConstraint(session_phase_check(nullable=nullable), name="session_phase_values"),
    ]


def create_default_partitions() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name in PARTITIONED_TABLES:
        op.execute(
            sa.text(
                f"CREATE TABLE IF NOT EXISTS {table_name}_default "
                f"PARTITION OF {table_name} DEFAULT"
            )
        )


def drop_default_partitions() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name in PARTITIONED_TABLES:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table_name}_default"))


def upgrade() -> None:
    op.create_table(
        "micro_session",
        sa.Column("micro_session_id", sa.String(length=96), primary_key=True),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("session_phase", sa.String(length=32), nullable=False),
        sa.Column("broker_trading_status", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("session_run.run_id"), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("timeframe", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("freeze_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollover_reason_code", sa.String(length=64), nullable=True),
        sa.Column("snapshot_payload", jsonb_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        *session_context_constraints(),
    )
    op.create_index(
        "ix_micro_session_scope",
        "micro_session",
        ["trading_date", "session_type", "instrument_id", "timeframe"],
    )
    op.create_index("ix_micro_session_status", "micro_session", ["trading_date", "status"])

    op.create_table(
        "market_context_snapshot",
        sa.Column("market_context_snapshot_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("signal_candidate.candidate_id"),
            nullable=True,
        ),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("snapshot_kind", sa.String(length=64), nullable=False),
        sa.Column("last_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("mid_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("best_bid_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("best_ask_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_abs", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("bid_depth_lots", sa.Numeric(24, 8), nullable=True),
        sa.Column("ask_depth_lots", sa.Numeric(24, 8), nullable=True),
        sa.Column("book_imbalance", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_quality_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("candle_age_ms", sa.Integer(), nullable=True),
        sa.Column("data_freshness_ms", sa.Integer(), nullable=True),
        sa.Column("feature_snapshot", jsonb_type(), nullable=False),
        sa.Column("explanation_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index("ix_market_context_candidate", "market_context_snapshot", ["candidate_id"])
    op.create_index(
        "ix_market_context_scope",
        "market_context_snapshot",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    op.create_table(
        "candidate_stage_result",
        sa.Column("candidate_stage_result_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("signal_candidate.candidate_id"),
            nullable=False,
        ),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("stage_seq", sa.Integer(), nullable=False),
        sa.Column("stage_name", sa.String(length=64), nullable=False),
        sa.Column("stage_outcome", sa.String(length=32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("blocker_code", sa.String(length=64), nullable=True),
        sa.Column("blocker_family", sa.String(length=64), nullable=True),
        sa.Column("measured_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("threshold_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("explanation_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index(
        "ix_candidate_stage_candidate",
        "candidate_stage_result",
        ["candidate_id", "stage_seq"],
    )
    op.create_index(
        "ix_candidate_stage_blocker",
        "candidate_stage_result",
        ["trading_date", "blocker_code"],
    )
    op.create_index(
        "ix_candidate_stage_scope",
        "candidate_stage_result",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    op.create_table(
        "order_state_event",
        sa.Column("order_state_event_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column(
            "order_intent_id",
            sa.Uuid(),
            sa.ForeignKey("order_intent.order_intent_id"),
            nullable=True,
        ),
        sa.Column("broker_order_id", sa.Uuid(), nullable=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("timeframe", sa.String(length=16), nullable=True),
        sa.Column("request_order_id", sa.Uuid(), nullable=True),
        sa.Column("exchange_order_id", sa.String(length=96), nullable=True),
        sa.Column("tracking_id", sa.String(length=128), nullable=True),
        sa.Column("state_seq", sa.Integer(), nullable=False),
        sa.Column("previous_state", sa.String(length=64), nullable=True),
        sa.Column("new_state", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("cancel_reason_code", sa.String(length=64), nullable=True),
        sa.Column("reject_reason_code", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Numeric(20, 4), nullable=True),
        sa.Column("state_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index("ix_order_state_candidate", "order_state_event", ["candidate_id"])
    op.create_index(
        "ix_order_state_intent_seq",
        "order_state_event",
        ["order_intent_id", "state_seq"],
    )
    op.create_index(
        "ix_order_state_request_order_id",
        "order_state_event",
        ["request_order_id"],
    )
    op.create_index(
        "ix_order_state_exchange_order_id",
        "order_state_event",
        ["exchange_order_id"],
    )
    op.create_index(
        "ix_order_state_scope",
        "order_state_event",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    add_columns_and_indexes_to_existing_tables()
    create_default_partitions()


def add_columns_and_indexes_to_existing_tables() -> None:
    op.create_index(
        "ix_signal_candidate_scope",
        "signal_candidate",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    op.add_column("blocker_event", sa.Column("timeframe", sa.String(length=16), nullable=True))
    op.add_column("blocker_event", sa.Column("stage_seq", sa.Integer(), nullable=True))
    op.add_column("blocker_event", sa.Column("stage_name", sa.String(length=64), nullable=True))
    op.add_column("blocker_event", sa.Column("stage_outcome", sa.String(length=32), nullable=True))
    op.add_column("blocker_event", sa.Column("blocker_code", sa.String(length=64), nullable=True))
    op.add_column("blocker_event", sa.Column("blocker_family", sa.String(length=64), nullable=True))
    op.add_column("blocker_event", sa.Column("measured_value", sa.Numeric(20, 8), nullable=True))
    op.add_column("blocker_event", sa.Column("threshold_value", sa.Numeric(20, 8), nullable=True))
    op.add_column(
        "blocker_event",
        sa.Column("explanation_payload", jsonb_type(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_blocker_event_blocker_code",
        "blocker_event",
        ["trading_date", "blocker_code"],
    )
    op.create_index(
        "ix_blocker_event_scope",
        "blocker_event",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    op.add_column("order_intent", sa.Column("timeframe", sa.String(length=16), nullable=True))
    op.add_column("order_intent", sa.Column("strategy_version", sa.Integer(), nullable=True))
    op.add_column("order_intent", sa.Column("tracking_id", sa.String(length=128), nullable=True))
    op.create_index("ix_order_intent_candidate", "order_intent", ["candidate_id"])
    op.create_index(
        "ix_order_intent_scope",
        "order_intent",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    op.add_column("broker_order", sa.Column("candidate_id", sa.Uuid(), nullable=True))
    op.add_column("broker_order", sa.Column("instrument_id", sa.String(length=64), nullable=True))
    op.add_column("broker_order", sa.Column("timeframe", sa.String(length=16), nullable=True))
    op.add_column("broker_order", sa.Column("tracking_id", sa.String(length=128), nullable=True))
    op.create_index("ix_broker_order_candidate", "broker_order", ["candidate_id"])
    op.create_index(
        "ix_broker_order_scope",
        "broker_order",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    op.add_column("fill_event", sa.Column("candidate_id", sa.Uuid(), nullable=True))
    op.add_column("fill_event", sa.Column("order_intent_id", sa.Uuid(), nullable=True))
    op.add_column("fill_event", sa.Column("tracking_id", sa.String(length=128), nullable=True))
    op.add_column("fill_event", sa.Column("timeframe", sa.String(length=16), nullable=True))
    op.add_column("fill_event", sa.Column("commission_gross", sa.Numeric(20, 6), nullable=True))
    op.add_column("fill_event", sa.Column("commission_net", sa.Numeric(20, 6), nullable=True))
    op.add_column("fill_event", sa.Column("slippage_bp", sa.Numeric(12, 4), nullable=True))
    op.add_column("fill_event", sa.Column("pnl_gross", sa.Numeric(20, 6), nullable=True))
    op.add_column("fill_event", sa.Column("pnl_net", sa.Numeric(20, 6), nullable=True))
    op.create_index("ix_fill_event_candidate", "fill_event", ["candidate_id"])
    op.create_index(
        "ix_fill_event_scope",
        "fill_event",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    op.add_column("risk_event", sa.Column("timeframe", sa.String(length=16), nullable=True))
    op.create_index("ix_risk_event_candidate", "risk_event", ["candidate_id"])
    op.create_index(
        "ix_risk_event_scope",
        "risk_event",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )

    for column in report_metric_columns():
        op.add_column("hourly_report", column)
    op.create_index(
        "ix_hourly_report_scope",
        "hourly_report",
        ["instrument_id", "timeframe", "trading_date"],
    )

    for column in report_metric_columns():
        op.add_column("daily_report", column)
    op.create_index(
        "ix_daily_report_instrument_timeframe",
        "daily_report",
        ["instrument_id", "timeframe", "trading_date"],
    )

    op.add_column(
        "counterfactual_result",
        sa.Column("timeframe", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "counterfactual_result",
        sa.Column("slippage_bp", sa.Numeric(12, 4), nullable=True),
    )
    op.add_column("counterfactual_result", sa.Column("pnl_gross", sa.Numeric(20, 6), nullable=True))
    op.add_column("counterfactual_result", sa.Column("pnl_net", sa.Numeric(20, 6), nullable=True))
    op.create_index(
        "ix_counterfactual_scope",
        "counterfactual_result",
        ["instrument_id", "timeframe", "trading_date", "session_type"],
    )


def downgrade() -> None:
    drop_default_partitions()
    drop_existing_table_extensions()
    op.drop_table("order_state_event")
    op.drop_table("candidate_stage_result")
    op.drop_table("market_context_snapshot")
    op.drop_table("micro_session")


def drop_existing_table_extensions() -> None:
    op.drop_index("ix_counterfactual_scope", table_name="counterfactual_result")
    op.drop_column("counterfactual_result", "pnl_net")
    op.drop_column("counterfactual_result", "pnl_gross")
    op.drop_column("counterfactual_result", "slippage_bp")
    op.drop_column("counterfactual_result", "timeframe")

    op.drop_index("ix_daily_report_instrument_timeframe", table_name="daily_report")
    for column_name in (
        "pnl_net",
        "pnl_gross",
        "slippage_bp",
        "commission_net",
        "commission_gross",
        "timeframe",
    ):
        op.drop_column("daily_report", column_name)

    op.drop_index("ix_hourly_report_scope", table_name="hourly_report")
    for column_name in (
        "pnl_net",
        "pnl_gross",
        "slippage_bp",
        "commission_net",
        "commission_gross",
        "timeframe",
    ):
        op.drop_column("hourly_report", column_name)

    op.drop_index("ix_risk_event_scope", table_name="risk_event")
    op.drop_index("ix_risk_event_candidate", table_name="risk_event")
    op.drop_column("risk_event", "timeframe")

    op.drop_index("ix_fill_event_scope", table_name="fill_event")
    op.drop_index("ix_fill_event_candidate", table_name="fill_event")
    for column_name in (
        "pnl_net",
        "pnl_gross",
        "slippage_bp",
        "commission_net",
        "commission_gross",
        "timeframe",
        "tracking_id",
        "order_intent_id",
        "candidate_id",
    ):
        op.drop_column("fill_event", column_name)

    op.drop_index("ix_broker_order_scope", table_name="broker_order")
    op.drop_index("ix_broker_order_candidate", table_name="broker_order")
    for column_name in ("tracking_id", "timeframe", "instrument_id", "candidate_id"):
        op.drop_column("broker_order", column_name)

    op.drop_index("ix_order_intent_scope", table_name="order_intent")
    op.drop_index("ix_order_intent_candidate", table_name="order_intent")
    for column_name in ("tracking_id", "strategy_version", "timeframe"):
        op.drop_column("order_intent", column_name)

    op.drop_index("ix_blocker_event_scope", table_name="blocker_event")
    op.drop_index("ix_blocker_event_blocker_code", table_name="blocker_event")
    for column_name in (
        "explanation_payload",
        "threshold_value",
        "measured_value",
        "blocker_family",
        "blocker_code",
        "stage_outcome",
        "stage_name",
        "stage_seq",
        "timeframe",
    ):
        op.drop_column("blocker_event", column_name)

    op.drop_index("ix_signal_candidate_scope", table_name="signal_candidate")

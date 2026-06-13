"""Initial PostgreSQL schema for trading robot domain data."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0001"
down_revision = None
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
    "fill_event",
    "audit_event",
    "blocker_event",
    "strategy_state_event",
    "counterfactual_result",
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
        "instrument_registry",
        sa.Column("instrument_id", sa.String(length=64), primary_key=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("class_code", sa.String(length=16), nullable=False),
        sa.Column("figi", sa.String(length=32), nullable=True),
        sa.Column("instrument_uid", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("min_price_increment", sa.Numeric(20, 8), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("supports_morning", sa.Boolean(), nullable=False),
        sa.Column("supports_evening", sa.Boolean(), nullable=False),
        sa.Column("supports_weekend", sa.Boolean(), nullable=False),
        sa.Column("instrument_payload", jsonb_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ticker", name="uq_instrument_registry_ticker"),
        sa.UniqueConstraint("figi", name="uq_instrument_registry_figi"),
        sa.UniqueConstraint("instrument_uid", name="uq_instrument_registry_instrument_uid"),
    )

    op.create_table(
        "strategy_config",
        sa.Column("strategy_config_id", sa.Uuid(), primary_key=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("session_template", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_payload", jsonb_type(), nullable=False),
        sa.Column("risk_limits", jsonb_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(session_type_check("session_template"), name="session_template_values"),
        sa.UniqueConstraint(
            "strategy_id",
            "version",
            "session_template",
            name="uq_strategy_config_version_template",
        ),
    )
    op.create_index(
        "ix_strategy_config_active_template",
        "strategy_config",
        ["strategy_id", "session_template", "is_active"],
    )
    op.create_index(
        "uq_strategy_config_one_active_template",
        "strategy_config",
        ["strategy_id", "session_template"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "session_run",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        *session_context_columns(),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("freeze_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason_code", sa.String(length=64), nullable=True),
        sa.Column("run_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        sa.UniqueConstraint("micro_session_id", name="uq_session_run_micro_session_id"),
    )
    op.create_index(
        "ix_session_run_trading_date_type",
        "session_run",
        ["trading_date", "session_type"],
    )

    op.create_table(
        "signal_candidate",
        sa.Column("candidate_id", sa.Uuid(), primary_key=True),
        *session_context_columns(),
        *event_timestamp_columns(),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("session_run.run_id"), nullable=True),
        sa.Column(
            "instrument_id",
            sa.String(length=64),
            sa.ForeignKey("instrument_registry.instrument_id"),
            nullable=False,
        ),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("signal_type", sa.String(length=32), nullable=False),
        sa.Column("candidate_status", sa.String(length=32), nullable=False),
        sa.Column("expected_edge_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("expected_holding_minutes", sa.Integer(), nullable=True),
        sa.Column("last_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("mid_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_abs", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("market_quality_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("book_imbalance", sa.Numeric(8, 4), nullable=True),
        sa.Column("candle_age_ms", sa.Integer(), nullable=True),
        sa.Column("data_freshness_ms", sa.Integer(), nullable=True),
        sa.Column("signal_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("signal_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
    )
    op.create_index(
        "ix_signal_candidate_trading_date",
        "signal_candidate",
        ["trading_date", "session_type"],
    )
    op.create_index(
        "ix_signal_candidate_instrument",
        "signal_candidate",
        ["instrument_id", "timeframe"],
    )

    op.create_table(
        "blocker_event",
        sa.Column("blocker_event_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("signal_candidate.candidate_id"),
            nullable=True,
        ),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("gate_name", sa.String(length=64), nullable=False),
        sa.Column("gate_rank", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason_payload", jsonb_type(), nullable=False),
        sa.Column("is_final_blocker", sa.Boolean(), nullable=False),
        sa.Column("blocker_rank", sa.Integer(), nullable=True),
        sa.Column("market_quality_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("expected_edge_bps", sa.Numeric(12, 4), nullable=True),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index("ix_blocker_event_candidate", "blocker_event", ["candidate_id"])
    op.create_index("ix_blocker_event_reason", "blocker_event", ["trading_date", "reason_code"])

    op.create_table(
        "order_intent",
        sa.Column("order_intent_id", sa.Uuid(), primary_key=True),
        *session_context_columns(),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("signal_candidate.candidate_id"),
            nullable=True,
        ),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("order_action", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=32), nullable=False),
        sa.Column("lot_qty", sa.Integer(), nullable=False),
        sa.Column("intended_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("time_in_force", sa.String(length=32), nullable=False),
        sa.Column("request_order_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("execution_policy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cancel_reason_code", sa.String(length=64), nullable=True),
        sa.Column("reject_reason_code", sa.String(length=64), nullable=True),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("intent_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        sa.CheckConstraint("lot_qty > 0", name="positive_lot_qty"),
        sa.UniqueConstraint("request_order_id", name="uq_order_intent_request_order_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_order_intent_idempotency_key"),
    )
    op.create_index("ix_order_intent_lifecycle", "order_intent", ["trading_date", "status"])

    op.create_table(
        "broker_order",
        sa.Column("broker_order_id", sa.Uuid(), primary_key=True),
        *session_context_columns(),
        sa.Column(
            "order_intent_id",
            sa.Uuid(),
            sa.ForeignKey("order_intent.order_intent_id"),
            nullable=True,
        ),
        sa.Column("request_order_id", sa.Uuid(), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=96), nullable=True),
        sa.Column("broker_status", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_seq", sa.Integer(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason_code", sa.String(length=64), nullable=True),
        sa.Column("broker_tracking_id", sa.String(length=128), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broker_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        sa.CheckConstraint("lifecycle_seq >= 0", name="non_negative_lifecycle_seq"),
        sa.UniqueConstraint("request_order_id", name="uq_broker_order_request_order_id"),
    )
    op.create_index("ix_broker_order_status", "broker_order", ["trading_date", "broker_status"])
    op.create_index(
        "ix_broker_order_exchange_order_id",
        "broker_order",
        ["exchange_order_id"],
        unique=True,
    )

    op.create_table(
        "fill_event",
        sa.Column("fill_event_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column("request_order_id", sa.Uuid(), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=96), nullable=False),
        sa.Column("broker_fill_id", sa.String(length=96), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("lot_qty", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("commission", sa.Numeric(20, 6), nullable=False),
        sa.Column("liquidity_flag", sa.String(length=32), nullable=True),
        sa.Column("fill_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        sa.CheckConstraint("lot_qty > 0", name="positive_fill_lot_qty"),
        sa.UniqueConstraint(
            "exchange_order_id",
            "broker_fill_id",
            "trading_date",
            name="uq_fill_event_exchange_fill",
        ),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index("ix_fill_event_request_order_id", "fill_event", ["request_order_id"])

    op.create_table(
        "risk_event",
        sa.Column("risk_event_id", sa.Uuid(), primary_key=True),
        *session_context_columns(),
        *event_timestamp_columns(),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("order_intent_id", sa.Uuid(), nullable=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("risk_rule", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("limit_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("observed_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("action_taken", sa.String(length=64), nullable=False),
        sa.Column("risk_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
    )
    op.create_index("ix_risk_event_reason", "risk_event", ["trading_date", "reason_code"])

    op.create_table(
        "position_snapshot",
        sa.Column("position_snapshot_id", sa.Uuid(), primary_key=True),
        *session_context_columns(),
        sa.Column("snapshot_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("position_side", sa.String(length=16), nullable=False),
        sa.Column("qty_lots", sa.Integer(), nullable=False),
        sa.Column("avg_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("market_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("realised_pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("exposure", sa.Numeric(20, 6), nullable=True),
        sa.Column("snapshot_reason", sa.String(length=64), nullable=False),
        sa.Column("snapshot_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        sa.UniqueConstraint(
            "micro_session_id",
            "instrument_id",
            "account_id",
            "snapshot_ts",
            name="uq_position_snapshot_context",
        ),
    )
    op.create_index(
        "ix_position_snapshot_instrument",
        "position_snapshot",
        ["trading_date", "instrument_id"],
    )

    op.create_table(
        "strategy_state_event",
        sa.Column("strategy_state_event_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("previous_state", sa.String(length=64), nullable=True),
        sa.Column("new_state", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("state_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index(
        "ix_strategy_state_event_strategy",
        "strategy_state_event",
        ["trading_date", "strategy_id"],
    )

    op.create_table(
        "hourly_report",
        sa.Column("hourly_report_id", sa.Uuid(), primary_key=True),
        *session_context_columns(),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("session_run.run_id"), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("realised_pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("unrealised_pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("commission", sa.Numeric(20, 6), nullable=True),
        sa.Column("signal_count", sa.Integer(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("exit_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("reject_count", sa.Integer(), nullable=False),
        sa.Column("cancel_count", sa.Integer(), nullable=False),
        sa.Column("reconnect_count", sa.Integer(), nullable=False),
        sa.Column("risk_event_count", sa.Integer(), nullable=False),
        sa.Column("fill_ratio", sa.Numeric(8, 4), nullable=True),
        sa.Column("report_payload", jsonb_type(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        *session_context_constraints(),
        sa.UniqueConstraint(
            "micro_session_id",
            "strategy_id",
            name="uq_hourly_report_micro_strategy",
        ),
    )
    op.create_index(
        "ix_hourly_report_trading_date",
        "hourly_report",
        ["trading_date", "session_type"],
    )

    op.create_table(
        "daily_report",
        sa.Column("daily_report_id", sa.Uuid(), primary_key=True),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=True),
        sa.Column("session_phase", sa.String(length=32), nullable=True),
        sa.Column("micro_session_id", sa.String(length=96), nullable=True),
        sa.Column("broker_trading_status", sa.String(length=64), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("market_regime", sa.String(length=32), nullable=False),
        sa.Column("realised_pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("commission", sa.Numeric(20, 6), nullable=True),
        sa.Column("signal_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("fill_ratio", sa.Numeric(8, 4), nullable=True),
        sa.Column("report_payload", jsonb_type(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        *session_context_constraints(nullable=True),
    )
    op.create_index("ix_daily_report_trading_date", "daily_report", ["trading_date"])
    op.create_index(
        "ix_daily_report_scope",
        "daily_report",
        ["trading_date", "strategy_id", "session_type"],
    )

    op.create_table(
        "counterfactual_result",
        sa.Column("counterfactual_result_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("order_intent_id", sa.Uuid(), nullable=True),
        sa.Column("source_event_type", sa.String(length=32), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("blocker_code", sa.String(length=64), nullable=True),
        sa.Column("cancel_reason_code", sa.String(length=64), nullable=True),
        sa.Column("fee_bps_assumed", sa.Numeric(12, 4), nullable=False),
        sa.Column("slippage_bps_assumed", sa.Numeric(12, 4), nullable=False),
        sa.Column("mfe_5m_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mae_5m_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mfe_10m_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mae_10m_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mfe_15m_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mae_15m_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("would_profit_5m", sa.Boolean(), nullable=True),
        sa.Column("would_profit_10m", sa.Boolean(), nullable=True),
        sa.Column("would_profit_15m", sa.Boolean(), nullable=True),
        sa.Column("result_payload", jsonb_type(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index("ix_counterfactual_candidate", "counterfactual_result", ["candidate_id"])
    op.create_index(
        "ix_counterfactual_reason",
        "counterfactual_result",
        ["trading_date", "blocker_code", "cancel_reason_code"],
    )

    op.create_table(
        "audit_event",
        sa.Column("audit_event_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=96), nullable=False),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("audit_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index("ix_audit_event_entity", "audit_event", ["entity_type", "entity_id"])
    op.create_index("ix_audit_event_action", "audit_event", ["trading_date", "action"])

    create_default_partitions()
    seed_reference_data()


def seed_reference_data() -> None:
    instrument_table = sa.table(
        "instrument_registry",
        sa.column("instrument_id", sa.String()),
        sa.column("ticker", sa.String()),
        sa.column("class_code", sa.String()),
        sa.column("figi", sa.String()),
        sa.column("instrument_uid", sa.String()),
        sa.column("name", sa.String()),
        sa.column("lot_size", sa.Integer()),
        sa.column("min_price_increment", sa.Numeric()),
        sa.column("currency", sa.String()),
        sa.column("is_enabled", sa.Boolean()),
        sa.column("supports_morning", sa.Boolean()),
        sa.column("supports_evening", sa.Boolean()),
        sa.column("supports_weekend", sa.Boolean()),
        sa.column("instrument_payload", jsonb_type()),
    )
    op.bulk_insert(
        instrument_table,
        [
            {
                "instrument_id": "MOEX:SBER",
                "ticker": "SBER",
                "class_code": "TQBR",
                "figi": None,
                "instrument_uid": None,
                "name": "Sberbank ordinary shares",
                "lot_size": 10,
                "min_price_increment": Decimal("0.01"),
                "currency": "RUB",
                "is_enabled": True,
                "supports_morning": True,
                "supports_evening": True,
                "supports_weekend": False,
                "instrument_payload": {"seed": True},
            },
            {
                "instrument_id": "MOEX:GAZP",
                "ticker": "GAZP",
                "class_code": "TQBR",
                "figi": None,
                "instrument_uid": None,
                "name": "Gazprom ordinary shares",
                "lot_size": 10,
                "min_price_increment": Decimal("0.01"),
                "currency": "RUB",
                "is_enabled": True,
                "supports_morning": True,
                "supports_evening": True,
                "supports_weekend": False,
                "instrument_payload": {"seed": True},
            },
            {
                "instrument_id": "MOEX:LKOH",
                "ticker": "LKOH",
                "class_code": "TQBR",
                "figi": None,
                "instrument_uid": None,
                "name": "Lukoil ordinary shares",
                "lot_size": 1,
                "min_price_increment": Decimal("0.50"),
                "currency": "RUB",
                "is_enabled": True,
                "supports_morning": True,
                "supports_evening": True,
                "supports_weekend": False,
                "instrument_payload": {"seed": True},
            },
        ],
    )

    strategy_table = sa.table(
        "strategy_config",
        sa.column("strategy_config_id", sa.Uuid()),
        sa.column("strategy_id", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("session_template", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("valid_from", sa.DateTime(timezone=True)),
        sa.column("valid_to", sa.DateTime(timezone=True)),
        sa.column("config_payload", jsonb_type()),
        sa.column("risk_limits", jsonb_type()),
    )
    valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_config_ids = {
        "weekday_morning": UUID("00000000-0000-0000-0000-000000000101"),
        "weekday_main": UUID("00000000-0000-0000-0000-000000000102"),
        "weekday_evening": UUID("00000000-0000-0000-0000-000000000103"),
        "weekend": UUID("00000000-0000-0000-0000-000000000104"),
    }
    op.bulk_insert(
        strategy_table,
        [
            {
                "strategy_config_id": strategy_config_ids[session_template],
                "strategy_id": "baseline",
                "version": 1,
                "session_template": session_template,
                "is_active": True,
                "valid_from": valid_from,
                "valid_to": None,
                "config_payload": {"enabled": False, "template": session_template},
                "risk_limits": {"max_position_lots": 0, "max_daily_loss_rub": 0},
            }
            for session_template in SESSION_TYPES
        ],
    )


def downgrade() -> None:
    drop_default_partitions()
    op.drop_table("audit_event")
    op.drop_table("counterfactual_result")
    op.drop_table("daily_report")
    op.drop_table("hourly_report")
    op.drop_table("strategy_state_event")
    op.drop_table("position_snapshot")
    op.drop_table("risk_event")
    op.drop_table("fill_event")
    op.drop_table("broker_order")
    op.drop_table("order_intent")
    op.drop_table("blocker_event")
    op.drop_table("signal_candidate")
    op.drop_table("session_run")
    op.drop_table("strategy_config")
    op.drop_table("instrument_registry")

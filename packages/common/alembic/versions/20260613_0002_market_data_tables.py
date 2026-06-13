"""Add market data aggregate tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0002"
down_revision = "20260613_0001"
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
    "market_candle",
    "market_status_snapshot",
    "order_book_summary",
)


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def session_type_check(column_name: str = "session_type") -> str:
    values = ", ".join(f"'{value}'" for value in SESSION_TYPES)
    return f"{column_name} in ({values})"


def session_phase_check(column_name: str = "session_phase") -> str:
    values = ", ".join(f"'{value}'" for value in SESSION_PHASES)
    return f"{column_name} in ({values})"


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


def session_context_constraints() -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(session_type_check(), name="session_type_values"),
        sa.CheckConstraint(session_phase_check(), name="session_phase_values"),
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
        "market_candle",
        sa.Column("market_candle_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("open_ts_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_ts_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_open_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_close_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("high_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("low_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("close_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume_lots", sa.Numeric(24, 8), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("candle_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        sa.UniqueConstraint(
            "instrument_id",
            "timeframe",
            "open_ts_utc",
            "trading_date",
            name="uq_market_candle_bucket",
        ),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index(
        "ix_market_candle_lookup",
        "market_candle",
        ["instrument_id", "timeframe", "open_ts_utc"],
    )

    op.create_table(
        "market_status_snapshot",
        sa.Column("market_status_snapshot_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("trading_status", sa.String(length=64), nullable=False),
        sa.Column("api_trade_available", sa.Boolean(), nullable=False),
        sa.Column("status_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index(
        "ix_market_status_instrument_ts",
        "market_status_snapshot",
        ["instrument_id", "ts_utc"],
    )

    op.create_table(
        "order_book_summary",
        sa.Column("order_book_summary_id", sa.Uuid(), primary_key=True),
        *session_context_columns(trading_date_primary_key=True),
        *event_timestamp_columns(),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("depth_levels", sa.Integer(), nullable=False),
        sa.Column("best_bid_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("best_bid_qty_lots", sa.Numeric(24, 8), nullable=True),
        sa.Column("best_ask_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("best_ask_qty_lots", sa.Numeric(24, 8), nullable=True),
        sa.Column("mid_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_abs", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("bid_depth_lots", sa.Numeric(24, 8), nullable=False),
        sa.Column("ask_depth_lots", sa.Numeric(24, 8), nullable=False),
        sa.Column("book_imbalance", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_quality_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("summary_payload", jsonb_type(), nullable=False),
        *session_context_constraints(),
        postgresql_partition_by="RANGE (trading_date)",
    )
    op.create_index(
        "ix_order_book_summary_instrument_ts",
        "order_book_summary",
        ["instrument_id", "ts_utc"],
    )

    create_default_partitions()


def downgrade() -> None:
    drop_default_partitions()
    op.drop_table("order_book_summary")
    op.drop_table("market_status_snapshot")
    op.drop_table("market_candle")

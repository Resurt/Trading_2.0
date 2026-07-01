"""Add exchange freshness metadata and data-only trade tape samples.

Revision ID: 20260613_0017
Revises: 20260613_0016
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260613_0017"
down_revision: str | None = "20260613_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


_FRESHNESS_COLUMNS = (
    sa.Column("exchange_age_ms", sa.Integer(), nullable=True),
    sa.Column("received_age_ms", sa.Integer(), nullable=True),
    sa.Column(
        "stale_by_exchange_time",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column(
        "stale_by_received_time",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("freshness_basis", sa.String(length=32), nullable=True),
    sa.Column("exchange_ts_missing_reason", sa.String(length=96), nullable=True),
    sa.Column(
        "strict_dual_freshness_eligible",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
)


def upgrade() -> None:
    for table_name in ("market_microstructure_snapshot", "order_book_summary"):
        for column in _FRESHNESS_COLUMNS:
            op.add_column(table_name, column.copy())

    op.create_table(
        "market_trade_sample",
        sa.Column("market_trade_sample_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("session_phase", sa.String(length=32), nullable=False),
        sa.Column("micro_session_id", sa.String(length=96), nullable=False),
        sa.Column("broker_trading_status", sa.String(length=64), nullable=False),
        sa.Column("exchange_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("quantity_lots", sa.Numeric(24, 8), nullable=True),
        sa.Column("side", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("venue_type", sa.String(length=32), nullable=True),
        sa.Column("trade_id", sa.String(length=128), nullable=True),
        sa.Column(
            "include_in_calibration",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload", _json_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("market_trade_sample_id"),
    )
    op.create_index(
        "ix_market_trade_sample_instrument_ts",
        "market_trade_sample",
        ["instrument_id", "received_ts"],
    )
    op.create_index(
        "ix_market_trade_sample_trading_date",
        "market_trade_sample",
        ["trading_date", "instrument_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_trade_sample_trading_date", table_name="market_trade_sample")
    op.drop_index("ix_market_trade_sample_instrument_ts", table_name="market_trade_sample")
    op.drop_table("market_trade_sample")
    for table_name in ("order_book_summary", "market_microstructure_snapshot"):
        for column in reversed(_FRESHNESS_COLUMNS):
            op.drop_column(table_name, column.name)

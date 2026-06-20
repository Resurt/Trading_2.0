"""Add data-only shadow microstructure snapshots.

Revision ID: 20260613_0012
Revises: 20260613_0011
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260613_0012"
down_revision: str | None = "20260613_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_microstructure_snapshot",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("ts_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("session_phase", sa.String(length=32), nullable=False),
        sa.Column("micro_session_id", sa.String(length=96), nullable=False),
        sa.Column("broker_trading_status", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("best_bid", sa.Numeric(20, 8), nullable=True),
        sa.Column("best_ask", sa.Numeric(20, 8), nullable=True),
        sa.Column("mid_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_abs", sa.Numeric(20, 8), nullable=True),
        sa.Column("spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("bid_depth_lots", sa.Numeric(24, 8), nullable=True),
        sa.Column("ask_depth_lots", sa.Numeric(24, 8), nullable=True),
        sa.Column("book_imbalance", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_quality_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("feed_freshness_age_ms", sa.Integer(), nullable=True),
        sa.Column("is_stale", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("snapshot_payload", _json_type(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    op.create_index(
        "ix_market_microstructure_instrument_ts",
        "market_microstructure_snapshot",
        ["instrument_id", "ts_utc"],
    )
    op.create_index(
        "ix_market_microstructure_date_instrument",
        "market_microstructure_snapshot",
        ["trading_date", "instrument_id"],
    )
    op.create_index(
        "ix_market_microstructure_session_date",
        "market_microstructure_snapshot",
        ["session_type", "trading_date"],
    )
    op.create_index(
        "ix_market_microstructure_spread_bps",
        "market_microstructure_snapshot",
        ["spread_bps"],
    )
    op.create_index(
        "ix_market_microstructure_quality",
        "market_microstructure_snapshot",
        ["market_quality_score"],
    )


def downgrade() -> None:
    for index_name in (
        "ix_market_microstructure_quality",
        "ix_market_microstructure_spread_bps",
        "ix_market_microstructure_session_date",
        "ix_market_microstructure_date_instrument",
        "ix_market_microstructure_instrument_ts",
    ):
        op.drop_index(index_name, table_name="market_microstructure_snapshot")
    op.drop_table("market_microstructure_snapshot")

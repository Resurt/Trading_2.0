"""Add dividend sync run read model."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0009"
down_revision = "20260613_0008"
branch_labels = None
depends_on = None


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "dividend_sync_run",
        sa.Column("dividend_sync_run_id", sa.Uuid(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("clean", sa.Boolean(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("instruments", jsonb_type(), nullable=False),
        sa.Column("instruments_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_instruments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_instruments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dividends_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dividends_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dividends_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("existing_unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("special_days_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "future_risk_windows_created",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_payload", jsonb_type(), nullable=False),
    )
    op.create_index(
        "ix_dividend_sync_run_finished_at",
        "dividend_sync_run",
        ["finished_at"],
    )
    op.create_index("ix_dividend_sync_run_status", "dividend_sync_run", ["status"])
    op.create_index("ix_dividend_sync_run_clean", "dividend_sync_run", ["clean"])


def downgrade() -> None:
    op.drop_index("ix_dividend_sync_run_clean", table_name="dividend_sync_run")
    op.drop_index("ix_dividend_sync_run_status", table_name="dividend_sync_run")
    op.drop_index("ix_dividend_sync_run_finished_at", table_name="dividend_sync_run")
    op.drop_table("dividend_sync_run")

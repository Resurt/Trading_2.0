"""Add historical quality and calibration report tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0007"
down_revision = "20260613_0006"
branch_labels = None
depends_on = None


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "historical_data_quality_report",
        sa.Column("report_id", sa.Uuid(), primary_key=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("instruments", jsonb_type(), nullable=False),
        sa.Column("timeframes", jsonb_type(), nullable=False),
        sa.Column("coverage_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("expected_candles", sa.Integer(), nullable=False),
        sa.Column("actual_candles", sa.Integer(), nullable=False),
        sa.Column("missing_intervals", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("invalid_ohlc_count", sa.Integer(), nullable=False),
        sa.Column("abnormal_gap_count", sa.Integer(), nullable=False),
        sa.Column("report_payload", jsonb_type(), nullable=False),
    )
    op.create_index(
        "ix_historical_quality_generated_at",
        "historical_data_quality_report",
        ["generated_at"],
    )
    op.create_index(
        "ix_historical_quality_period",
        "historical_data_quality_report",
        ["from_date", "to_date"],
    )
    op.create_index(
        "ix_historical_quality_coverage",
        "historical_data_quality_report",
        ["coverage_pct"],
    )

    op.create_table(
        "calibration_report",
        sa.Column("calibration_report_id", sa.Uuid(), primary_key=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("instruments", jsonb_type(), nullable=False),
        sa.Column("timeframes", jsonb_type(), nullable=False),
        sa.Column("group_by", jsonb_type(), nullable=False),
        sa.Column("report_payload", jsonb_type(), nullable=False),
    )
    op.create_index("ix_calibration_report_generated_at", "calibration_report", ["generated_at"])
    op.create_index("ix_calibration_report_strategy", "calibration_report", ["strategy_id"])
    op.create_index(
        "ix_calibration_report_period",
        "calibration_report",
        ["from_date", "to_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_calibration_report_period", table_name="calibration_report")
    op.drop_index("ix_calibration_report_strategy", table_name="calibration_report")
    op.drop_index("ix_calibration_report_generated_at", table_name="calibration_report")
    op.drop_table("calibration_report")

    op.drop_index(
        "ix_historical_quality_coverage",
        table_name="historical_data_quality_report",
    )
    op.drop_index("ix_historical_quality_period", table_name="historical_data_quality_report")
    op.drop_index(
        "ix_historical_quality_generated_at",
        table_name="historical_data_quality_report",
    )
    op.drop_table("historical_data_quality_report")

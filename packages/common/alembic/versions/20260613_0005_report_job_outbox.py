"""Add report job outbox for report-worker dispatch."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0005"
down_revision = "20260613_0004"
branch_labels = None
depends_on = None


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "report_job_outbox",
        sa.Column("report_job_id", sa.Uuid(), primary_key=True),
        sa.Column("celery_task_id", sa.String(length=128), nullable=True),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("report_type", sa.String(length=32), nullable=False),
        sa.Column("micro_session_id", sa.String(length=96), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=2048), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("job_payload", jsonb_type(), nullable=False),
        sa.Column("result_payload", jsonb_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("retry_count >= 0", name="ck_report_job_retry_count_non_negative"),
        sa.CheckConstraint("max_retries >= 1", name="ck_report_job_max_retries_positive"),
        sa.UniqueConstraint("idempotency_key", name="uq_report_job_outbox_idempotency_key"),
    )
    op.create_index(
        "ix_report_job_outbox_status",
        "report_job_outbox",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_report_job_outbox_micro_strategy",
        "report_job_outbox",
        ["micro_session_id", "strategy_id"],
    )
    op.create_index(
        "ix_report_job_outbox_celery_task_id",
        "report_job_outbox",
        ["celery_task_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_report_job_outbox_celery_task_id", table_name="report_job_outbox")
    op.drop_index("ix_report_job_outbox_micro_strategy", table_name="report_job_outbox")
    op.drop_index("ix_report_job_outbox_status", table_name="report_job_outbox")
    op.drop_table("report_job_outbox")

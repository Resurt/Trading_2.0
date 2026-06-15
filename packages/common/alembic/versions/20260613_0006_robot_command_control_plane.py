"""Add persistent robot command control plane."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0006"
down_revision = "20260613_0005"
branch_labels = None
depends_on = None


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "robot_command",
        sa.Column("command_id", sa.Uuid(), primary_key=True),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=96), nullable=False),
        sa.Column("requested_role", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", jsonb_type(), nullable=False),
        sa.Column("result_payload", jsonb_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "command_type in ('start', 'stop', 'pause', 'resume', 'emergency_stop')",
            name="ck_robot_command_type",
        ),
        sa.CheckConstraint(
            "status in ('requested', 'accepted', 'applied', 'rejected', 'failed')",
            name="ck_robot_command_status",
        ),
    )
    op.create_index(
        "ix_robot_command_status_requested",
        "robot_command",
        ["status", "requested_at"],
    )
    op.create_index(
        "ix_robot_command_type_requested",
        "robot_command",
        ["command_type", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_robot_command_type_requested", table_name="robot_command")
    op.drop_index("ix_robot_command_status_requested", table_name="robot_command")
    op.drop_table("robot_command")

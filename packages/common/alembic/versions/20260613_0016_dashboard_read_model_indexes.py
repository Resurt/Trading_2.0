"""Add dashboard read model indexes.

Revision ID: 20260613_0016
Revises: 20260613_0015
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260613_0016"
down_revision: str | None = "20260613_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_session_run_started_at", "session_run", ["started_at"])
    op.create_index(
        "ix_strategy_state_event_ts",
        "strategy_state_event",
        ["ts_utc"],
    )
    op.create_index(
        "ix_robot_command_requested_at",
        "robot_command",
        ["requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_robot_command_requested_at", table_name="robot_command")
    op.drop_index("ix_strategy_state_event_ts", table_name="strategy_state_event")
    op.drop_index("ix_session_run_started_at", table_name="session_run")

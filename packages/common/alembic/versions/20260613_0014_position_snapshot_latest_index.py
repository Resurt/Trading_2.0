"""Add latest position snapshot index.

Revision ID: 20260613_0014
Revises: 20260613_0013
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260613_0014"
down_revision: str | None = "20260613_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_position_snapshot_snapshot_ts",
        "position_snapshot",
        ["snapshot_ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_position_snapshot_snapshot_ts", table_name="position_snapshot")

"""Add broker order status observed index.

Revision ID: 20260613_0015
Revises: 20260613_0014
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260613_0015"
down_revision: str | None = "20260613_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_broker_order_status_observed",
        "broker_order",
        ["broker_status", "last_observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_broker_order_status_observed", table_name="broker_order")

"""Add instrument resolution hygiene fields.

Revision ID: 20260613_0010
Revises: 20260613_0009
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260613_0010"
down_revision: str | None = "20260613_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column(
        "instrument_registry",
        sa.Column("source", sa.String(length=32), nullable=False, server_default="seed"),
    )
    op.add_column(
        "instrument_registry",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "instrument_registry",
        sa.Column(
            "resolution_status",
            sa.String(length=32),
            nullable=False,
            server_default="unresolved",
        ),
    )
    op.add_column(
        "instrument_registry",
        sa.Column("resolution_error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "instrument_registry",
        sa.Column("resolution_error_message", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "instrument_registry",
        sa.Column("broker_payload", jsonb_type(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            update instrument_registry
            set source = 'seed',
                resolution_status = 'unresolved'
            where instrument_id like 'MOEX:%'
              and instrument_uid is null
            """
        )
    )


def downgrade() -> None:
    op.drop_column("instrument_registry", "broker_payload")
    op.drop_column("instrument_registry", "resolution_error_message")
    op.drop_column("instrument_registry", "resolution_error_code")
    op.drop_column("instrument_registry", "resolution_status")
    op.drop_column("instrument_registry", "resolved_at")
    op.drop_column("instrument_registry", "source")

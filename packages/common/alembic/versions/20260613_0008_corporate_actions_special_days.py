"""Add corporate action and market special day tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0008"
down_revision = "20260613_0007"
branch_labels = None
depends_on = None


def jsonb_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "corporate_action_event",
        sa.Column("corporate_action_id", sa.Uuid(), primary_key=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("declared_date", sa.Date(), nullable=True),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("registry_close_date", sa.Date(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("amount_per_share", sa.Numeric(20, 6), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("action_payload", jsonb_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "instrument_id",
            "action_type",
            "ex_date",
            "amount_per_share",
            "source",
            name="uq_corporate_action_identity",
        ),
    )
    op.create_index(
        "ix_corporate_action_instrument_ex_date",
        "corporate_action_event",
        ["instrument_id", "ex_date"],
    )
    op.create_index(
        "ix_corporate_action_ticker_ex_date",
        "corporate_action_event",
        ["ticker", "ex_date"],
    )
    op.create_index(
        "ix_corporate_action_type_ex_date",
        "corporate_action_event",
        ["action_type", "ex_date"],
    )
    op.create_index("ix_corporate_action_source", "corporate_action_event", ["source"])

    op.create_table(
        "market_special_day",
        sa.Column("special_day_id", sa.Uuid(), primary_key=True),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column("special_day_type", sa.String(length=64), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("linked_corporate_action_id", sa.Uuid(), nullable=True),
        sa.Column("open_gap_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("previous_close", sa.Numeric(20, 8), nullable=True),
        sa.Column("session_open_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("expected_dividend_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("detected_gap_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column(
            "exclude_from_primary_calibration",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "trade_policy",
            sa.String(length=32),
            nullable=False,
            server_default="shadow_only",
        ),
        sa.Column("special_day_payload", jsonb_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["linked_corporate_action_id"],
            ["corporate_action_event.corporate_action_id"],
        ),
        sa.UniqueConstraint(
            "trading_date",
            "instrument_id",
            "special_day_type",
            "reason_code",
            name="uq_market_special_day_identity",
        ),
    )
    op.create_index(
        "ix_market_special_day_trading_instrument",
        "market_special_day",
        ["trading_date", "instrument_id"],
    )
    op.create_index(
        "ix_market_special_day_type_date",
        "market_special_day",
        ["special_day_type", "trading_date"],
    )
    op.create_index(
        "ix_market_special_day_excluded",
        "market_special_day",
        ["exclude_from_primary_calibration"],
    )
    op.create_index(
        "ix_market_special_day_corporate_action",
        "market_special_day",
        ["linked_corporate_action_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_special_day_corporate_action", table_name="market_special_day")
    op.drop_index("ix_market_special_day_excluded", table_name="market_special_day")
    op.drop_index("ix_market_special_day_type_date", table_name="market_special_day")
    op.drop_index("ix_market_special_day_trading_instrument", table_name="market_special_day")
    op.drop_table("market_special_day")

    op.drop_index("ix_corporate_action_source", table_name="corporate_action_event")
    op.drop_index("ix_corporate_action_type_ex_date", table_name="corporate_action_event")
    op.drop_index("ix_corporate_action_ticker_ex_date", table_name="corporate_action_event")
    op.drop_index("ix_corporate_action_instrument_ex_date", table_name="corporate_action_event")
    op.drop_table("corporate_action_event")

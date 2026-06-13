"""Add idempotency keys for domain journaling."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260613_0004"
down_revision = "20260613_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("broker_order", sa.Column("latency_ms", sa.Numeric(20, 4), nullable=True))
    op.create_index(
        "ux_signal_candidate_fingerprint",
        "signal_candidate",
        ["signal_fingerprint"],
        unique=True,
    )
    op.create_index(
        "ux_market_context_candidate_kind",
        "market_context_snapshot",
        ["candidate_id", "snapshot_kind", "trading_date"],
        unique=True,
    )
    op.create_index(
        "uq_candidate_stage_result_candidate_seq",
        "candidate_stage_result",
        ["candidate_id", "stage_seq", "trading_date"],
        unique=True,
    )
    op.create_index(
        "uq_blocker_event_candidate_gate_reason",
        "blocker_event",
        ["candidate_id", "gate_rank", "reason_code", "trading_date"],
        unique=True,
    )
    op.create_index(
        "uq_order_state_event_intent_seq_type",
        "order_state_event",
        ["order_intent_id", "state_seq", "event_type", "trading_date"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_order_state_event_intent_seq_type",
        table_name="order_state_event",
    )
    op.drop_index(
        "uq_blocker_event_candidate_gate_reason",
        table_name="blocker_event",
    )
    op.drop_index(
        "uq_candidate_stage_result_candidate_seq",
        table_name="candidate_stage_result",
    )
    op.drop_index("ux_market_context_candidate_kind", table_name="market_context_snapshot")
    op.drop_index("ux_signal_candidate_fingerprint", table_name="signal_candidate")
    op.drop_column("broker_order", "latency_ms")

"""Add indexes for historical pipeline rebuilds.

Revision ID: 20260613_0011
Revises: 20260613_0010
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260613_0011"
down_revision: str | None = "20260613_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        create index if not exists ix_market_special_day_source
        on market_special_day (source)
        """
    )
    op.execute(
        """
        create index if not exists ix_corporate_action_source_type
        on corporate_action_event (source, action_type)
        """
    )
    op.execute(
        """
        create index if not exists ix_hourly_report_micro_strategy_365d
        on hourly_report (micro_session_id, strategy_id)
        """
    )
    op.execute(
        """
        create index if not exists ix_hourly_report_date_strategy_session
        on hourly_report (trading_date, strategy_id, session_type)
        """
    )
    op.execute(
        """
        create index if not exists ix_signal_candidate_date_strategy_scope
        on signal_candidate (trading_date, strategy_id, instrument_id, timeframe)
        """
    )
    op.execute(
        """
        create index if not exists ix_signal_candidate_micro_strategy
        on signal_candidate (micro_session_id, strategy_id)
        """
    )
    op.execute(
        """
        create index if not exists ix_blocker_event_date_strategy_scope
        on blocker_event (trading_date, strategy_id, instrument_id, timeframe)
        """
    )
    op.execute(
        """
        create index if not exists ix_blocker_event_candidate_reason
        on blocker_event (candidate_id, reason_code)
        """
    )
    op.execute(
        """
        create index if not exists ix_counterfactual_date_strategy_scope
        on counterfactual_result (trading_date, strategy_id, instrument_id, timeframe)
        """
    )
    op.execute(
        """
        create index if not exists ix_counterfactual_candidate_blocker
        on counterfactual_result (candidate_id, blocker_code)
        """
    )


def downgrade() -> None:
    for index_name in (
        "ix_counterfactual_candidate_blocker",
        "ix_counterfactual_date_strategy_scope",
        "ix_blocker_event_candidate_reason",
        "ix_blocker_event_date_strategy_scope",
        "ix_signal_candidate_micro_strategy",
        "ix_signal_candidate_date_strategy_scope",
        "ix_hourly_report_date_strategy_session",
        "ix_hourly_report_micro_strategy_365d",
        "ix_corporate_action_source_type",
        "ix_market_special_day_source",
    ):
        op.execute(f"drop index if exists {index_name}")

"""Add intraday analytics and calibration center tables.

Revision ID: 20260613_0013
Revises: 20260613_0012
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260613_0013"
down_revision: str | None = "20260613_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "intraday_session_analytics",
        sa.Column("intraday_analytics_id", sa.Uuid(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("session_phase", sa.String(length=32), nullable=False),
        sa.Column("micro_session_id", sa.String(length=96), nullable=True),
        sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("timeframe", sa.String(length=16), nullable=True),
        sa.Column("side", sa.String(length=16), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("market_bias", sa.String(length=32), nullable=False),
        sa.Column("market_activity", sa.String(length=32), nullable=False),
        sa.Column("trend_strength", sa.Numeric(12, 4), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("pseudo_order_count", sa.Integer(), nullable=False),
        sa.Column("real_order_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("near_miss_count", sa.Integer(), nullable=False),
        sa.Column("avg_spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("p95_spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg_depth", sa.Numeric(24, 8), nullable=True),
        sa.Column("avg_imbalance", sa.Numeric(8, 4), nullable=True),
        sa.Column("avg_market_quality", sa.Numeric(8, 4), nullable=True),
        sa.Column("stale_incidents", sa.Integer(), nullable=False),
        sa.Column("candle_lag_p95_seconds", sa.Numeric(20, 4), nullable=True),
        sa.Column("gross_pnl_proxy", sa.Numeric(20, 6), nullable=True),
        sa.Column("net_pnl_proxy", sa.Numeric(20, 6), nullable=True),
        sa.Column("analytics_payload", _json_type(), nullable=False),
        sa.PrimaryKeyConstraint("intraday_analytics_id"),
    )
    op.create_index(
        "ix_intraday_analytics_trading_session",
        "intraday_session_analytics",
        ["trading_date", "session_type"],
    )
    op.create_index(
        "ix_intraday_analytics_trading_instrument",
        "intraday_session_analytics",
        ["trading_date", "instrument_id"],
    )
    op.create_index(
        "ix_intraday_analytics_scope",
        "intraday_session_analytics",
        ["trading_date", "session_type", "instrument_id", "timeframe", "side"],
    )
    op.create_index(
        "ix_intraday_analytics_generated_at",
        "intraday_session_analytics",
        ["generated_at"],
    )
    op.create_index("ix_intraday_analytics_mode", "intraday_session_analytics", ["mode"])

    op.create_table(
        "rolling_performance_cube",
        sa.Column("cube_id", sa.Uuid(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_name", sa.String(length=16), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("approved_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("pseudo_order_count", sa.Integer(), nullable=False),
        sa.Column("real_order_count", sa.Integer(), nullable=False),
        sa.Column("gross_pnl_proxy", sa.Numeric(20, 6), nullable=False),
        sa.Column("net_pnl_proxy", sa.Numeric(20, 6), nullable=False),
        sa.Column("avg_net_pnl_proxy", sa.Numeric(20, 6), nullable=False),
        sa.Column("win_proxy", sa.Numeric(8, 4), nullable=True),
        sa.Column("avg_spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("p95_spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg_depth", sa.Numeric(24, 8), nullable=True),
        sa.Column("p95_depth", sa.Numeric(24, 8), nullable=True),
        sa.Column("avg_imbalance", sa.Numeric(8, 4), nullable=True),
        sa.Column("avg_market_quality", sa.Numeric(8, 4), nullable=True),
        sa.Column("stale_incidents", sa.Integer(), nullable=False),
        sa.Column("stream_gap_count", sa.Integer(), nullable=False),
        sa.Column("active_days", sa.Integer(), nullable=False),
        sa.Column("last_signal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_warning", sa.String(length=256), nullable=True),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("contour_status", sa.String(length=32), nullable=False),
        sa.Column("cube_payload", _json_type(), nullable=False),
        sa.PrimaryKeyConstraint("cube_id"),
    )
    op.create_index(
        "ix_rolling_cube_window_scope",
        "rolling_performance_cube",
        ["window_name", "instrument_id", "timeframe", "side"],
    )
    op.create_index(
        "ix_rolling_cube_generated_at",
        "rolling_performance_cube",
        ["generated_at"],
    )
    op.create_index(
        "ix_rolling_cube_contour_status",
        "rolling_performance_cube",
        ["contour_status"],
    )
    op.create_index("ix_rolling_cube_mode", "rolling_performance_cube", ["mode"])
    op.create_index(
        "ix_rolling_cube_full_scope",
        "rolling_performance_cube",
        ["instrument_id", "session_type", "timeframe", "side"],
    )

    op.create_table(
        "calibration_diagnostic_run",
        sa.Column("diagnostic_run_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=True),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("from_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("to_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe", _json_type(), nullable=False),
        sa.Column("diagnosis", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("blocking_issues", _json_type(), nullable=False),
        sa.Column("warnings", _json_type(), nullable=False),
        sa.Column("diagnostic_payload", _json_type(), nullable=False),
        sa.PrimaryKeyConstraint("diagnostic_run_id"),
    )
    op.create_index(
        "ix_calibration_diagnostic_created_at",
        "calibration_diagnostic_run",
        ["created_at"],
    )
    op.create_index(
        "ix_calibration_diagnostic_status",
        "calibration_diagnostic_run",
        ["status"],
    )
    op.create_index(
        "ix_calibration_diagnostic_trigger",
        "calibration_diagnostic_run",
        ["trigger_type"],
    )
    op.create_index(
        "ix_calibration_diagnostic_diagnosis",
        "calibration_diagnostic_run",
        ["diagnosis"],
    )

    op.create_table(
        "strategy_config_candidate",
        sa.Column("candidate_config_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_diagnostic_run_id", sa.Uuid(), nullable=True),
        sa.Column("base_strategy_id", sa.String(length=64), nullable=False),
        sa.Column("proposed_strategy_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("proposed_by", sa.String(length=32), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposal_payload", _json_type(), nullable=False),
        sa.Column("validation_payload", _json_type(), nullable=False),
        sa.Column("caveats", _json_type(), nullable=False),
        sa.Column("rejection_reason", sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_diagnostic_run_id"],
            ["calibration_diagnostic_run.diagnostic_run_id"],
        ),
        sa.PrimaryKeyConstraint("candidate_config_id"),
    )
    op.create_index(
        "ix_strategy_config_candidate_created_at",
        "strategy_config_candidate",
        ["created_at"],
    )
    op.create_index(
        "ix_strategy_config_candidate_status",
        "strategy_config_candidate",
        ["status"],
    )
    op.create_index(
        "ix_strategy_config_candidate_base",
        "strategy_config_candidate",
        ["base_strategy_id"],
    )
    op.create_index(
        "ix_strategy_config_candidate_source_run",
        "strategy_config_candidate",
        ["source_diagnostic_run_id"],
    )

    op.create_table(
        "market_regime_snapshot",
        sa.Column("regime_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=True),
        sa.Column("session_type", sa.String(length=32), nullable=True),
        sa.Column("market_regime", sa.String(length=32), nullable=False),
        sa.Column("volume_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("volatility_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("spread_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("depth_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("imbalance_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("candidate_frequency_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("regime_payload", _json_type(), nullable=False),
        sa.PrimaryKeyConstraint("regime_snapshot_id"),
    )
    op.create_index(
        "ix_market_regime_generated_at",
        "market_regime_snapshot",
        ["generated_at"],
    )
    op.create_index(
        "ix_market_regime_window",
        "market_regime_snapshot",
        ["window_start", "window_end"],
    )
    op.create_index(
        "ix_market_regime_instrument_session",
        "market_regime_snapshot",
        ["instrument_id", "session_type"],
    )
    op.create_index("ix_market_regime_label", "market_regime_snapshot", ["market_regime"])


def downgrade() -> None:
    op.drop_index("ix_market_regime_label", table_name="market_regime_snapshot")
    op.drop_index("ix_market_regime_instrument_session", table_name="market_regime_snapshot")
    op.drop_index("ix_market_regime_window", table_name="market_regime_snapshot")
    op.drop_index("ix_market_regime_generated_at", table_name="market_regime_snapshot")
    op.drop_table("market_regime_snapshot")

    op.drop_index(
        "ix_strategy_config_candidate_source_run",
        table_name="strategy_config_candidate",
    )
    op.drop_index("ix_strategy_config_candidate_base", table_name="strategy_config_candidate")
    op.drop_index("ix_strategy_config_candidate_status", table_name="strategy_config_candidate")
    op.drop_index(
        "ix_strategy_config_candidate_created_at",
        table_name="strategy_config_candidate",
    )
    op.drop_table("strategy_config_candidate")

    op.drop_index(
        "ix_calibration_diagnostic_diagnosis",
        table_name="calibration_diagnostic_run",
    )
    op.drop_index("ix_calibration_diagnostic_trigger", table_name="calibration_diagnostic_run")
    op.drop_index("ix_calibration_diagnostic_status", table_name="calibration_diagnostic_run")
    op.drop_index(
        "ix_calibration_diagnostic_created_at",
        table_name="calibration_diagnostic_run",
    )
    op.drop_table("calibration_diagnostic_run")

    op.drop_index("ix_rolling_cube_full_scope", table_name="rolling_performance_cube")
    op.drop_index("ix_rolling_cube_mode", table_name="rolling_performance_cube")
    op.drop_index("ix_rolling_cube_contour_status", table_name="rolling_performance_cube")
    op.drop_index("ix_rolling_cube_generated_at", table_name="rolling_performance_cube")
    op.drop_index("ix_rolling_cube_window_scope", table_name="rolling_performance_cube")
    op.drop_table("rolling_performance_cube")

    op.drop_index("ix_intraday_analytics_mode", table_name="intraday_session_analytics")
    op.drop_index(
        "ix_intraday_analytics_generated_at",
        table_name="intraday_session_analytics",
    )
    op.drop_index("ix_intraday_analytics_scope", table_name="intraday_session_analytics")
    op.drop_index(
        "ix_intraday_analytics_trading_instrument",
        table_name="intraday_session_analytics",
    )
    op.drop_index(
        "ix_intraday_analytics_trading_session",
        table_name="intraday_session_analytics",
    )
    op.drop_table("intraday_session_analytics")

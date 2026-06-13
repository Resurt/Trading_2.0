"""SQLAlchemy domain models for trading state, events, reports, and audit."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from trading_common.db.base import Base

JsonPayload = dict[str, object]
JSONB_TYPE = JSON().with_variant(JSONB(), "postgresql")
MONEY_TYPE = Numeric(20, 6)
PRICE_TYPE = Numeric(20, 8)
BPS_TYPE = Numeric(12, 4)


class TimestampMixin:
    """Creation/update timestamps shared by mutable aggregate tables."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=func.now(),
    )


class EventTimestampMixin:
    """Timestamps shared by domain event tables."""

    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionContextMixin:
    """Canonical session context required for analytics and replay."""

    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    session_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    micro_session_id: Mapped[str] = mapped_column(String(96), nullable=False)
    broker_trading_status: Mapped[str] = mapped_column(String(64), nullable=False)


class InstrumentRegistry(Base, TimestampMixin):
    """Tradable instrument metadata controlled by the local robot."""

    __tablename__ = "instrument_registry"

    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    class_code: Mapped[str] = mapped_column(String(16), nullable=False, default="TQBR")
    figi: Mapped[str | None] = mapped_column(String(32), unique=True)
    instrument_uid: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    min_price_increment: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_morning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_evening: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_weekend: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    instrument_payload: Mapped[JsonPayload] = mapped_column(
        JSONB_TYPE,
        nullable=False,
        default=dict,
    )


class StrategyConfig(Base, TimestampMixin):
    """Versioned strategy configuration scoped by a session template."""

    __tablename__ = "strategy_config"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "version",
            "session_template",
            name="uq_strategy_config_version_template",
        ),
        Index(
            "ix_strategy_config_active_template",
            "strategy_id",
            "session_template",
            "is_active",
        ),
    )

    strategy_config_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    session_template: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    risk_limits: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class SessionRun(Base, SessionContextMixin):
    """Logical micro-session run without restarting the trade-core container."""

    __tablename__ = "session_run"
    __table_args__ = (
        UniqueConstraint("micro_session_id", name="uq_session_run_micro_session_id"),
        Index("ix_session_run_trading_date_type", "trading_date", "session_type"),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    freeze_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason_code: Mapped[str | None] = mapped_column(String(64))
    run_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class SignalCandidate(Base, SessionContextMixin, EventTimestampMixin):
    """Potential trade before blocker/risk/execution gates finish."""

    __tablename__ = "signal_candidate"
    __table_args__ = (
        Index("ix_signal_candidate_trading_date", "trading_date", "session_type"),
        Index("ix_signal_candidate_instrument", "instrument_id", "timeframe"),
    )

    candidate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("session_run.run_id"),
    )
    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("instrument_registry.instrument_id"),
        nullable=False,
    )
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    expected_edge_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    expected_holding_minutes: Mapped[int | None] = mapped_column(Integer)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    mid_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_abs: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    market_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    book_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    candle_age_ms: Mapped[int | None] = mapped_column(Integer)
    data_freshness_ms: Mapped[int | None] = mapped_column(Integer)
    signal_fingerprint: Mapped[str | None] = mapped_column(String(128))
    signal_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class BlockerEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Causal gate outcome for blocked and allowed signal candidates."""

    __tablename__ = "blocker_event"
    __table_args__ = (
        Index("ix_blocker_event_candidate", "candidate_id"),
        Index("ix_blocker_event_reason", "trading_date", "reason_code"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    blocker_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    candidate_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("signal_candidate.candidate_id"),
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_name: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    is_final_blocker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocker_rank: Mapped[int | None] = mapped_column(Integer)
    market_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    expected_edge_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)


class OrderIntent(Base, SessionContextMixin):
    """Idempotent internal intent to place, cancel, replace, or skip an order."""

    __tablename__ = "order_intent"
    __table_args__ = (
        UniqueConstraint("request_order_id", name="uq_order_intent_request_order_id"),
        UniqueConstraint("idempotency_key", name="uq_order_intent_idempotency_key"),
        Index("ix_order_intent_lifecycle", "trading_date", "status"),
    )

    order_intent_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    candidate_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("signal_candidate.candidate_id"),
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_action: Mapped[str] = mapped_column(String(16), nullable=False, default="place")
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    lot_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    intended_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    time_in_force: Mapped[str] = mapped_column(String(32), nullable=False)
    request_order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    execution_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    cancel_reason_code: Mapped[str | None] = mapped_column(String(64))
    reject_reason_code: Mapped[str | None] = mapped_column(String(64))
    created_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    intent_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class BrokerOrder(Base, SessionContextMixin):
    """Broker-observed order lifecycle keyed by request and exchange ids."""

    __tablename__ = "broker_order"
    __table_args__ = (
        UniqueConstraint("request_order_id", name="uq_broker_order_request_order_id"),
        Index("ix_broker_order_exchange_order_id", "exchange_order_id", unique=True),
        Index("ix_broker_order_status", "trading_date", "broker_status"),
    )

    broker_order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    order_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("order_intent.order_intent_id"),
    )
    request_order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(96))
    broker_status: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason_code: Mapped[str | None] = mapped_column(String(64))
    broker_tracking_id: Mapped[str | None] = mapped_column(String(128))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    broker_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class FillEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Full or partial execution event from the broker."""

    __tablename__ = "fill_event"
    __table_args__ = (
        UniqueConstraint(
            "exchange_order_id",
            "broker_fill_id",
            "trading_date",
            name="uq_fill_event_exchange_fill",
        ),
        Index("ix_fill_event_request_order_id", "request_order_id"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    fill_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    request_order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    exchange_order_id: Mapped[str] = mapped_column(String(96), nullable=False)
    broker_fill_id: Mapped[str] = mapped_column(String(96), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    lot_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE_TYPE, nullable=False)
    commission: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    liquidity_flag: Mapped[str | None] = mapped_column(String(32))
    fill_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class RiskEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Risk engine decision or limit observation."""

    __tablename__ = "risk_event"
    __table_args__ = (Index("ix_risk_event_reason", "trading_date", "reason_code"),)

    risk_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    order_intent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    risk_rule: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    limit_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    observed_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    action_taken: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class PositionSnapshot(Base, SessionContextMixin):
    """Position state captured at micro-session boundaries and risk events."""

    __tablename__ = "position_snapshot"
    __table_args__ = (
        Index("ix_position_snapshot_instrument", "trading_date", "instrument_id"),
        UniqueConstraint(
            "micro_session_id",
            "instrument_id",
            "account_id",
            "snapshot_ts",
            name="uq_position_snapshot_context",
        ),
    )

    position_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    qty_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    market_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    realised_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    exposure: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    snapshot_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class StrategyStateEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Strategy state transition event for replay and diagnostics."""

    __tablename__ = "strategy_state_event"
    __table_args__ = (
        Index("ix_strategy_state_event_strategy", "trading_date", "strategy_id"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    strategy_state_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    previous_state: Mapped[str | None] = mapped_column(String(64))
    new_state: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    state_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class HourlyReport(Base, SessionContextMixin):
    """Aggregated report for one closed micro-session."""

    __tablename__ = "hourly_report"
    __table_args__ = (
        UniqueConstraint("micro_session_id", "strategy_id", name="uq_hourly_report_micro_strategy"),
        Index("ix_hourly_report_trading_date", "trading_date", "session_type"),
    )

    hourly_report_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("session_run.run_id"),
    )
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    realised_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    unrealised_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reject_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fill_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    report_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyReport(Base):
    """Aggregated report for a trading date, optionally filtered by session/instrument."""

    __tablename__ = "daily_report"
    __table_args__ = (
        Index("ix_daily_report_trading_date", "trading_date"),
        Index("ix_daily_report_scope", "trading_date", "strategy_id", "session_type"),
    )

    daily_report_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    session_type: Mapped[str | None] = mapped_column(String(32))
    session_phase: Mapped[str | None] = mapped_column(String(32))
    micro_session_id: Mapped[str | None] = mapped_column(String(96))
    broker_trading_status: Mapped[str | None] = mapped_column(String(64))
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    market_regime: Mapped[str] = mapped_column(String(32), nullable=False)
    realised_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fill_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    report_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CounterfactualResult(Base, SessionContextMixin):
    """Outcome analysis for blocked or cancelled candidates."""

    __tablename__ = "counterfactual_result"
    __table_args__ = (
        Index("ix_counterfactual_candidate", "candidate_id"),
        Index("ix_counterfactual_reason", "trading_date", "blocker_code", "cancel_reason_code"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    counterfactual_result_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    order_intent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    blocker_code: Mapped[str | None] = mapped_column(String(64))
    cancel_reason_code: Mapped[str | None] = mapped_column(String(64))
    fee_bps_assumed: Mapped[Decimal] = mapped_column(BPS_TYPE, nullable=False)
    slippage_bps_assumed: Mapped[Decimal] = mapped_column(BPS_TYPE, nullable=False)
    mfe_5m_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    mae_5m_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    mfe_10m_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    mae_10m_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    mfe_15m_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    mae_15m_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    would_profit_5m: Mapped[bool | None] = mapped_column(Boolean)
    would_profit_10m: Mapped[bool | None] = mapped_column(Boolean)
    would_profit_15m: Mapped[bool | None] = mapped_column(Boolean)
    result_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Structured audit event for operator and system actions."""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_entity", "entity_type", "entity_id"),
        Index("ix_audit_event_action", "trading_date", "action"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    audit_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(96), nullable=False)
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    audit_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)

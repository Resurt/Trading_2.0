"""SQLAlchemy domain models for trading state, events, reports, and audit."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="seed")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unresolved",
    )
    resolution_error_code: Mapped[str | None] = mapped_column(String(128))
    resolution_error_message: Mapped[str | None] = mapped_column(String(1024))
    broker_payload: Mapped[JsonPayload | None] = mapped_column(JSONB_TYPE)
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


class MicroSession(Base, SessionContextMixin, TimestampMixin):
    """Physical analytics row for an hourly logical micro-session."""

    __tablename__ = "micro_session"
    __table_args__ = (
        Index(
            "ix_micro_session_scope",
            "trading_date",
            "session_type",
            "instrument_id",
            "timeframe",
        ),
        Index("ix_micro_session_status", "trading_date", "status"),
    )

    micro_session_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("session_run.run_id"),
    )
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    freeze_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollover_reason_code: Mapped[str | None] = mapped_column(String(64))
    snapshot_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class SignalCandidate(Base, SessionContextMixin, EventTimestampMixin):
    """Potential trade before blocker/risk/execution gates finish."""

    __tablename__ = "signal_candidate"
    __table_args__ = (
        Index("ix_signal_candidate_trading_date", "trading_date", "session_type"),
        Index("ix_signal_candidate_instrument", "instrument_id", "timeframe"),
        Index("ux_signal_candidate_fingerprint", "signal_fingerprint", unique=True),
        Index(
            "ix_signal_candidate_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
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


class MarketContextSnapshot(Base, SessionContextMixin, EventTimestampMixin):
    """Feature snapshot used to explain candidates, blockers, and reports."""

    __tablename__ = "market_context_snapshot"
    __table_args__ = (
        Index("ix_market_context_candidate", "candidate_id"),
        Index(
            "ux_market_context_candidate_kind",
            "candidate_id",
            "snapshot_kind",
            "trading_date",
            unique=True,
        ),
        Index(
            "ix_market_context_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    market_context_snapshot_id: Mapped[UUID] = mapped_column(
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
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    mid_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    best_bid_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    best_ask_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_abs: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    bid_depth_lots: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    ask_depth_lots: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    book_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    market_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    candle_age_ms: Mapped[int | None] = mapped_column(Integer)
    data_freshness_ms: Mapped[int | None] = mapped_column(Integer)
    feature_snapshot: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    explanation_payload: Mapped[JsonPayload] = mapped_column(
        JSONB_TYPE,
        nullable=False,
        default=dict,
    )


class CandidateStageResult(Base, SessionContextMixin, EventTimestampMixin):
    """Append-only decision journal for every candidate pipeline stage."""

    __tablename__ = "candidate_stage_result"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "stage_seq",
            "trading_date",
            name="uq_candidate_stage_result_candidate_seq",
        ),
        Index("ix_candidate_stage_candidate", "candidate_id", "stage_seq"),
        Index("ix_candidate_stage_blocker", "trading_date", "blocker_code"),
        Index(
            "ix_candidate_stage_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    candidate_stage_result_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    candidate_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("signal_candidate.candidate_id"),
        nullable=False,
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    blocker_code: Mapped[str | None] = mapped_column(String(64))
    blocker_family: Mapped[str | None] = mapped_column(String(64))
    measured_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    threshold_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    explanation_payload: Mapped[JsonPayload] = mapped_column(
        JSONB_TYPE,
        nullable=False,
        default=dict,
    )


class BlockerEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Causal gate outcome for blocked and allowed signal candidates."""

    __tablename__ = "blocker_event"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "gate_rank",
            "reason_code",
            "trading_date",
            name="uq_blocker_event_candidate_gate_reason",
        ),
        Index("ix_blocker_event_candidate", "candidate_id"),
        Index("ix_blocker_event_reason", "trading_date", "reason_code"),
        Index("ix_blocker_event_blocker_code", "trading_date", "blocker_code"),
        Index(
            "ix_blocker_event_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
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
    timeframe: Mapped[str | None] = mapped_column(String(16))
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_name: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_seq: Mapped[int | None] = mapped_column(Integer)
    stage_name: Mapped[str | None] = mapped_column(String(64))
    stage_outcome: Mapped[str | None] = mapped_column(String(32))
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    blocker_code: Mapped[str | None] = mapped_column(String(64))
    blocker_family: Mapped[str | None] = mapped_column(String(64))
    measured_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    threshold_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    reason_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    explanation_payload: Mapped[JsonPayload] = mapped_column(
        JSONB_TYPE,
        nullable=False,
        default=dict,
    )
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
        Index("ix_order_intent_candidate", "candidate_id"),
        Index(
            "ix_order_intent_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
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
    timeframe: Mapped[str | None] = mapped_column(String(16))
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[int | None] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_action: Mapped[str] = mapped_column(String(16), nullable=False, default="place")
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    lot_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    intended_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    time_in_force: Mapped[str] = mapped_column(String(32), nullable=False)
    request_order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tracking_id: Mapped[str | None] = mapped_column(String(128))
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
        Index("ix_broker_order_candidate", "candidate_id"),
        Index(
            "ix_broker_order_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
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
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    request_order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(96))
    tracking_id: Mapped[str | None] = mapped_column(String(128))
    broker_status: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason_code: Mapped[str | None] = mapped_column(String(64))
    broker_tracking_id: Mapped[str | None] = mapped_column(String(128))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    broker_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class OrderStateEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Append-only broker order state transition event."""

    __tablename__ = "order_state_event"
    __table_args__ = (
        UniqueConstraint(
            "order_intent_id",
            "state_seq",
            "event_type",
            "trading_date",
            name="uq_order_state_event_intent_seq_type",
        ),
        Index("ix_order_state_candidate", "candidate_id"),
        Index("ix_order_state_intent_seq", "order_intent_id", "state_seq"),
        Index("ix_order_state_request_order_id", "request_order_id"),
        Index("ix_order_state_exchange_order_id", "exchange_order_id"),
        Index(
            "ix_order_state_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    order_state_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    order_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("order_intent.order_intent_id"),
    )
    broker_order_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    request_order_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    exchange_order_id: Mapped[str | None] = mapped_column(String(96))
    tracking_id: Mapped[str | None] = mapped_column(String(128))
    state_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(64))
    new_state: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    cancel_reason_code: Mapped[str | None] = mapped_column(String(64))
    reject_reason_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    state_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


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
        Index("ix_fill_event_candidate", "candidate_id"),
        Index(
            "ix_fill_event_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    fill_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    order_intent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    request_order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    exchange_order_id: Mapped[str] = mapped_column(String(96), nullable=False)
    tracking_id: Mapped[str | None] = mapped_column(String(128))
    broker_fill_id: Mapped[str] = mapped_column(String(96), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    lot_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE_TYPE, nullable=False)
    commission: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    commission_gross: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission_net: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    slippage_bp: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    pnl_gross: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    pnl_net: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    liquidity_flag: Mapped[str | None] = mapped_column(String(32))
    fill_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class RiskEvent(Base, SessionContextMixin, EventTimestampMixin):
    """Risk engine decision or limit observation."""

    __tablename__ = "risk_event"
    __table_args__ = (
        Index("ix_risk_event_reason", "trading_date", "reason_code"),
        Index("ix_risk_event_candidate", "candidate_id"),
        Index(
            "ix_risk_event_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
    )

    risk_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    order_intent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str | None] = mapped_column(String(16))
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


class MarketCandle(Base, SessionContextMixin):
    """Closed or explicitly marked forming market candle for analytics and replay."""

    __tablename__ = "market_candle"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "timeframe",
            "open_ts_utc",
            "trading_date",
            name="uq_market_candle_bucket",
        ),
        Index("ix_market_candle_lookup", "instrument_id", "timeframe", "open_ts_utc"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    market_candle_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    open_ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close_ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange_open_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange_close_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open_price: Mapped[Decimal] = mapped_column(PRICE_TYPE, nullable=False)
    high_price: Mapped[Decimal] = mapped_column(PRICE_TYPE, nullable=False)
    low_price: Mapped[Decimal] = mapped_column(PRICE_TYPE, nullable=False)
    close_price: Mapped[Decimal] = mapped_column(PRICE_TYPE, nullable=False)
    volume_lots: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    candle_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class MarketStatusSnapshot(Base, SessionContextMixin, EventTimestampMixin):
    """Broker market status observation normalized for analytics."""

    __tablename__ = "market_status_snapshot"
    __table_args__ = (
        Index("ix_market_status_instrument_ts", "instrument_id", "ts_utc"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    market_status_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_status: Mapped[str] = mapped_column(String(64), nullable=False)
    api_trade_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class OrderBookSummary(Base, SessionContextMixin, EventTimestampMixin):
    """Lightweight order book aggregate; full tick-level book is not stored."""

    __tablename__ = "order_book_summary"
    __table_args__ = (
        Index("ix_order_book_summary_instrument_ts", "instrument_id", "ts_utc"),
        {"postgresql_partition_by": "RANGE (trading_date)"},
    )

    order_book_summary_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    depth_levels: Mapped[int] = mapped_column(Integer, nullable=False)
    best_bid_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    best_bid_qty_lots: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    best_ask_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    best_ask_qty_lots: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    mid_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_abs: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    bid_depth_lots: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    ask_depth_lots: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    book_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    market_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    summary_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class MarketMicrostructureSnapshot(Base, SessionContextMixin):
    """Live market microstructure read model for data-only shadow collection."""

    __tablename__ = "market_microstructure_snapshot"
    __table_args__ = (
        Index("ix_market_microstructure_instrument_ts", "instrument_id", "ts_utc"),
        Index("ix_market_microstructure_date_instrument", "trading_date", "instrument_id"),
        Index("ix_market_microstructure_session_date", "session_type", "trading_date"),
        Index("ix_market_microstructure_spread_bps", "spread_bps"),
        Index("ix_market_microstructure_quality", "market_quality_score"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    best_bid: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    best_ask: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    mid_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_abs: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    bid_depth_lots: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    ask_depth_lots: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    book_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    market_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    feed_freshness_age_ms: Mapped[int | None] = mapped_column(Integer)
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
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
        Index("ix_hourly_report_scope", "instrument_id", "timeframe", "trading_date"),
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
    timeframe: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    realised_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    unrealised_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission_gross: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission_net: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    slippage_bp: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    pnl_gross: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    pnl_net: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
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
        Index("ix_daily_report_instrument_timeframe", "instrument_id", "timeframe", "trading_date"),
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
    timeframe: Mapped[str | None] = mapped_column(String(16))
    market_regime: Mapped[str] = mapped_column(String(32), nullable=False)
    realised_pnl: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission_gross: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    commission_net: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    slippage_bp: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    pnl_gross: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    pnl_net: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fill_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    report_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HistoricalDataQualityReport(Base):
    """Persisted quality summary for DB-backed historical candle data."""

    __tablename__ = "historical_data_quality_report"
    __table_args__ = (
        Index("ix_historical_quality_generated_at", "generated_at"),
        Index("ix_historical_quality_period", "from_date", "to_date"),
        Index("ix_historical_quality_coverage", "coverage_pct"),
    )

    report_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    instruments: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    timeframes: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    coverage_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    expected_candles: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_candles: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_intervals: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    invalid_ohlc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    abnormal_gap_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class CalibrationReport(Base):
    """Persisted calibration aggregate and threshold recommendations."""

    __tablename__ = "calibration_report"
    __table_args__ = (
        Index("ix_calibration_report_generated_at", "generated_at"),
        Index("ix_calibration_report_strategy", "strategy_id"),
        Index("ix_calibration_report_period", "from_date", "to_date"),
    )

    calibration_report_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instruments: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    timeframes: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    group_by: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    report_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class IntradaySessionAnalytics(Base):
    """Session/hour/instrument diagnostic snapshot for the current trading day."""

    __tablename__ = "intraday_session_analytics"
    __table_args__ = (
        Index("ix_intraday_analytics_trading_session", "trading_date", "session_type"),
        Index("ix_intraday_analytics_trading_instrument", "trading_date", "instrument_id"),
        Index(
            "ix_intraday_analytics_scope",
            "trading_date",
            "session_type",
            "instrument_id",
            "timeframe",
            "side",
        ),
        Index("ix_intraday_analytics_generated_at", "generated_at"),
        Index("ix_intraday_analytics_mode", "mode"),
    )

    intraday_analytics_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    session_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    micro_session_id: Mapped[str | None] = mapped_column(String(96))
    hour_bucket: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    side: Mapped[str | None] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    market_bias: Mapped[str] = mapped_column(String(32), nullable=False)
    market_activity: Mapped[str] = mapped_column(String(32), nullable=False)
    trend_strength: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pseudo_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    real_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    near_miss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    p95_spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    avg_depth: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    avg_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    avg_market_quality: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    stale_incidents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candle_lag_p95_seconds: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    gross_pnl_proxy: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    net_pnl_proxy: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    analytics_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class RollingPerformanceCube(Base):
    """Rolling contour statistics by instrument/session/timeframe/side/mode."""

    __tablename__ = "rolling_performance_cube"
    __table_args__ = (
        Index(
            "ix_rolling_cube_window_scope",
            "window_name",
            "instrument_id",
            "timeframe",
            "side",
        ),
        Index("ix_rolling_cube_generated_at", "generated_at"),
        Index("ix_rolling_cube_contour_status", "contour_status"),
        Index("ix_rolling_cube_mode", "mode"),
        Index(
            "ix_rolling_cube_full_scope",
            "instrument_id",
            "session_type",
            "timeframe",
            "side",
        ),
    )

    cube_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_name: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pseudo_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    real_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gross_pnl_proxy: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False, default=0)
    net_pnl_proxy: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False, default=0)
    avg_net_pnl_proxy: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False, default=0)
    win_proxy: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    avg_spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    p95_spread_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    avg_depth: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    p95_depth: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    avg_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    avg_market_quality: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    stale_incidents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stream_gap_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sample_warning: Mapped[str | None] = mapped_column(String(256))
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    contour_status: Mapped[str] = mapped_column(String(32), nullable=False)
    cube_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class CalibrationDiagnosticRun(Base):
    """Persisted Calibration Center diagnostic run."""

    __tablename__ = "calibration_diagnostic_run"
    __table_args__ = (
        Index("ix_calibration_diagnostic_created_at", "created_at"),
        Index("ix_calibration_diagnostic_status", "status"),
        Index("ix_calibration_diagnostic_trigger", "trigger_type"),
        Index("ix_calibration_diagnostic_diagnosis", "diagnosis"),
    )

    diagnostic_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[str | None] = mapped_column(String(128))
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    from_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    to_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    universe: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    diagnosis: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    blocking_issues: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    warnings: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    diagnostic_payload: Mapped[JsonPayload] = mapped_column(
        JSONB_TYPE,
        nullable=False,
        default=dict,
    )


class StrategyConfigCandidate(Base):
    """Draft/proposal storage for future strategy config changes."""

    __tablename__ = "strategy_config_candidate"
    __table_args__ = (
        Index("ix_strategy_config_candidate_created_at", "created_at"),
        Index("ix_strategy_config_candidate_status", "status"),
        Index("ix_strategy_config_candidate_base", "base_strategy_id"),
        Index("ix_strategy_config_candidate_source_run", "source_diagnostic_run_id"),
    )

    candidate_config_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_diagnostic_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("calibration_diagnostic_run.diagnostic_run_id"),
    )
    base_strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    proposed_by: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proposal_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    validation_payload: Mapped[JsonPayload] = mapped_column(
        JSONB_TYPE,
        nullable=False,
        default=dict,
    )
    caveats: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    rejection_reason: Mapped[str | None] = mapped_column(String(1024))


class MarketRegimeSnapshot(Base):
    """Market regime/drift diagnostic snapshot."""

    __tablename__ = "market_regime_snapshot"
    __table_args__ = (
        Index("ix_market_regime_generated_at", "generated_at"),
        Index("ix_market_regime_window", "window_start", "window_end"),
        Index("ix_market_regime_instrument_session", "instrument_id", "session_type"),
        Index("ix_market_regime_label", "market_regime"),
    )

    regime_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(String(64))
    session_type: Mapped[str | None] = mapped_column(String(32))
    market_regime: Mapped[str] = mapped_column(String(32), nullable=False)
    volume_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    volatility_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    spread_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    depth_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    imbalance_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    candidate_frequency_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    regime_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class CorporateActionEvent(Base):
    """Manual/API imported corporate action fact used by calibration filters."""

    __tablename__ = "corporate_action_event"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "action_type",
            "ex_date",
            "amount_per_share",
            "source",
            name="uq_corporate_action_identity",
        ),
        Index("ix_corporate_action_instrument_ex_date", "instrument_id", "ex_date"),
        Index("ix_corporate_action_ticker_ex_date", "ticker", "ex_date"),
        Index("ix_corporate_action_type_ex_date", "action_type", "ex_date"),
        Index("ix_corporate_action_source", "source"),
    )

    corporate_action_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16))
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    declared_date: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    registry_close_date: Mapped[date | None] = mapped_column(Date)
    payment_date: Mapped[date | None] = mapped_column(Date)
    amount_per_share: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    currency: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    action_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=func.now(),
    )


class DividendSyncRun(Base):
    """Read model for the latest T-Bank dividend synchronization outcome."""

    __tablename__ = "dividend_sync_run"
    __table_args__ = (
        Index("ix_dividend_sync_run_finished_at", "finished_at"),
        Index("ix_dividend_sync_run_status", "status"),
        Index("ix_dividend_sync_run_clean", "clean"),
    )

    dividend_sync_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    clean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    instruments: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    instruments_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_instruments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_instruments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dividends_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dividends_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dividends_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    existing_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    special_days_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    future_risk_windows_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class MarketSpecialDay(Base):
    """Instrument/date flag for dividend gaps, corporate actions, and excluded days."""

    __tablename__ = "market_special_day"
    __table_args__ = (
        UniqueConstraint(
            "trading_date",
            "instrument_id",
            "special_day_type",
            "reason_code",
            name="uq_market_special_day_identity",
        ),
        Index("ix_market_special_day_trading_instrument", "trading_date", "instrument_id"),
        Index("ix_market_special_day_type_date", "special_day_type", "trading_date"),
        Index("ix_market_special_day_excluded", "exclude_from_primary_calibration"),
        Index("ix_market_special_day_corporate_action", "linked_corporate_action_id"),
    )

    special_day_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16))
    special_day_type: Mapped[str] = mapped_column(String(64), nullable=False)
    session_type: Mapped[str | None] = mapped_column(String(32))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    linked_corporate_action_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("corporate_action_event.corporate_action_id"),
    )
    open_gap_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    previous_close: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    session_open_price: Mapped[Decimal | None] = mapped_column(PRICE_TYPE)
    expected_dividend_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    detected_gap_bps: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    exclude_from_primary_calibration: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    trade_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="shadow_only")
    special_day_payload: Mapped[JsonPayload] = mapped_column(
        JSONB_TYPE,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReportJobOutbox(Base, TimestampMixin):
    """Transactional outbox row for report-worker Celery jobs."""

    __tablename__ = "report_job_outbox"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_report_job_outbox_idempotency_key",
        ),
        Index("ix_report_job_outbox_status", "status", "next_retry_at"),
        Index("ix_report_job_outbox_micro_strategy", "micro_session_id", "strategy_id"),
        Index("ix_report_job_outbox_celery_task_id", "celery_task_id", unique=True),
    )

    report_job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(128))
    task_name: Mapped[str] = mapped_column(String(128), nullable=False)
    report_type: Mapped[str] = mapped_column(String(32), nullable=False)
    micro_session_id: Mapped[str | None] = mapped_column(String(96))
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(String(2048))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    job_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    result_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class RobotCommand(Base, TimestampMixin):
    """Persistent operator command consumed by the long-lived trade-core runtime."""

    __tablename__ = "robot_command"
    __table_args__ = (
        CheckConstraint(
            "command_type in ('start', 'stop', 'pause', 'resume', 'emergency_stop')",
            name="ck_robot_command_type",
        ),
        CheckConstraint(
            "status in ('requested', 'accepted', 'applied', 'rejected', 'failed')",
            name="ck_robot_command_status",
        ),
        Index("ix_robot_command_status_requested", "status", "requested_at"),
        Index("ix_robot_command_type_requested", "command_type", "requested_at"),
    )

    command_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(96), nullable=False)
    requested_role: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="requested")
    reason_code: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)
    result_payload: Mapped[JsonPayload] = mapped_column(JSONB_TYPE, nullable=False, default=dict)


class CounterfactualResult(Base, SessionContextMixin):
    """Outcome analysis for blocked or cancelled candidates."""

    __tablename__ = "counterfactual_result"
    __table_args__ = (
        Index("ix_counterfactual_candidate", "candidate_id"),
        Index("ix_counterfactual_reason", "trading_date", "blocker_code", "cancel_reason_code"),
        Index(
            "ix_counterfactual_scope",
            "instrument_id",
            "timeframe",
            "trading_date",
            "session_type",
        ),
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
    timeframe: Mapped[str | None] = mapped_column(String(16))
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    blocker_code: Mapped[str | None] = mapped_column(String(64))
    cancel_reason_code: Mapped[str | None] = mapped_column(String(64))
    fee_bps_assumed: Mapped[Decimal] = mapped_column(BPS_TYPE, nullable=False)
    slippage_bps_assumed: Mapped[Decimal] = mapped_column(BPS_TYPE, nullable=False)
    slippage_bp: Mapped[Decimal | None] = mapped_column(BPS_TYPE)
    pnl_gross: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
    pnl_net: Mapped[Decimal | None] = mapped_column(MONEY_TYPE)
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

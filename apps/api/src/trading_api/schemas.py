"""Pydantic schemas for FastAPI BFF contracts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

JsonPayload = dict[str, Any]


class ApiRole(StrEnum):
    OBSERVER = "observer"
    OPERATOR = "operator"
    ADMIN = "admin"


class RobotCommand(StrEnum):
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    EMERGENCY_STOP = "emergency_stop"


class ReportScope(StrEnum):
    HOURLY = "hourly"
    DAILY = "daily"


class RobotCommandResponse(BaseModel):
    accepted: bool
    command_id: UUID | None = None
    command: RobotCommand
    requested_by_role: ApiRole
    requested_by: str = "unknown"
    requested_at: datetime | None = None
    status: str
    reason_code: str | None = None
    payload: JsonPayload = Field(default_factory=dict)
    message: str


class AuthStatusResponse(BaseModel):
    auth_mode: str
    role: ApiRole
    subject: str
    production_like: bool


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_at: datetime
    auth_mode: str


class MoneyBalance(BaseModel):
    currency: str = "RUB"
    available: Decimal = Decimal("0")
    blocked: Decimal = Decimal("0")


class SessionSnapshotResponse(BaseModel):
    calendar_date: date | None = None
    trading_date: date | None = None
    session_type: str = "unknown"
    session_phase: str = "closed"
    micro_session_id: str | None = None
    broker_trading_status: str = "unknown"
    observed_at: datetime | None = None


class RobotStatusResponse(BaseModel):
    balance: MoneyBalance
    active_instruments: list[str]
    active_timeframes: list[str]
    strategy_state: str
    session_type: str
    session_phase: str
    broker_trading_status: str
    open_orders_count: int
    active_positions_count: int
    degraded_flags: list[str]
    robot_control_state: str
    micro_session_id: str | None = None


class PositionResponse(BaseModel):
    instrument_id: str
    account_id: str
    position_side: str
    qty_lots: int
    avg_price: Decimal | None = None
    market_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realised_pnl: Decimal | None = None
    snapshot_ts: datetime


class OrderResponse(BaseModel):
    order_intent_id: UUID | None = None
    request_order_id: UUID
    exchange_order_id: str | None = None
    instrument_id: str | None = None
    side: str | None = None
    order_type: str | None = None
    lot_qty: int | None = None
    intended_price: Decimal | None = None
    broker_status: str
    cancel_reason_code: str | None = None
    reject_reason_code: str | None = None
    last_observed_at: datetime | None = None


class SignalResponse(BaseModel):
    candidate_id: UUID
    instrument_id: str
    strategy_id: str
    timeframe: str
    side: str
    signal_type: str
    candidate_status: str
    expected_edge_bps: Decimal | None = None
    expected_holding_minutes: int | None = None
    final_blocker_code: str | None = None
    payload: JsonPayload = Field(default_factory=dict)


class MarketInstrumentOverview(BaseModel):
    instrument_id: str
    spread: Decimal | None = None
    mid_price: Decimal | None = None
    market_quality: Decimal | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    recent_market_trades: list[JsonPayload] = Field(default_factory=list)
    order_book_summary: JsonPayload = Field(default_factory=dict)


class MarketOverviewResponse(BaseModel):
    generated_at: datetime
    instruments: list[MarketInstrumentOverview]


class HourlyReportResponse(BaseModel):
    hourly_report_id: UUID
    trading_date: date
    session_type: str
    micro_session_id: str
    strategy_id: str
    instrument_id: str | None = None
    timeframe: str | None = None
    realised_pnl: Decimal | None = None
    commission: Decimal | None = None
    signal_count: int
    blocked_count: int
    fill_ratio: Decimal | None = None
    payload: JsonPayload = Field(default_factory=dict)


class DailyReportResponse(BaseModel):
    daily_report_id: UUID
    trading_date: date
    strategy_id: str
    market_regime: str
    session_type: str | None = None
    instrument_id: str | None = None
    timeframe: str | None = None
    realised_pnl: Decimal | None = None
    commission: Decimal | None = None
    signal_count: int
    blocked_count: int
    fill_ratio: Decimal | None = None
    payload: JsonPayload = Field(default_factory=dict)


class CounterfactualResponse(BaseModel):
    counterfactual_result_id: UUID
    trading_date: date
    candidate_id: UUID | None = None
    order_intent_id: UUID | None = None
    source_event_type: str
    instrument_id: str
    timeframe: str | None = None
    strategy_id: str
    blocker_code: str | None = None
    cancel_reason_code: str | None = None
    pnl_gross: Decimal | None = None
    pnl_net: Decimal | None = None
    slippage_bp: Decimal | None = None
    mfe_5m_bps: Decimal | None = None
    mae_5m_bps: Decimal | None = None
    mfe_10m_bps: Decimal | None = None
    mae_10m_bps: Decimal | None = None
    mfe_15m_bps: Decimal | None = None
    mae_15m_bps: Decimal | None = None
    would_profit_5m: bool | None = None
    would_profit_10m: bool | None = None
    would_profit_15m: bool | None = None
    payload: JsonPayload = Field(default_factory=dict)


class DailyReportRunRequest(BaseModel):
    trading_date: date
    strategy_id: str
    include_counterfactual: bool = True


class ReportRebuildRequest(BaseModel):
    scope: ReportScope = ReportScope.DAILY
    trading_date: date
    strategy_id: str
    micro_session_id: str | None = None
    instrument_id: str | None = None
    timeframe: str | None = None
    session_type: str | None = None
    strategy_version: int | None = None
    include_counterfactual: bool = True
    force_rebuild: bool = True


class ReportJobResponse(BaseModel):
    job_id: str
    task_name: str
    status: str
    payload: JsonPayload = Field(default_factory=dict)


class ReportJobStatusResponse(BaseModel):
    job_id: str
    task_name: str
    status: str
    ready: bool
    successful: bool
    failed: bool
    result: JsonPayload | None = None
    error: str | None = None
    payload: JsonPayload = Field(default_factory=dict)


class BlockerAnalyticsRow(BaseModel):
    blocker_code: str
    blocker_family: str | None = None
    count: int
    terminal_count: int
    candidate_count: int
    measured_value_avg: Decimal | None = None
    threshold_value_avg: Decimal | None = None
    missed_pnl_gross: Decimal | None = None
    missed_pnl_net: Decimal | None = None
    avoided_loss: Decimal | None = None
    false_positive_rate: Decimal | None = None
    explanation_payload: JsonPayload = Field(default_factory=dict)


class BlockerAnalyticsResponse(BaseModel):
    generated_at: datetime
    filters: JsonPayload = Field(default_factory=dict)
    rows: list[BlockerAnalyticsRow]


class CandidateFunnelStage(BaseModel):
    stage_name: str
    count: int
    percentage_of_created: Decimal | None = None
    payload: JsonPayload = Field(default_factory=dict)


class CandidateFunnelResponse(BaseModel):
    generated_at: datetime
    filters: JsonPayload = Field(default_factory=dict)
    stages: list[CandidateFunnelStage]
    totals: JsonPayload = Field(default_factory=dict)


class CanceledOrderDiagnosticsRow(BaseModel):
    cancel_reason_code: str
    count: int
    missed_pnl_gross: Decimal | None = None
    missed_pnl_net: Decimal | None = None
    avoided_loss: Decimal | None = None
    would_profit_5m_count: int = 0
    would_profit_10m_count: int = 0
    would_profit_15m_count: int = 0
    explanation_payload: JsonPayload = Field(default_factory=dict)


class CanceledOrderDiagnosticsResponse(BaseModel):
    generated_at: datetime
    filters: JsonPayload = Field(default_factory=dict)
    rows: list[CanceledOrderDiagnosticsRow]


class StrategyConfigResponse(BaseModel):
    strategy_config_id: UUID | None = None
    strategy_id: str
    version: int
    session_template: str
    is_active: bool = True
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    config_payload: JsonPayload = Field(default_factory=dict)
    risk_limits: JsonPayload = Field(default_factory=dict)


class StrategyConfigUpdateRequest(BaseModel):
    strategy_id: str
    session_template: str
    config_payload: JsonPayload = Field(default_factory=dict)
    risk_limits: JsonPayload = Field(default_factory=dict)
    actor: str = "operator"


class WebSocketEnvelope(BaseModel):
    message_id: UUID
    ts_utc: datetime
    type: str
    run_id: UUID | None = None
    micro_session_id: str | None = None
    payload: JsonPayload = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    detail: str


class OpenApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

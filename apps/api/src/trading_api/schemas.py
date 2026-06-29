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
    command_type: str | None = None
    requested_by_role: ApiRole
    requested_by: str = "unknown"
    requested_at: datetime | None = None
    status: str
    reason_code: str | None = None
    payload: JsonPayload = Field(default_factory=dict)
    preflight_result: JsonPayload | None = None
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
    total_portfolio_value_rub: Decimal | None = None
    available_cash_rub: Decimal | None = None
    blocked_cash_rub: Decimal | None = None
    expected_yield_rub: Decimal | None = None
    free_collateral_rub: Decimal | None = None
    account_id_masked: str | None = None
    account_type: str | None = None
    account_status: str | None = None
    balance_currency: str = "RUB"
    last_balance_refresh_at: datetime | None = None
    balance_freshness_seconds: int | None = None
    balance_degraded: bool = True
    balance_degraded_reason_code: str | None = "broker_balance_unavailable"


class SessionSnapshotResponse(BaseModel):
    calendar_date: date | None = None
    trading_date: date | None = None
    session_type: str = "unknown"
    session_phase: str = "closed"
    micro_session_id: str | None = None
    broker_trading_status: str = "unknown"
    observed_at: datetime | None = None
    source: str = "runtime_session_snapshot"
    stale: bool = False
    stale_reason: str | None = None


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
    data_shadow_collector_state: str | None = None
    daily_collection_active: bool = False
    effective_logging_state: str = "stopped"
    micro_session_id: str | None = None
    session_source: str = "runtime_session_snapshot"
    session_stale: bool = False
    session_stale_reason: str | None = None


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


class PortfolioSummaryResponse(BaseModel):
    balance: MoneyBalance
    positions_count: int
    source: str


class PortfolioRefreshRequest(BaseModel):
    account_id: str | None = None


class SessionPreflightResponse(BaseModel):
    market_open: bool
    market_closed_expected: bool
    now_msk: datetime
    trading_date: date
    calendar_date: date
    session_type: str
    session_phase: str
    broker_trading_status: str
    api_trade_available: bool
    official_exchange_open: bool = False
    official_exchange_closed: bool = False
    official_exchange_reason_code: str | None = None
    official_exchange_source: str | None = None
    broker_stream_available: bool = False
    broker_otc_or_indicative_available: bool = False
    api_trade_available_raw: bool = False
    api_trade_available_for_exchange: bool = False
    quote_source_allowed_for_data_collection: bool = False
    data_only_collection_allowed: bool = False
    streams_for_display_allowed: bool = False
    streams_for_calibration_allowed: bool = False
    venue_type: str = "unknown"
    trading_mode: str = "unknown"
    broker_availability_ignored_because_official_exchange_closed: bool = False
    next_session_at: datetime | None = None
    next_session_type: str | None = None
    current_window_start_at: datetime | None = None
    current_window_end_at: datetime | None = None
    reason_code: str
    source: str
    schedule_source: str = "unknown"
    status_source: str = "unknown"
    schedule_error_code: str | None = None
    schedule_error_message: str | None = None
    status_error_count: int = 0
    status_success_count: int = 0
    fallback_used: bool = False
    market_window_open: bool = False
    trading_allowed: bool = False
    blocking_layer: str | None = None
    broker_schedule_windows_count: int | None = None
    fallback_reason: str | None = None
    market_data_probe_success_count: int = 0
    market_data_probe_error_count: int = 0
    market_data_probe: JsonPayload = Field(default_factory=dict)
    cache_hit: bool = False
    cache_key: str | None = None
    requested_instruments: list[str] = Field(default_factory=list)
    working_instruments: list[str] = Field(default_factory=list)
    blocked_instruments: list[JsonPayload] = Field(default_factory=list)
    instruments_checked: list[str]
    per_instrument_status: JsonPayload = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


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
    ticker: str | None = None
    class_code: str | None = None
    board: str | None = None
    exchange: str = "MOEX"
    venue_type: str = "unknown"
    trading_mode: str = "unknown"
    official_exchange_open: bool = False
    official_exchange_closed: bool = False
    quote_source: str = "unavailable"
    quote_allowed_for_data_collection: bool = False
    quote_allowed_for_display: bool = False
    last_price: Decimal | None = None
    last_price_at: datetime | None = None
    last_price_ts: datetime | None = None
    last_price_source: str | None = None
    is_price_stale: bool = True
    price_staleness_seconds: int | None = None
    previous_close: Decimal | None = None
    change_abs: Decimal | None = None
    change_bps: Decimal | None = None
    session_type: str | None = None
    broker_trading_status: str | None = None
    api_trade_available: bool | None = None
    quote_status: str = "unavailable"
    last_candle_timeframe: str | None = None
    spread: Decimal | None = None
    spread_abs: Decimal | None = None
    spread_bps: Decimal | None = None
    spread_abs_rub: Decimal | None = None
    spread_units_validated: bool = True
    mid_price: Decimal | None = None
    market_quality: Decimal | None = None
    market_quality_score: Decimal | None = None
    display_market_quality_score: Decimal | None = None
    calibration_market_quality_score: Decimal | None = None
    market_quality_label: str = "unknown"
    market_quality_components: JsonPayload = Field(default_factory=dict)
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    bid_depth_lots: Decimal | None = None
    ask_depth_lots: Decimal | None = None
    book_imbalance: Decimal | None = None
    order_book_source: str | None = None
    order_book_ts: datetime | None = None
    order_book_age_ms: int | None = None
    order_book_stale: bool = True
    recent_market_trades: list[JsonPayload] = Field(default_factory=list)
    market_trades_source: str | None = None
    market_trades_age_ms: int | None = None
    reason_code: str | None = None
    warning: str | None = None
    order_book_summary: JsonPayload = Field(default_factory=dict)
    quote_payload: JsonPayload = Field(default_factory=dict)


class MarketOverviewResponse(BaseModel):
    generated_at: datetime
    instruments: list[MarketInstrumentOverview]


class MarketMicrostructureSnapshotResponse(BaseModel):
    snapshot_id: UUID
    ts_utc: datetime
    exchange_ts: datetime | None = None
    received_ts: datetime
    instrument_id: str
    session_type: str
    session_phase: str
    micro_session_id: str
    broker_trading_status: str
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid_price: Decimal | None = None
    spread_abs: Decimal | None = None
    spread_bps: Decimal | None = None
    bid_depth_lots: Decimal | None = None
    ask_depth_lots: Decimal | None = None
    book_imbalance: Decimal | None = None
    market_quality_score: Decimal | None = None
    feed_freshness_age_ms: int | None = None
    is_stale: bool
    source: str
    payload: JsonPayload = Field(default_factory=dict)


class MarketMicrostructureSummaryResponse(BaseModel):
    generated_at: datetime
    lookback_minutes: int
    instrument_id: str | None = None
    snapshots_count: int
    avg_spread_bps: Decimal | None = None
    p95_spread_bps: Decimal | None = None
    avg_bid_depth_lots: Decimal | None = None
    avg_ask_depth_lots: Decimal | None = None
    avg_book_imbalance: Decimal | None = None
    avg_market_quality_score: Decimal | None = None
    stale_incidents: int
    latest_ts_utc: datetime | None = None
    sessions: JsonPayload = Field(default_factory=dict)


class DataShadowStatusResponse(BaseModel):
    enabled: bool
    collector_state: str = "stopped"
    day_collection_state: str = "inactive"
    daily_collection_active: bool = False
    current_window_state: str = "stopped"
    next_collection_window_at: datetime | None = None
    remaining_windows_today: int = 0
    collector_left_running: bool = False
    paused_at: datetime | None = None
    completed_for_day_at: datetime | None = None
    last_stop_reason: str | None = None
    last_pause_reason: str | None = None
    last_resume_at: datetime | None = None
    last_window_completed_at: datetime | None = None
    strategy_trading_disabled: bool
    real_orders_disabled: bool
    market_open: bool | None = None
    market_closed_expected: bool | None = None
    reason_code: str | None = None
    next_session_at: datetime | None = None
    stream_alive: bool
    last_message_age_seconds: Decimal | None = None
    candles_received: int | None = None
    order_book_snapshots: int
    market_microstructure_snapshots: int
    avg_spread_bps: Decimal | None = None
    p95_spread_bps: Decimal | None = None
    avg_market_quality_score: Decimal | None = None
    current_session: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_command_id: UUID | None = None
    last_command_status: str | None = None
    last_command_reason_code: str | None = None
    instruments: list[str] = Field(default_factory=list)
    stream_batches: list[JsonPayload] = Field(default_factory=list)
    supervisor_enabled: bool = False
    supervisor_state: str = "not_configured"
    stream_restart_count: int = 0
    last_restart_at: datetime | None = None
    last_restart_reason: str | None = None
    stream_stale_count: int = 0
    last_stream_error: str | None = None
    per_stream_status: JsonPayload = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    warning: str | None = None


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


class IntradayAnalyticsSnapshotResponse(BaseModel):
    generated_at: datetime
    trading_date: date | None = None
    session_summaries: list[JsonPayload] = Field(default_factory=list)
    instrument_summaries: list[JsonPayload] = Field(default_factory=list)
    timeframe_summaries: list[JsonPayload] = Field(default_factory=list)
    side_summaries: list[JsonPayload] = Field(default_factory=list)
    market_bias: str = "unknown"
    market_activity: str = "unknown"
    near_miss_count: int = 0
    spread_depth_imbalance_summary: JsonPayload = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    rows: list[JsonPayload] = Field(default_factory=list)


class CalibrationObservatoryRunRequest(BaseModel):
    universe: str = "SBER,GAZP"
    lookback_days: int = Field(default=20, ge=1, le=3660)
    windows: str = "7d,20d,60d,90d,180d,365d"
    mode: str = "all"
    trigger_type: str = "manual"
    create_candidate_config: bool = False
    requested_by: str | None = None


class CalibrationObservatoryRunResponse(BaseModel):
    diagnostic_run_id: UUID
    diagnosis: str
    confidence: str
    rolling_cube_rows: int
    regime_summary: JsonPayload = Field(default_factory=dict)
    top_contours: list[JsonPayload] = Field(default_factory=list)
    dead_contours: list[JsonPayload] = Field(default_factory=list)
    calibration_recommended: bool
    candidate_config_id: UUID | None = None
    warnings: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    payload: JsonPayload = Field(default_factory=dict)


class CalibrationDiagnosticRunResponse(BaseModel):
    diagnostic_run_id: UUID
    created_at: datetime
    completed_at: datetime | None = None
    requested_by: str | None = None
    trigger_type: str
    status: str
    from_ts: datetime
    to_ts: datetime
    universe: JsonPayload = Field(default_factory=dict)
    diagnosis: str
    confidence: str
    blocking_issues: JsonPayload = Field(default_factory=dict)
    warnings: JsonPayload = Field(default_factory=dict)
    diagnostic_payload: JsonPayload = Field(default_factory=dict)


class RollingPerformanceCubeResponse(BaseModel):
    cube_id: UUID
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    window_name: str
    instrument_id: str
    session_type: str
    timeframe: str
    side: str
    mode: str
    candidate_count: int
    approved_count: int
    blocked_count: int
    pseudo_order_count: int
    real_order_count: int
    gross_pnl_proxy: Decimal
    net_pnl_proxy: Decimal
    avg_net_pnl_proxy: Decimal
    win_proxy: Decimal | None = None
    avg_spread_bps: Decimal | None = None
    p95_spread_bps: Decimal | None = None
    avg_depth: Decimal | None = None
    p95_depth: Decimal | None = None
    avg_imbalance: Decimal | None = None
    avg_market_quality: Decimal | None = None
    stale_incidents: int
    stream_gap_count: int
    active_days: int
    last_signal_at: datetime | None = None
    sample_warning: str | None = None
    confidence: str
    contour_status: str
    cube_payload: JsonPayload = Field(default_factory=dict)


class MarketRegimeSnapshotResponse(BaseModel):
    regime_snapshot_id: UUID
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    instrument_id: str | None = None
    session_type: str | None = None
    market_regime: str
    volume_score: Decimal | None = None
    volatility_score: Decimal | None = None
    spread_score: Decimal | None = None
    depth_score: Decimal | None = None
    imbalance_score: Decimal | None = None
    candidate_frequency_score: Decimal | None = None
    regime_payload: JsonPayload = Field(default_factory=dict)


class StrategyConfigCandidateResponse(BaseModel):
    candidate_config_id: UUID
    created_at: datetime
    source_diagnostic_run_id: UUID | None = None
    base_strategy_id: str
    proposed_strategy_id: str
    status: str
    proposed_by: str
    approval_required: bool
    approved_by: str | None = None
    approved_at: datetime | None = None
    proposal_payload: JsonPayload = Field(default_factory=dict)
    validation_payload: JsonPayload = Field(default_factory=dict)
    caveats: JsonPayload = Field(default_factory=dict)
    rejection_reason: str | None = None


class StrategyConfigCandidateRejectRequest(BaseModel):
    reason: str = "operator_rejected"


class CalibrationObservatoryStatusResponse(BaseModel):
    generated_at: datetime
    latest_diagnostic: CalibrationDiagnosticRunResponse | None = None
    latest_cube_generated_at: datetime | None = None
    latest_regime_generated_at: datetime | None = None
    open_candidate_configs: int = 0
    caveat: str = "Candidate configs are not applied to live trading automatically."


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

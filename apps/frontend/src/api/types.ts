export type JsonPayload = Record<string, unknown>;

export type ConnectionState = "idle" | "loading" | "live" | "snapshot_closed" | "degraded";

export interface MoneyBalance {
  currency: string;
  available: string;
  blocked: string;
}

export interface RobotStatusResponse {
  balance: MoneyBalance;
  active_instruments: string[];
  active_timeframes: string[];
  strategy_state: string;
  session_type: string;
  session_phase: string;
  broker_trading_status: string;
  open_orders_count: number;
  active_positions_count: number;
  degraded_flags: string[];
  robot_control_state: string;
  micro_session_id: string | null;
}

export interface SessionSnapshotResponse {
  calendar_date: string | null;
  trading_date: string | null;
  session_type: string;
  session_phase: string;
  micro_session_id: string | null;
  broker_trading_status: string;
  observed_at: string | null;
}

export interface PositionResponse {
  instrument_id: string;
  account_id: string;
  position_side: string;
  qty_lots: number;
  avg_price: string | null;
  market_price: string | null;
  unrealized_pnl: string | null;
  realised_pnl: string | null;
  snapshot_ts: string;
}

export interface OrderResponse {
  order_intent_id: string | null;
  request_order_id: string;
  exchange_order_id: string | null;
  instrument_id: string | null;
  side: string | null;
  order_type: string | null;
  lot_qty: number | null;
  intended_price: string | null;
  broker_status: string;
  cancel_reason_code: string | null;
  reject_reason_code: string | null;
  last_observed_at: string | null;
}

export interface SignalResponse {
  candidate_id: string;
  instrument_id: string;
  strategy_id: string;
  timeframe: string;
  side: string;
  signal_type: string;
  candidate_status: string;
  expected_edge_bps: string | null;
  expected_holding_minutes: number | null;
  final_blocker_code: string | null;
  payload: JsonPayload;
}

export interface MarketInstrumentOverview {
  instrument_id: string;
  spread: string | null;
  mid_price: string | null;
  market_quality: string | null;
  best_bid: string | null;
  best_ask: string | null;
  recent_market_trades: JsonPayload[];
  order_book_summary: JsonPayload;
}

export interface MarketOverviewResponse {
  generated_at: string;
  instruments: MarketInstrumentOverview[];
}

export interface HourlyReportResponse {
  hourly_report_id: string;
  trading_date: string;
  session_type: string;
  micro_session_id: string;
  strategy_id: string;
  instrument_id: string | null;
  timeframe: string | null;
  realised_pnl: string | null;
  commission: string | null;
  signal_count: number;
  blocked_count: number;
  fill_ratio: string | null;
  payload: JsonPayload;
}

export interface DailyReportResponse {
  daily_report_id: string;
  trading_date: string;
  strategy_id: string;
  market_regime: string;
  session_type: string | null;
  instrument_id: string | null;
  timeframe: string | null;
  realised_pnl: string | null;
  commission: string | null;
  signal_count: number;
  blocked_count: number;
  fill_ratio: string | null;
  payload: JsonPayload;
}

export interface CounterfactualResponse {
  counterfactual_result_id: string;
  trading_date: string;
  candidate_id: string | null;
  order_intent_id: string | null;
  source_event_type: string;
  instrument_id: string;
  timeframe: string | null;
  strategy_id: string;
  blocker_code: string | null;
  cancel_reason_code: string | null;
  pnl_gross: string | null;
  pnl_net: string | null;
  slippage_bp: string | null;
  mfe_5m_bps: string | null;
  mae_5m_bps: string | null;
  mfe_10m_bps: string | null;
  mae_10m_bps: string | null;
  mfe_15m_bps: string | null;
  mae_15m_bps: string | null;
  would_profit_5m: boolean | null;
  would_profit_10m: boolean | null;
  would_profit_15m: boolean | null;
  payload: JsonPayload;
}

export interface ReportJobResponse {
  job_id: string;
  task_name: string;
  status: string;
  payload: JsonPayload;
}

export interface ReportJobStatusResponse {
  job_id: string;
  task_name: string;
  status: string;
  ready: boolean;
  successful: boolean;
  failed: boolean;
  result: JsonPayload | null;
  error: string | null;
  payload: JsonPayload;
}

export type ReportScope = "hourly" | "daily";

export interface ReportRebuildRequest {
  scope: ReportScope;
  trading_date: string;
  strategy_id: string;
  micro_session_id?: string | null;
  instrument_id?: string | null;
  timeframe?: string | null;
  session_type?: string | null;
  strategy_version?: number | null;
  include_counterfactual: boolean;
  force_rebuild: boolean;
}

export interface BlockerAnalyticsRow {
  blocker_code: string;
  blocker_family: string | null;
  count: number;
  terminal_count: number;
  candidate_count: number;
  measured_value_avg: string | null;
  threshold_value_avg: string | null;
  missed_pnl_gross: string | null;
  missed_pnl_net: string | null;
  avoided_loss: string | null;
  false_positive_rate: string | null;
  explanation_payload: JsonPayload;
}

export interface BlockerAnalyticsResponse {
  generated_at: string;
  filters: JsonPayload;
  rows: BlockerAnalyticsRow[];
}

export interface CandidateFunnelStage {
  stage_name: string;
  count: number;
  percentage_of_created: string | null;
  payload: JsonPayload;
}

export interface CandidateFunnelResponse {
  generated_at: string;
  filters: JsonPayload;
  stages: CandidateFunnelStage[];
  totals: JsonPayload;
}

export interface CanceledOrderDiagnosticsRow {
  cancel_reason_code: string;
  count: number;
  missed_pnl_gross: string | null;
  missed_pnl_net: string | null;
  avoided_loss: string | null;
  would_profit_5m_count: number;
  would_profit_10m_count: number;
  would_profit_15m_count: number;
  explanation_payload: JsonPayload;
}

export interface CanceledOrderDiagnosticsResponse {
  generated_at: string;
  filters: JsonPayload;
  rows: CanceledOrderDiagnosticsRow[];
}

export interface StrategyConfigResponse {
  strategy_config_id: string | null;
  strategy_id: string;
  version: number;
  session_template: string;
  is_active: boolean;
  valid_from: string | null;
  valid_to: string | null;
  config_payload: JsonPayload;
  risk_limits: JsonPayload;
}

export interface DailyReportRunRequest {
  trading_date: string;
  strategy_id: string;
  include_counterfactual: boolean;
}

export interface StrategyConfigUpdateRequest {
  strategy_id: string;
  session_template: string;
  config_payload: JsonPayload;
  risk_limits: JsonPayload;
  actor: string;
}

export interface AuthStatusResponse {
  auth_mode: string;
  role: string;
  subject: string;
  production_like: boolean;
}

export interface WebSocketTicketResponse {
  ticket: string;
  expires_at: string;
  auth_mode: string;
}

export interface HistoricalQualityResponse {
  report_id: string | null;
  coverage_pct: string;
  expected_candles: number;
  actual_candles: number;
  missing_intervals: number;
  duplicate_count: number;
  invalid_ohlc_count: number;
  abnormal_gap_count: number;
  source_distribution: Record<string, number>;
  session_type_distribution: Record<string, number>;
  timeframe_distribution: Record<string, number>;
  instrument_timeframes: JsonPayload[];
  corporate_action_days_count: number;
  dividend_gap_days_count: number;
  abnormal_gap_days_count: number;
  excluded_days_count: number;
  included_days_count: number;
  special_day_distribution: Record<string, number>;
  corporate_action_classification_status: string;
  quality_warnings: string[];
}

export interface HistoricalRunResponse {
  source: string;
  dry_run?: boolean;
  trading_days_processed?: number;
  bars_processed?: number;
  candidates_created?: number;
  blockers_created?: number;
  order_intents_created?: number;
  pseudo_orders_created?: number;
  counterfactual_results_built?: number;
  hourly_reports_built?: number;
  daily_reports_built?: number;
  deterministic_fingerprint?: string;
  real_orders_disabled?: boolean;
  bars_skipped_special_day?: number;
  skipped_dividend_gap_days?: number;
  skipped_corporate_action_days?: number;
  skipped_abnormal_gap_days?: number;
  strategy_config_source?: string;
  strategy_config_version?: number;
  special_day_classification_status?: string;
}

export interface CalibrationResponse {
  calibration_report_id: string | null;
  source: string;
  calibration_scope: string;
  calibration_clean: boolean;
  calibration_warnings: string[];
  calibration_data_mode: string;
  not_calibrated_from_history: string[];
  requires_shadow_live_calibration: boolean;
  normal_days_count: number;
  special_days_count: number;
  dividend_gap_days_count: number;
  corporate_action_days_count: number;
  abnormal_gap_days_count: number;
  excluded_days_count: number;
  included_days_count: number;
  excluded_from_primary_calibration_count: number;
  normal_days_stats: JsonPayload;
  dividend_gap_days_stats: JsonPayload;
  abnormal_gap_days_stats: JsonPayload;
  corporate_action_days_stats: JsonPayload;
  candidate_count: number;
  approved_count: number;
  blocked_count: number;
  pseudo_order_count: number;
  blocker_ranking: JsonPayload[];
  final_blocker_ranking: JsonPayload[];
  missed_opportunity_summary: JsonPayload;
  avoided_loss_summary: JsonPayload;
  gross_simulated_pnl: string;
  net_simulated_pnl: string;
  total_assumed_fees: string;
  total_assumed_slippage: string;
  best_session_type: string | null;
  worst_session_type: string | null;
  best_timeframe: string | null;
  worst_timeframe: string | null;
  best_instrument: string | null;
  worst_instrument: string | null;
  cost_sensitivity: JsonPayload;
  recommended_threshold_changes: JsonPayload;
  recommendations: JsonPayload;
}

export interface CorporateActionResponse {
  corporate_action_id: string;
  instrument_id: string;
  ticker: string | null;
  action_type: string;
  ex_date: string | null;
  amount_per_share: string | null;
  currency: string | null;
  source: string;
  confidence: string;
  payload: JsonPayload;
}

export interface MarketSpecialDayResponse {
  special_day_id: string;
  trading_date: string;
  instrument_id: string;
  ticker: string | null;
  special_day_type: string;
  reason_code: string;
  source: string;
  open_gap_bps: string | null;
  severity: string;
  exclude_from_primary_calibration: boolean;
  trade_policy: string;
  payload: JsonPayload;
}

export interface MarketSpecialDayClassificationResponse {
  source: string;
  classification_status: string;
  special_days_created: number;
  dividend_gap_days: number;
  abnormal_gap_days: number;
  excluded_from_primary_calibration: number;
  instruments: string[];
  from_date: string;
  to_date: string;
}

export interface WebSocketEnvelope<TPayload = unknown> {
  message_id: string;
  ts_utc: string;
  type: string;
  run_id: string | null;
  micro_session_id: string | null;
  payload: TPayload;
}

export interface DashboardSnapshotPayload {
  data?: {
    robot_status?: RobotStatusResponse;
    market?: MarketOverviewResponse;
    open_orders?: OrderResponse[];
    positions?: PositionResponse[];
    signals?: SignalResponse[];
    blockers?: BlockerAnalyticsResponse;
    candidate_funnel?: CandidateFunnelResponse;
  };
}

export interface ReportsSnapshotPayload {
  data?: {
    hourly?: HourlyReportResponse[];
    daily?: DailyReportResponse[];
    blockers?: BlockerAnalyticsResponse;
    candidate_funnel?: CandidateFunnelResponse;
    counterfactual?: CounterfactualResponse[];
    canceled_orders?: CanceledOrderDiagnosticsResponse;
  };
}

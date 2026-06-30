export type JsonPayload = Record<string, unknown>;

export type ConnectionState = "idle" | "loading" | "live" | "snapshot_closed" | "degraded";

export interface MoneyBalance {
  currency: string;
  available: string;
  blocked: string;
  total_portfolio_value_rub: string | null;
  available_cash_rub: string | null;
  blocked_cash_rub: string | null;
  expected_yield_rub: string | null;
  free_collateral_rub: string | null;
  account_id_masked: string | null;
  account_type: string | null;
  account_status: string | null;
  balance_currency: string;
  last_balance_refresh_at: string | null;
  balance_freshness_seconds: number | null;
  balance_degraded: boolean;
  balance_degraded_reason_code: string | null;
}

export interface PortfolioSummaryResponse {
  balance: MoneyBalance;
  positions_count: number;
  source: string;
}

export interface SessionPreflightResponse {
  market_open: boolean;
  market_closed_expected: boolean;
  now_msk: string;
  trading_date: string;
  calendar_date: string;
  session_type: string;
  session_phase: string;
  broker_trading_status: string;
  api_trade_available: boolean;
  official_exchange_open: boolean;
  official_exchange_closed: boolean;
  official_exchange_reason_code: string | null;
  official_exchange_source: string | null;
  broker_stream_available: boolean;
  broker_otc_or_indicative_available: boolean;
  api_trade_available_raw: boolean;
  api_trade_available_for_exchange: boolean;
  quote_source_allowed_for_data_collection: boolean;
  data_only_collection_allowed: boolean;
  streams_for_display_allowed: boolean;
  streams_for_calibration_allowed: boolean;
  venue_type: string;
  trading_mode: string;
  broker_availability_ignored_because_official_exchange_closed: boolean;
  next_session_at: string | null;
  next_session_type: string | null;
  current_window_start_at: string | null;
  current_window_end_at: string | null;
  reason_code: string;
  source: string;
  instruments_checked: string[];
  per_instrument_status: JsonPayload;
  warnings: string[];
}

export interface RobotCommandResponse {
  accepted: boolean;
  queued?: boolean;
  command_id: string | null;
  command: string;
  command_type: string | null;
  requested_by_role: string;
  requested_by: string;
  requested_at: string | null;
  status: string;
  reason_code: string | null;
  payload: JsonPayload;
  preflight_result: SessionPreflightResponse | JsonPayload | null;
  preflight_summary?: SessionPreflightResponse | JsonPayload | null;
  next_poll_after_seconds?: number | null;
  effective_logging_state?: string | null;
  message: string;
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
  data_shadow_collector_state?: string | null;
  daily_collection_active?: boolean;
  effective_logging_state?: string;
  command_id?: string | null;
  command_status?: string | null;
  preflight_phase?: string | null;
  start_in_progress?: boolean;
  start_requested_at?: string | null;
  preflight_started_at?: string | null;
  collector_started_at?: string | null;
  last_command_error?: string | null;
  last_command_reason_code?: string | null;
  next_retry_at?: string | null;
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
  ticker: string | null;
  class_code: string | null;
  board: string | null;
  exchange: string;
  venue_type: string;
  trading_mode: string;
  official_exchange_open: boolean;
  official_exchange_closed: boolean;
  quote_source: string;
  quote_allowed_for_data_collection: boolean;
  quote_allowed_for_display: boolean;
  last_price: string | null;
  last_price_at: string | null;
  last_price_ts: string | null;
  last_price_source: string | null;
  is_price_stale: boolean;
  price_staleness_seconds: number | null;
  received_ts?: string | null;
  exchange_ts?: string | null;
  received_age_ms?: number | null;
  exchange_age_ms?: number | null;
  stale_by_received_time?: boolean;
  stale_by_exchange_time?: boolean;
  freshness_status?: string;
  freshness_reason?: string | null;
  previous_close: string | null;
  change_abs: string | null;
  change_bps: string | null;
  session_type: string | null;
  broker_trading_status: string | null;
  api_trade_available: boolean | null;
  quote_status: string;
  last_candle_timeframe: string | null;
  spread: string | null;
  spread_abs: string | null;
  spread_bps: string | null;
  spread_abs_rub: string | null;
  spread_units_validated: boolean;
  mid_price: string | null;
  market_quality: string | null;
  market_quality_score: string | null;
  display_market_quality_score: string | null;
  calibration_market_quality_score: string | null;
  market_quality_label: string;
  market_quality_components: JsonPayload;
  best_bid: string | null;
  best_ask: string | null;
  bid_depth_lots: string | null;
  ask_depth_lots: string | null;
  book_imbalance: string | null;
  order_book_source: string | null;
  order_book_ts: string | null;
  order_book_age_ms: number | null;
  order_book_stale: boolean;
  recent_market_trades: JsonPayload[];
  market_trades_source: string | null;
  market_trades_age_ms: number | null;
  trade_tape_status?: string | null;
  trade_tape_reason?: string | null;
  reason_code: string | null;
  warning: string | null;
  order_book_summary: JsonPayload;
  quote_payload: JsonPayload;
}

export interface MarketOverviewResponse {
  generated_at: string;
  instruments: MarketInstrumentOverview[];
}

export interface DashboardMarketFeedSession {
  market_open: boolean;
  session_type: string | null;
  session_phase: string | null;
  venue_type: string | null;
  data_only_collection_allowed: boolean;
  reason_code: string | null;
  next_session_at: string | null;
}

export interface DashboardMarketFeedStatus {
    enabled: boolean;
    running: boolean;
    market_open: boolean;
    session_type: string | null;
    session_phase: string | null;
    venue_type: string | null;
    next_session_at?: string | null;
    last_refresh_at: string | null;
  selected_instrument: string | null;
  quote_rows_count: number;
  order_book_available: boolean;
  trade_tape_available: boolean;
  errors: string[];
  warnings: string[];
}

export interface DashboardMarketFeedSnapshot {
  generated_at: string;
  source: string;
  data_only_collection_required: boolean;
  session: DashboardMarketFeedSession;
  quote_rows: MarketInstrumentOverview[];
  market_overview: MarketOverviewResponse;
  selected_instrument: string;
  selected_details: MarketInstrumentOverview | null;
  errors: string[];
  warnings: string[];
  status: DashboardMarketFeedStatus;
}

export interface MarketMicrostructureSnapshotResponse {
  snapshot_id: string;
  ts_utc: string;
  exchange_ts: string | null;
  received_ts: string;
  instrument_id: string;
  session_type: string;
  session_phase: string;
  micro_session_id: string;
  broker_trading_status: string;
  best_bid: string | null;
  best_ask: string | null;
  mid_price: string | null;
  spread_abs: string | null;
  spread_bps: string | null;
  bid_depth_lots: string | null;
  ask_depth_lots: string | null;
  book_imbalance: string | null;
  market_quality_score: string | null;
  feed_freshness_age_ms: number | null;
  is_stale: boolean;
  source: string;
  payload: JsonPayload;
}

export interface MarketMicrostructureSummaryResponse {
  generated_at: string;
  lookback_minutes: number;
  instrument_id: string | null;
  snapshots_count: number;
  avg_spread_bps: string | null;
  p95_spread_bps: string | null;
  avg_bid_depth_lots: string | null;
  avg_ask_depth_lots: string | null;
  avg_book_imbalance: string | null;
  avg_market_quality_score: string | null;
  stale_incidents: number;
  latest_ts_utc: string | null;
  sessions: JsonPayload;
}

export interface DataShadowStatusResponse {
  enabled: boolean;
  collector_state: string;
  data_shadow_collector_state?: string | null;
  day_collection_state?: string;
  daily_collection_active?: boolean;
  current_window_state?: string;
  effective_logging_state?: string;
  command_status?: string | null;
  preflight_phase?: string | null;
  start_in_progress?: boolean;
  start_requested_at?: string | null;
  preflight_started_at?: string | null;
  collector_started_at?: string | null;
  last_command_error?: string | null;
  next_retry_at?: string | null;
  next_collection_window_at?: string | null;
  remaining_windows_today?: number;
  collector_left_running?: boolean;
  paused_at?: string | null;
  completed_for_day_at?: string | null;
  last_stop_reason?: string | null;
  last_pause_reason?: string | null;
  last_resume_at?: string | null;
  last_window_completed_at?: string | null;
  strategy_trading_disabled: boolean;
  real_orders_disabled: boolean;
  market_open: boolean | null;
  market_closed_expected: boolean | null;
  reason_code: string | null;
  next_session_at: string | null;
  stream_alive: boolean;
  last_message_age_seconds: string | null;
  candles_received: number | null;
  order_book_snapshots: number;
  market_microstructure_snapshots: number;
  avg_spread_bps: string | null;
  p95_spread_bps: string | null;
  avg_market_quality_score: string | null;
  current_session: string | null;
  started_at: string | null;
  stopped_at: string | null;
  last_command_id: string | null;
  last_command_status: string | null;
  last_command_reason_code: string | null;
  instruments: string[];
  stream_batches: JsonPayload[];
  supervisor_enabled?: boolean;
  supervisor_state?: string;
  stream_restart_count?: number;
  last_restart_at?: string | null;
  last_restart_reason?: string | null;
  stream_stale_count?: number;
  last_stream_error?: string | null;
  per_stream_status?: JsonPayload;
  warnings: string[];
  warning: string | null;
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
  dividend_sync_status: string;
  dividend_sync_clean: boolean;
  dividend_sync_failed_instruments: number;
  dividend_sync_error_count: number;
  api_import_dividend_events_count: number;
  manual_dividend_events_count: number;
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
  dividend_sync_status: string;
  dividend_sync_clean: boolean;
  dividend_sync_age_hours: number | null;
  dividend_sync_failed_instruments: number;
  dividend_sync_error_count: number;
  ready_for_shadow: boolean;
  ready_for_production: boolean;
  api_import_dividend_events_count: number;
  allow_manual_corporate_actions: boolean;
  future_dividend_windows_count: number;
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

export interface DividendSyncStatusResponse {
  source: string;
  status: string;
  clean: boolean;
  finished_at: string | null;
  age_hours: number | null;
  max_age_hours: number;
  from_date: string | null;
  to_date: string | null;
  instruments: string[];
  requested_from_date?: string;
  requested_to_date?: string;
  requested_instruments?: string[];
  instruments_processed: number;
  successful_instruments: number;
  failed_instruments: number;
  error_count: number;
  ready_for_shadow: boolean;
  ready_for_production: boolean;
  api_import_dividend_events_count: number;
  manual_dividend_events_count: number;
  last_sync_payload: JsonPayload;
}

export interface InstrumentRegistryResponse {
  instrument_id: string;
  ticker: string;
  class_code: string;
  source: string;
  resolution_status: string;
  resolved_at: string | null;
  instrument_uid_present: boolean;
  figi_present: boolean;
  lot_size: number;
  min_price_increment: string | null;
  currency: string;
  is_enabled: boolean;
  ready_for_broker_calls: boolean;
  resolution_error_code: string | null;
  resolution_error_message: string | null;
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

export interface IntradayAnalyticsSnapshotResponse {
  generated_at: string;
  trading_date: string;
  calendar_date: string | null;
  session_type: string | null;
  session_phase: string | null;
  mode: string;
  market_bias: string;
  market_activity: string;
  trend_strength: string | null;
  candidate_count: number;
  pseudo_order_count: number;
  real_order_count: number;
  blocked_count: number;
  near_miss_count: number;
  avg_spread_bps: string | null;
  p95_spread_bps: string | null;
  avg_depth: string | null;
  avg_imbalance: string | null;
  avg_market_quality: string | null;
  stale_incidents: number;
  candle_lag_p95_seconds: string | null;
  gross_pnl_proxy: string | null;
  net_pnl_proxy: string | null;
  warnings: string[];
  no_trade_reason: string | null;
  session_summaries: JsonPayload[];
  instrument_summaries: JsonPayload[];
  timeframe_summaries: JsonPayload[];
  side_summaries: JsonPayload[];
  micro_sessions: JsonPayload[];
  hour_summaries: JsonPayload[];
  contour_rows: JsonPayload[];
  payload: JsonPayload;
}

export interface CalibrationObservatoryStatusResponse {
  generated_at: string;
  latest_diagnostic: JsonPayload | null;
  latest_cube_generated_at: string | null;
  latest_regime_generated_at: string | null;
  draft_candidate_count: number;
  caveats: string[];
}

export interface CalibrationObservatoryRunRequest {
  universe: string;
  lookback_days: number;
  windows: string;
  mode: string;
  trigger_type: string;
  create_candidate_config: boolean;
  requested_by?: string | null;
}

export interface CalibrationObservatoryRunResponse {
  diagnostic_run_id: string;
  diagnosis: string;
  confidence: string;
  rolling_cube_rows: number;
  regime_summary: JsonPayload;
  top_contours: JsonPayload[];
  dead_contours: JsonPayload[];
  calibration_recommended: boolean;
  candidate_config_id: string | null;
  warnings: string[];
  blocking_issues: string[];
}

export interface CalibrationDiagnosticRunResponse {
  diagnostic_run_id: string;
  created_at: string;
  completed_at: string | null;
  requested_by: string | null;
  trigger_type: string;
  status: string;
  from_ts: string;
  to_ts: string;
  universe: JsonPayload;
  diagnosis: string;
  confidence: string;
  blocking_issues: JsonPayload[];
  warnings: JsonPayload[];
  diagnostic_payload: JsonPayload;
}

export interface RollingPerformanceCubeResponse {
  cube_id: string;
  generated_at: string;
  window_start: string;
  window_end: string;
  window_name: string;
  instrument_id: string;
  session_type: string;
  timeframe: string;
  side: string;
  mode: string;
  candidate_count: number;
  approved_count: number;
  blocked_count: number;
  pseudo_order_count: number;
  real_order_count: number;
  gross_pnl_proxy: string;
  net_pnl_proxy: string;
  avg_net_pnl_proxy: string;
  win_proxy: string | null;
  avg_spread_bps: string | null;
  p95_spread_bps: string | null;
  avg_depth: string | null;
  p95_depth: string | null;
  avg_imbalance: string | null;
  avg_market_quality: string | null;
  stale_incidents: number;
  stream_gap_count: number;
  active_days: number;
  last_signal_at: string | null;
  sample_warning: string | null;
  confidence: string;
  contour_status: string;
  cube_payload: JsonPayload;
}

export interface MarketRegimeSnapshotResponse {
  regime_snapshot_id: string;
  generated_at: string;
  window_start: string;
  window_end: string;
  instrument_id: string | null;
  session_type: string | null;
  market_regime: string;
  volume_score: string | null;
  volatility_score: string | null;
  spread_score: string | null;
  depth_score: string | null;
  imbalance_score: string | null;
  candidate_frequency_score: string | null;
  regime_payload: JsonPayload;
}

export interface StrategyConfigCandidateResponse {
  candidate_config_id: string;
  created_at: string;
  source_diagnostic_run_id: string | null;
  base_strategy_id: string;
  proposed_strategy_id: string;
  status: string;
  proposed_by: string;
  approval_required: boolean;
  approved_by: string | null;
  approved_at: string | null;
  proposal_payload: JsonPayload;
  validation_payload: JsonPayload;
  caveats: JsonPayload;
  rejection_reason: string | null;
}

export interface StrategyConfigCandidateRejectRequest {
  reason: string;
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
    session_preflight?: SessionPreflightResponse;
    session_preflight_error?: string;
  };
  sequence?: number;
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

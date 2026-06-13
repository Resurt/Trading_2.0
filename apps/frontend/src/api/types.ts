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
  strategy_id: string;
  blocker_code: string | null;
  cancel_reason_code: string | null;
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
    signals?: SignalResponse[];
  };
}

export interface ReportsSnapshotPayload {
  data?: {
    hourly?: HourlyReportResponse[];
    daily?: DailyReportResponse[];
  };
}

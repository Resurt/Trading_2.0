import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import CalibrationView from "../views/CalibrationView.vue";
import IntradayAnalyticsView from "../views/IntradayAnalyticsView.vue";

const { apiClientMock } = vi.hoisted(() => {
const intradaySnapshot = {
  generated_at: "2026-06-20T10:00:00Z",
  trading_date: "2026-06-20",
  calendar_date: "2026-06-20",
  session_type: "weekday_main",
  session_phase: "continuous_trading",
  mode: "data_shadow",
  market_bias: "long_bias",
  market_activity: "normal",
  trend_strength: "42.0000",
  candidate_count: 3,
  pseudo_order_count: 0,
  real_order_count: 0,
  blocked_count: 2,
  near_miss_count: 1,
  avg_spread_bps: "7.0000",
  p95_spread_bps: "9.0000",
  avg_depth: "55.0000",
  avg_imbalance: "0.1000",
  avg_market_quality: "0.9000",
  stale_incidents: 0,
  candle_lag_p95_seconds: null,
  gross_pnl_proxy: "0",
  net_pnl_proxy: "0",
  warnings: ["small_sample_is_early_evidence_not_final_truth"],
  no_trade_reason: "robot_too_strict_or_risk_blocked",
  session_summaries: [],
  instrument_summaries: [],
  timeframe_summaries: [],
  side_summaries: [],
  micro_sessions: [
    {
      micro_session_id: "2026-06-20:weekday_main:1000",
      market_activity: "normal",
      market_bias: "long_bias",
      candidate_count: 3,
      near_miss_count: 1,
    },
  ],
  hour_summaries: [],
  contour_rows: [
    {
      instrument_id: "MOEX:SBER",
      timeframe: "5m",
      side: "long",
      market_bias: "long_bias",
      market_activity: "normal",
      candidate_count: 3,
      blocked_count: 2,
      near_miss_count: 1,
      avg_spread_bps: "7.0000",
    },
  ],
  payload: {
    session_statuses: {
      weekday_morning: "completed",
      weekday_main: "running",
      weekday_evening: "not_started",
      weekend: "not_started",
    },
    top_instruments: [{ instrument_id: "MOEX:SBER", market_activity: "normal" }],
    weak_instruments: [{ instrument_id: "MOEX:GAZP", no_trade_reason: "market_dead" }],
  },
};

const observatoryRun = {
  diagnostic_run_id: "11111111-1111-1111-1111-111111111111",
  diagnosis: "calibration_recommended",
  confidence: "low",
  rolling_cube_rows: 1,
  regime_summary: { dominant_regime: "normal" },
  top_contours: [{ instrument_id: "MOEX:SBER", timeframe: "5m", side: "long" }],
  dead_contours: [{ instrument_id: "MOEX:GAZP", timeframe: "15m", side: "all" }],
  calibration_recommended: true,
  candidate_config_id: "22222222-2222-2222-2222-222222222222",
  warnings: ["small_sample_early_evidence_only"],
  blocking_issues: [],
};

const apiClientMock = {
  intradayToday: vi.fn(async () => intradaySnapshot),
  intradaySession: vi.fn(async () => intradaySnapshot),
  calibrationObservatoryStatus: vi.fn(async () => ({
    generated_at: "2026-06-20T10:00:00Z",
    latest_diagnostic: {
      diagnostic_run_id: "11111111-1111-1111-1111-111111111111",
      diagnosis: "market_dead",
      confidence: "low",
      warnings: { values: ["small_sample_early_evidence_only"] },
    },
    latest_cube_generated_at: "2026-06-20T10:00:00Z",
    latest_regime_generated_at: "2026-06-20T10:00:00Z",
    draft_candidate_count: 1,
    caveats: ["Candidate configs are not applied to live trading automatically."],
  })),
  runCalibrationObservatory: vi.fn(async () => observatoryRun),
  rollingPerformance: vi.fn(async () => [
    {
      cube_id: "33333333-3333-3333-3333-333333333333",
      generated_at: "2026-06-20T10:00:00Z",
      window_start: "2026-06-01T10:00:00Z",
      window_end: "2026-06-20T10:00:00Z",
      window_name: "20d",
      instrument_id: "MOEX:SBER",
      session_type: "weekday_main",
      timeframe: "5m",
      side: "long",
      mode: "data_shadow",
      candidate_count: 3,
      approved_count: 1,
      blocked_count: 2,
      pseudo_order_count: 0,
      real_order_count: 0,
      gross_pnl_proxy: "0",
      net_pnl_proxy: "0",
      avg_net_pnl_proxy: "0",
      win_proxy: null,
      avg_spread_bps: "7.0000",
      p95_spread_bps: "9.0000",
      avg_depth: "55.0000",
      p95_depth: "60.0000",
      avg_imbalance: "0.1000",
      avg_market_quality: "0.9000",
      stale_incidents: 0,
      stream_gap_count: 0,
      active_days: 1,
      last_signal_at: "2026-06-20T10:00:00Z",
      sample_warning: "small_candidate_sample",
      confidence: "low",
      contour_status: "research_only",
      cube_payload: {},
    },
  ]),
  calibrationRegime: vi.fn(async () => [
    {
      regime_snapshot_id: "44444444-4444-4444-4444-444444444444",
      generated_at: "2026-06-20T10:00:00Z",
      window_start: "2026-06-01T10:00:00Z",
      window_end: "2026-06-20T10:00:00Z",
      instrument_id: "MOEX:SBER",
      session_type: "weekday_main",
      market_regime: "normal",
      volume_score: "100",
      volatility_score: "20",
      spread_score: "7",
      depth_score: "55",
      imbalance_score: "0.1",
      candidate_frequency_score: "3",
      regime_payload: {},
    },
  ]),
  configCandidates: vi.fn(async () => [
    {
      candidate_config_id: "22222222-2222-2222-2222-222222222222",
      created_at: "2026-06-20T10:00:00Z",
      source_diagnostic_run_id: "11111111-1111-1111-1111-111111111111",
      base_strategy_id: "baseline",
      proposed_strategy_id: "baseline_candidate_draft",
      status: "draft",
      proposed_by: "system",
      approval_required: true,
      approved_by: null,
      approved_at: null,
      proposal_payload: { apply_automatically: false },
      validation_payload: { runtime_config_changed: false },
      caveats: { no_live_config_auto_apply: true },
      rejection_reason: null,
    },
  ]),
  approveConfigCandidateForShadow: vi.fn(async () => ({})),
  rejectConfigCandidate: vi.fn(async () => ({})),
  calibrationReport: vi.fn(async () => ({})),
};

  return { apiClientMock };
});

vi.mock("../api/client", () => ({
  apiClient: apiClientMock,
}));

describe("IntradayAnalyticsView", () => {
  it("renders diagnostic-only intraday summaries", async () => {
    const wrapper = mount(IntradayAnalyticsView);
    await flushPromises();

    expect(wrapper.find('[data-testid="intraday-analytics-page"]').exists()).toBe(true);
    expect(wrapper.text()).toContain(
      "Intraday analytics is diagnostic only. It does not enable trading.",
    );
    expect(wrapper.text()).toContain("MOEX:SBER");
    expect(wrapper.text()).toContain("long_bias");
    expect(wrapper.text()).toContain("robot_too_strict_or_risk_blocked");
  });
});

describe("CalibrationView", () => {
  it("renders observatory diagnostics and safe candidate proposal controls", async () => {
    const wrapper = mount(CalibrationView);
    await flushPromises();

    expect(wrapper.find('[data-testid="calibration-page"]').exists()).toBe(true);
    expect(wrapper.text()).toContain(
      "Candidate configs are not applied to live trading automatically.",
    );
    expect(wrapper.text()).toContain("market_dead");
    expect(wrapper.text()).toContain("MOEX:SBER");
    expect(wrapper.text()).toContain("baseline_candidate_draft");

    await wrapper.find("button").trigger("click");
    await flushPromises();

    expect(apiClientMock.runCalibrationObservatory).toHaveBeenCalled();
    expect(wrapper.text()).toContain("calibration_recommended");
  });
});

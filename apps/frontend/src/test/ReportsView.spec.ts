import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { describe, expect, it } from "vitest";

import ReportsView from "../views/ReportsView.vue";
import { useReportsStore } from "../stores/reports";

describe("ReportsView", () => {
  it("renders report analytics sections and counterfactual rows", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const reports = useReportsStore();
    reports.dailyReports = [
      {
        daily_report_id: "daily-1",
        trading_date: "2026-06-13",
        strategy_id: "baseline",
        market_regime: "long_bias",
        session_type: null,
        instrument_id: null,
        timeframe: null,
        realised_pnl: "120",
        commission: "4",
        signal_count: 10,
        blocked_count: 3,
        fill_ratio: "0.7",
        payload: {
          trend: { market_regime: "long_bias", average_return_bps: "40" },
          execution_quality: { fill_ratio: "0.7" },
          funnel: { candidates: 10, blockers: 3 },
          blocker_ranking: [{ reason_code: "market_quality_low", count: 2 }],
          summary_by_session_type: { weekday_main: { signal_count: 10 } },
          summary_by_instrument: { "MOEX:SBER": { signal_count: 8 } },
          summary_by_timeframe: { "5m": { signal_count: 6 } },
        },
      },
    ];
    reports.hourlyReports = [
      {
        hourly_report_id: "hourly-1",
        trading_date: "2026-06-13",
        session_type: "weekday_main",
        micro_session_id: "2026-06-13:weekday_main:1000",
        strategy_id: "baseline",
        instrument_id: "MOEX:SBER",
        timeframe: "5m",
        realised_pnl: "10",
        commission: "1",
        signal_count: 2,
        blocked_count: 1,
        fill_ratio: "0.5",
        payload: {},
      },
    ];
    reports.counterfactuals = [
      {
        counterfactual_result_id: "cf-1",
        trading_date: "2026-06-13",
        candidate_id: "candidate-1",
        order_intent_id: null,
        source_event_type: "blocked_candidate",
        instrument_id: "MOEX:SBER",
        timeframe: "5m",
        strategy_id: "baseline",
        blocker_code: "market_quality_low",
        cancel_reason_code: null,
        pnl_gross: "15",
        pnl_net: "14",
        slippage_bp: "1",
        mfe_5m_bps: "20",
        mae_5m_bps: "-5",
        mfe_10m_bps: "30",
        mae_10m_bps: "-7",
        mfe_15m_bps: "40",
        mae_15m_bps: "-10",
        would_profit_5m: true,
        would_profit_10m: true,
        would_profit_15m: false,
        payload: {},
      },
    ];
    reports.blockerAnalytics = {
      generated_at: "2026-06-13T07:10:00Z",
      filters: {},
      rows: [
        {
          blocker_code: "market_quality_low",
          blocker_family: "market_quality",
          count: 2,
          terminal_count: 2,
          candidate_count: 2,
          measured_value_avg: "0.4",
          threshold_value_avg: "0.7",
          missed_pnl_gross: "15",
          missed_pnl_net: "14",
          avoided_loss: "0",
          false_positive_rate: "0.5",
          explanation_payload: { summary: "quality under threshold" },
        },
      ],
    };
    reports.candidateFunnel = {
      generated_at: "2026-06-13T07:10:00Z",
      filters: {},
      stages: [
        { stage_name: "created", count: 10, percentage_of_created: "1", payload: {} },
        { stage_name: "blocked", count: 3, percentage_of_created: "0.3", payload: {} },
        { stage_name: "filled", count: 4, percentage_of_created: "0.4", payload: {} },
      ],
      totals: {},
    };
    reports.canceledDiagnostics = {
      generated_at: "2026-06-13T07:10:00Z",
      filters: {},
      rows: [
        {
          cancel_reason_code: "stale_order",
          count: 1,
          missed_pnl_gross: "8",
          missed_pnl_net: "7",
          avoided_loss: "0",
          would_profit_5m_count: 1,
          would_profit_10m_count: 1,
          would_profit_15m_count: 0,
          explanation_payload: { summary: "order became stale" },
        },
      ],
    };

    const wrapper = mount(ReportsView, {
      global: {
        plugins: [pinia],
      },
    });

    expect(wrapper.find('[data-testid="reports-page"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("long_bias");
    expect(wrapper.text()).toContain("market_quality_low");
    expect(wrapper.text()).toContain("2026-06-13:weekday_main:1000");
    expect(wrapper.text()).toContain("Candidate funnel");
    expect(wrapper.text()).toContain("stale_order");
    expect(wrapper.text()).toContain("Counterfactual horizons");
  });
});

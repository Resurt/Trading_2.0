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
        strategy_id: "baseline",
        blocker_code: "market_quality_low",
        cancel_reason_code: null,
        would_profit_5m: true,
        would_profit_10m: true,
        would_profit_15m: false,
        payload: {},
      },
    ];

    const wrapper = mount(ReportsView, {
      global: {
        plugins: [pinia],
      },
    });

    expect(wrapper.find('[data-testid="reports-page"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("long_bias");
    expect(wrapper.text()).toContain("market_quality_low");
    expect(wrapper.text()).toContain("2026-06-13:weekday_main:1000");
  });
});

import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { describe, expect, it } from "vitest";

import LiveDashboardView from "../views/LiveDashboardView.vue";
import { useMarketStore } from "../stores/market";
import { usePortfolioStore } from "../stores/portfolio";
import { useReportsStore } from "../stores/reports";
import { useRobotStore } from "../stores/robot";

function mountWithStores() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const robot = useRobotStore();
  const market = useMarketStore();
  const portfolio = usePortfolioStore();
  const reports = useReportsStore();

  robot.status = {
    balance: {
      currency: "RUB",
      available: "150000",
      blocked: "1000",
      total_portfolio_value_rub: "250000",
      available_cash_rub: "150000",
      blocked_cash_rub: "1000",
      expected_yield_rub: "2500",
      free_collateral_rub: "120000",
      account_id_masked: "acc***001",
      account_type: "broker",
      account_status: "open",
      balance_currency: "RUB",
      last_balance_refresh_at: "2026-06-13T07:10:00Z",
      balance_freshness_seconds: 12,
      balance_degraded: false,
      balance_degraded_reason_code: null,
    },
    active_instruments: ["MOEX:SBER"],
    active_timeframes: ["5m"],
    strategy_state: "candidate",
    session_type: "weekday_main",
    session_phase: "continuous_trading",
    broker_trading_status: "normal_trading",
    open_orders_count: 1,
    active_positions_count: 1,
    degraded_flags: ["balance_unavailable"],
    robot_control_state: "start_requested",
    micro_session_id: "2026-06-13:weekday_main:1000",
  };
  robot.signals = [
    {
      candidate_id: "candidate-1",
      instrument_id: "MOEX:SBER",
      strategy_id: "baseline",
      timeframe: "5m",
      side: "buy",
      signal_type: "entry",
      candidate_status: "blocked",
      expected_edge_bps: "12.5",
      expected_holding_minutes: 5,
      final_blocker_code: "spread_too_wide",
      payload: { explanation: "spread above configured threshold" },
    },
  ];
  market.overview = {
    generated_at: "2026-06-13T07:10:00Z",
    instruments: [
      {
        instrument_id: "MOEX:SBER",
        spread: "0.1",
        mid_price: "100.05",
        market_quality: "0.92",
        best_bid: "100",
        best_ask: "100.1",
        recent_market_trades: [],
        order_book_summary: {
          best_bid_qty_lots: "10",
          best_ask_qty_lots: "12",
          bid_depth_lots: "100",
          ask_depth_lots: "120",
        },
      },
    ],
  };
  market.dataShadowStatus = {
    enabled: true,
    strategy_trading_disabled: true,
    real_orders_disabled: true,
    stream_alive: true,
    last_message_age_seconds: "1.2",
    candles_received: null,
    order_book_snapshots: 42,
    market_microstructure_snapshots: 42,
    avg_spread_bps: "8.5",
    p95_spread_bps: "12.0",
    avg_market_quality_score: "0.88",
    current_session: "weekday_main",
    warning: "Strategy trading disabled: data-only shadow mode",
  };
  portfolio.positions = [
    {
      instrument_id: "MOEX:SBER",
      account_id: "acc",
      position_side: "long",
      qty_lots: 10,
      avg_price: "99",
      market_price: "100",
      unrealized_pnl: "10",
      realised_pnl: "0",
      snapshot_ts: "2026-06-13T07:10:00Z",
    },
  ];
  portfolio.openOrders = [
    {
      order_intent_id: "intent-1",
      request_order_id: "request-1",
      exchange_order_id: "exchange-1",
      instrument_id: "MOEX:SBER",
      side: "buy",
      order_type: "limit",
      lot_qty: 10,
      intended_price: "100",
      broker_status: "working",
      cancel_reason_code: null,
      reject_reason_code: null,
      last_observed_at: "2026-06-13T07:10:00Z",
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
      signal_count: 1,
      blocked_count: 1,
      fill_ratio: "0.5",
      payload: {},
    },
  ];

  return mount(LiveDashboardView, {
    global: {
      plugins: [pinia],
    },
  });
}

describe("LiveDashboardView", () => {
  it("renders live widgets with machine-readable reason codes", () => {
    const wrapper = mountWithStores();

    expect(wrapper.find('[data-testid="live-dashboard"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("MOEX:SBER");
    expect(wrapper.text()).toContain("spread_too_wide");
    expect(wrapper.text()).toContain("spread above configured threshold");
    expect(wrapper.text()).toContain("weekday_main");
    expect(wrapper.text()).toContain("request-1");
    expect(wrapper.text()).toContain("Stream health / reconnect");
    expect(wrapper.text()).toContain("Strategy trading disabled: data-only shadow mode");
    expect(wrapper.text()).toContain("acc***001");
    expect(wrapper.text()).toContain("Обновить баланс");
  });

  it("renders degraded balance state", async () => {
    const wrapper = mountWithStores();
    const robot = useRobotStore();

    robot.status.balance = {
      ...robot.status.balance,
      balance_degraded: true,
      balance_degraded_reason_code: "broker_balance_unavailable",
      account_id_masked: null,
    };
    await nextTick();

    expect(wrapper.text()).toContain("Баланс недоступен");
    expect(wrapper.text()).toContain("broker_balance_unavailable");
  });
});

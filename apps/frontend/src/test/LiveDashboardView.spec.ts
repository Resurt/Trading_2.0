import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { describe, expect, it } from "vitest";

import { useMarketStore } from "../stores/market";
import { usePortfolioStore } from "../stores/portfolio";
import { useReportsStore } from "../stores/reports";
import { useRobotStore } from "../stores/robot";
import LiveDashboardView from "../views/LiveDashboardView.vue";

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
      quoteFixture("MOEX:SBER", "SBER", "100.05", "live", "live_order_book_mid"),
      quoteFixture("MOEX:GAZP", "GAZP", "104.49", "stale", "latest_market_candle_close"),
      quoteFixture("MOEX:LKOH", "LKOH", "4377", "stale", "latest_market_candle_close"),
      quoteFixture("MOEX:YDEX", "YDEX", "3912", "stale", "latest_market_candle_close"),
      quoteFixture("MOEX:TATN", "TATN", "535.3", "stale", "latest_market_candle_close"),
      quoteFixture("MOEX:GMKN", "GMKN", "131.7", "stale", "latest_market_candle_close"),
      quoteFixture("MOEX:OZON", "OZON", "3710.5", "stale", "latest_market_candle_close"),
      quoteFixture("MOEX:VTBR", "VTBR", "75.44", "previous_close", "previous_close"),
    ],
  };
  market.selectedInstrumentId = "MOEX:SBER";
  market.dataShadowStatus = {
    enabled: true,
    collector_state: "collecting",
    strategy_trading_disabled: true,
    real_orders_disabled: true,
    market_open: true,
    market_closed_expected: false,
    reason_code: "market_open",
    next_session_at: null,
    stream_alive: true,
    last_message_age_seconds: "1.2",
    candles_received: null,
    order_book_snapshots: 42,
    market_microstructure_snapshots: 42,
    avg_spread_bps: "8.5",
    p95_spread_bps: "12.0",
    avg_market_quality_score: "0.88",
    current_session: "weekday_main",
    started_at: "2026-06-13T07:10:00Z",
    stopped_at: null,
    last_command_id: "command-1",
    last_command_status: "applied",
    last_command_reason_code: "data_only_collection_started",
    instruments: ["MOEX:SBER"],
    stream_batches: [{ batch: 1, instruments: ["MOEX:SBER"] }],
    warnings: [],
    warning: "Strategy trading disabled: data-only shadow mode",
  };
  portfolio.positions = [];
  portfolio.openOrders = [];
  reports.hourlyReports = [];

  return mount(LiveDashboardView, {
    global: {
      plugins: [pinia],
    },
  });
}

describe("LiveDashboardView", () => {
  it("renders operator dashboard with quote table and readable status", () => {
    const wrapper = mountWithStores();

    expect(wrapper.find('[data-testid="live-dashboard"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("Котировки core universe");
    expect(wrapper.text()).toContain("8 инструментов");
    expect(wrapper.text()).toContain("СТАКАН");
    expect(wrapper.text()).toContain("ЛЕНТА СДЕЛОК");
    expect(wrapper.text()).toContain("100,00");
    expect(wrapper.text()).toContain("100,10");
    expect(wrapper.text()).toContain("Покупка");
    expect(wrapper.text()).toContain("Сессия MOEX");
    expect(wrapper.text()).toContain("Data-only сбор");
    expect(wrapper.text()).toContain("real orders, pseudo-orders");
    expect(wrapper.text()).toContain("acc***001");
    expect(wrapper.text()).toContain("Broker balance получен");
    expect(wrapper.text()).toContain("spread_too_wide");
    expect(wrapper.text()).toContain("spread above configured threshold");
    expect(wrapper.text()).not.toContain("request-1");
  });

  it("renders degraded balance state with refresh guidance", async () => {
    const wrapper = mountWithStores();
    const robot = useRobotStore();

    robot.status.balance = {
      ...robot.status.balance,
      balance_degraded: true,
      balance_degraded_reason_code: "broker_balance_unavailable",
      account_id_masked: null,
    };
    await nextTick();

    expect(wrapper.text()).toContain("Счёт не получен");
    expect(wrapper.text()).toContain("Нет сохранённых данных счёта");
    expect(wrapper.text()).toContain("Обновить");
  });

  it("updates selected instrument panel when a quote row is clicked", async () => {
    const wrapper = mountWithStores();

    await wrapper.findAll(".quote-table tbody tr")[1].trigger("click");
    await nextTick();

    const market = useMarketStore();
    expect(market.selectedInstrumentId).toBe("MOEX:GAZP");
    expect(wrapper.text()).toContain("GAZP / stale");
  });
});

function quoteFixture(
  instrumentId: string,
  ticker: string,
  price: string,
  quoteStatus: string,
  source: string,
) {
  const live = quoteStatus === "live";
  return {
    instrument_id: instrumentId,
    ticker,
    last_price: price,
    last_price_at: live ? "2026-06-13T07:10:00Z" : "2026-06-11T20:50:00Z",
    last_price_ts: live ? "2026-06-13T07:10:00Z" : "2026-06-11T20:50:00Z",
    last_price_source: source,
    is_price_stale: !live,
    price_staleness_seconds: live ? 1 : 172800,
    previous_close: "99.00",
    change_abs: "1.05",
    change_bps: "106.1",
    session_type: live ? "weekday_main" : "weekend",
    broker_trading_status: live ? "normal_trading" : "closed",
    api_trade_available: live,
    quote_status: quoteStatus,
    last_candle_timeframe: "1m",
    spread: live ? "0.1" : null,
    spread_abs: live ? "0.1" : null,
    spread_bps: live ? "10.0" : null,
    mid_price: live ? price : null,
    market_quality: live ? "0.92" : null,
    best_bid: live ? "100" : null,
    best_ask: live ? "100.1" : null,
    bid_depth_lots: live ? "100" : null,
    ask_depth_lots: live ? "120" : null,
    book_imbalance: live ? "-0.09" : null,
    order_book_source: live ? "tbank_order_book" : null,
    order_book_ts: live ? "2026-06-13T07:10:00Z" : null,
    order_book_stale: !live,
    order_book_summary: live
      ? {
          source: "tbank_order_book",
          bids: [
            { price: "100.00", quantity_lots: "10" },
            { price: "99.98", quantity_lots: "30" },
          ],
          asks: [
            { price: "100.10", quantity_lots: "12" },
            { price: "100.12", quantity_lots: "26" },
          ],
          best_bid_qty_lots: "10",
          best_ask_qty_lots: "12",
          bid_depth_lots: "100",
          ask_depth_lots: "120",
        }
      : {},
    recent_market_trades: live
      ? [
          {
            exchange_ts: "2026-06-13T07:10:01Z",
            price: "100.05",
            quantity_lots: "5",
            side: "buy",
          },
        ]
      : [],
    quote_payload: {},
  };
}

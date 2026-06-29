import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DashboardMarketFeedSnapshot, MarketInstrumentOverview } from "../api/types";
import { useMarketStore } from "../stores/market";

const apiClientMock = vi.hoisted(() => ({
  dashboardMarketFeedSnapshot: vi.fn(),
  refreshDashboardMarketFeed: vi.fn(),
  marketOverview: vi.fn(),
  refreshMarketQuotes: vi.fn(),
  marketInstrumentDetails: vi.fn(),
  dataShadowStatus: vi.fn(),
}));

vi.mock("../api/client", () => ({
  apiClient: apiClientMock,
  openAuthenticatedWebSocket: vi.fn(),
}));

function instrument(
  overrides: Partial<MarketInstrumentOverview> = {},
): MarketInstrumentOverview {
  return {
    instrument_id: "MOEX:SBER",
    ticker: null,
    class_code: "TQBR",
    board: "TQBR",
    exchange: "MOEX",
    venue_type: "unknown",
    trading_mode: "unknown",
    official_exchange_open: false,
    official_exchange_closed: false,
    quote_source: "unavailable",
    quote_allowed_for_data_collection: false,
    quote_allowed_for_display: false,
    last_price: null,
    last_price_at: null,
    last_price_ts: null,
    last_price_source: null,
    is_price_stale: true,
    price_staleness_seconds: null,
    previous_close: null,
    change_abs: null,
    change_bps: null,
    session_type: null,
    broker_trading_status: null,
    api_trade_available: null,
    quote_status: "unknown",
    last_candle_timeframe: null,
    spread: null,
    spread_abs: null,
    spread_bps: null,
    spread_abs_rub: null,
    spread_units_validated: true,
    mid_price: null,
    market_quality: null,
    market_quality_score: null,
    display_market_quality_score: null,
    calibration_market_quality_score: null,
    market_quality_label: "unknown",
    market_quality_components: {},
    best_bid: null,
    best_ask: null,
    bid_depth_lots: null,
    ask_depth_lots: null,
    book_imbalance: null,
    order_book_source: null,
    order_book_ts: null,
    order_book_age_ms: null,
    order_book_stale: true,
    recent_market_trades: [],
    market_trades_source: null,
    market_trades_age_ms: null,
    reason_code: null,
    warning: null,
    order_book_summary: {},
    quote_payload: {},
    ...overrides,
  };
}

function feedSnapshot(
  rows: MarketInstrumentOverview[],
  selectedInstrumentId = "MOEX:SBER",
): DashboardMarketFeedSnapshot {
  const selected =
    rows.find((row) => row.instrument_id === selectedInstrumentId) ?? rows[0] ?? null;
  const generatedAt = new Date().toISOString();
  return {
    generated_at: generatedAt,
    source: "dashboard_market_feed",
    data_only_collection_required: false,
    session: {
      market_open: true,
      session_type: "weekday_main",
      session_phase: "continuous_trading",
      venue_type: "official_exchange",
      data_only_collection_allowed: true,
      reason_code: "market_open",
      next_session_at: null,
    },
    quote_rows: rows,
    market_overview: {
      generated_at: generatedAt,
      instruments: rows,
    },
    selected_instrument: selectedInstrumentId,
    selected_details: selected,
    errors: [],
    warnings: [],
    status: {
      enabled: true,
      running: true,
      market_open: true,
      session_type: "weekday_main",
      session_phase: "continuous_trading",
      venue_type: "official_exchange",
      last_refresh_at: generatedAt,
      selected_instrument: selectedInstrumentId,
      quote_rows_count: rows.length,
      order_book_available: Boolean(selected?.order_book_source),
      trade_tape_available: Boolean(selected?.recent_market_trades.length),
      errors: [],
      warnings: [],
    },
  };
}

describe("market store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    Object.values(apiClientMock).forEach((mock) => mock.mockReset());
  });

  it("starts with eight core instruments and selects SBER by default", () => {
    const market = useMarketStore();

    expect(market.quoteRows).toHaveLength(8);
    expect(market.selectedInstrumentId).toBe("MOEX:SBER");
    expect(market.currentInstrument?.instrument_id).toBe("MOEX:SBER");
  });

  it("does not clear quote board on empty WebSocket snapshot", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [instrument({ last_price: "313.10", quote_status: "live" })],
    });

    market.applyOverview({ generated_at: now, instruments: [] });

    expect(market.quoteRows).toHaveLength(8);
    expect(market.currentInstrument?.last_price).toBe("313.10");
    expect(market.warnings).toContain("empty_market_ws_snapshot");
  });

  it("merges partial snapshots without deleting existing quote rows", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({ instrument_id: "MOEX:SBER", ticker: "SBER", last_price: "313.10" }),
        instrument({ instrument_id: "MOEX:GAZP", ticker: "GAZP", last_price: "144.20" }),
      ],
    });

    market.applyOverview({
      generated_at: now,
      instruments: [instrument({ instrument_id: "MOEX:SBER", ticker: "SBER", last_price: "313.50" })],
    });

    expect(market.quoteRows).toHaveLength(8);
    expect(market.quoteRows.find((row) => row.instrument_id === "MOEX:SBER")?.last_price).toBe("313.50");
    expect(market.quoteRows.find((row) => row.instrument_id === "MOEX:GAZP")?.last_price).toBe("144.20");
  });

  it("preserves old quote board rows when overview API fails", async () => {
    const market = useMarketStore();
    market.applyOverview({
      generated_at: new Date().toISOString(),
      instruments: [instrument({ last_price: "313.10", quote_status: "live" })],
    });
    apiClientMock.dashboardMarketFeedSnapshot.mockRejectedValue(new Error("temporary API failure"));

    await market.fetchOverview({ silent: true });

    expect(market.currentInstrument?.last_price).toBe("313.10");
  });

  it("loads selected instrument details lazily", async () => {
    const market = useMarketStore();
    const gazp = instrument({ instrument_id: "MOEX:GAZP", ticker: "GAZP", last_price: "144.20" });
    market.applyOverview({ generated_at: new Date().toISOString(), instruments: [gazp] });
    apiClientMock.dashboardMarketFeedSnapshot.mockResolvedValue(
      feedSnapshot([{ ...gazp, best_bid: "144.18", best_ask: "144.22" }], "MOEX:GAZP"),
    );
    market.selectedInstrumentId = "MOEX:GAZP";

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(apiClientMock.dashboardMarketFeedSnapshot).toHaveBeenCalledWith(
      expect.objectContaining({
        selected_instrument: "MOEX:GAZP",
        include_order_book: true,
        include_trades: true,
      }),
    );
    expect(market.currentInstrument?.best_bid).toBe("144.18");
  });

  it("does not revert selected instrument when an old details response arrives late", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    const sber = instrument({ instrument_id: "MOEX:SBER", ticker: "SBER", last_price: "313.10" });
    const gazp = instrument({ instrument_id: "MOEX:GAZP", ticker: "GAZP", last_price: "144.20" });
    market.applyOverview({ generated_at: now, instruments: [sber, gazp] });

    market.selectedInstrumentId = "MOEX:GAZP";
    market.applyDashboardFeedSnapshot(
      feedSnapshot([{ ...sber, best_bid: "313.00", best_ask: "313.20" }], "MOEX:SBER"),
      "MOEX:SBER",
    );

    expect(market.selectedInstrumentId).toBe("MOEX:GAZP");
    expect(market.currentInstrument?.instrument_id).toBe("MOEX:GAZP");
    expect(market.quoteRows.find((row) => row.instrument_id === "MOEX:SBER")?.best_bid).toBe("313.00");
  });

  it("starts dashboard feed polling without requiring Start", async () => {
    const market = useMarketStore();
    apiClientMock.dashboardMarketFeedSnapshot.mockResolvedValue(
      feedSnapshot([instrument({ last_price: "313.10", quote_status: "live" })]),
    );
    apiClientMock.dataShadowStatus.mockResolvedValue({
      enabled: true,
      collector_state: "stopped",
      strategy_trading_disabled: true,
      real_orders_disabled: true,
      market_open: null,
      market_closed_expected: null,
      reason_code: null,
      next_session_at: null,
      stream_alive: false,
      last_message_age_seconds: null,
      candles_received: null,
      order_book_snapshots: 0,
      market_microstructure_snapshots: 0,
      avg_spread_bps: null,
      p95_spread_bps: null,
      avg_market_quality_score: null,
      current_session: null,
      started_at: null,
      stopped_at: null,
      last_command_id: null,
      last_command_status: null,
      last_command_reason_code: null,
      instruments: [],
      stream_batches: [],
      warnings: [],
      warning: null,
    });

    market.startDashboardFeed();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(apiClientMock.dashboardMarketFeedSnapshot).toHaveBeenCalled();
    expect(market.currentInstrument?.last_price).toBe("313.10");
    expect(market.dataShadowStatus.collector_state).toBe("stopped");
    market.stopDashboardFeed();
  });

  it("keeps broker order book metrics when weaker snapshots arrive later", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();

    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          last_price: "313.19",
          last_price_at: now,
          last_price_source: "live_exchange_order_book",
          quote_source: "live_exchange_order_book",
          venue_type: "official_exchange",
          trading_mode: "standard_exchange",
          official_exchange_open: true,
          quote_allowed_for_data_collection: true,
          quote_allowed_for_display: true,
          quote_status: "live",
          spread: "0.36",
          spread_abs: "0.36",
          spread_bps: "11.49",
          spread_abs_rub: "0.36",
          mid_price: "313.19",
          market_quality: "0.7017",
          display_market_quality_score: "0.7017",
          calibration_market_quality_score: "0.7017",
          market_quality_label: "ok",
          best_bid: "313.01",
          best_ask: "313.37",
          bid_depth_lots: "700",
          ask_depth_lots: "420",
          order_book_source: "tbank_order_book",
          order_book_ts: now,
          order_book_stale: false,
          order_book_summary: {
            bid_depth_lots: "700",
            ask_depth_lots: "420",
            spread_bps: "11.49",
          },
        }),
      ],
    });

    market.applyOverview({
      generated_at: "2026-06-21T08:56:01Z",
      instruments: [
        instrument({
          last_price: "312.84",
          last_price_at: "2026-06-19T20:50:00Z",
          last_price_source: "latest_market_candle_close",
          quote_status: "stale",
        }),
      ],
    });

    expect(market.currentInstrument?.last_price_source).toBe("live_exchange_order_book");
    expect(market.currentInstrument?.last_price).toBe("313.19");
    expect(market.currentInstrument?.mid_price).toBe("313.19");
    expect(market.currentInstrument?.best_bid).toBe("313.01");
    expect(market.currentInstrument?.best_ask).toBe("313.37");
    expect(market.currentInstrument?.market_quality).toBe("0.7017");
    expect(market.currentInstrument?.order_book_summary.spread_bps).toBe("11.49");
  });
});

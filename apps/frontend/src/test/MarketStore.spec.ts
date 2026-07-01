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
const openAuthenticatedWebSocketMock = vi.hoisted(() => vi.fn());

vi.mock("../api/client", () => ({
  apiClient: apiClientMock,
  openAuthenticatedWebSocket: openAuthenticatedWebSocketMock,
}));

class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  readonly readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    this.onclose?.();
  }
}

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
    received_ts: null,
    exchange_ts: null,
    received_age_ms: null,
    exchange_age_ms: null,
    stale_by_received_time: true,
    stale_by_exchange_time: true,
    freshness_status: "unknown",
    freshness_reason: null,
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
    trade_tape_status: null,
    trade_tape_reason: null,
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
    openAuthenticatedWebSocketMock.mockReset();
    vi.stubGlobal("WebSocket", FakeWebSocket);
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

  it("does not show transient dashboard timeout as blocking when live rows are available", async () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          last_price: "313.10",
          last_price_at: now,
          is_price_stale: false,
          quote_status: "live",
          freshness_status: "fresh",
        }),
      ],
    });
    apiClientMock.dashboardMarketFeedSnapshot.mockRejectedValue(new Error("request_timeout"));

    await market.fetchOverview({ silent: true });

    expect(market.currentInstrument?.last_price).toBe("313.10");
    expect(market.feedErrors).toEqual([]);
    expect(market.feedWarnings).toContain("dashboard_refresh_retrying");
  });

  it("treats backend dashboard timeout snapshot as retry warning with usable data", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    const snapshot = feedSnapshot(
      [
        instrument({
          last_price: "313.10",
          last_price_at: now,
          is_price_stale: false,
          quote_status: "live",
          freshness_status: "fresh",
        }),
      ],
    );
    snapshot.errors = ["dashboard_market_feed_timeout"];
    snapshot.status.errors = ["dashboard_market_feed_timeout"];

    market.applyDashboardFeedSnapshot(snapshot);

    expect(market.feedErrors).toEqual([]);
    expect(market.feedWarnings).toContain("dashboard_refresh_retrying");
    expect(market.dashboardFeedStatus.errors).toEqual([]);
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

    expect(apiClientMock.dashboardMarketFeedSnapshot).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        selected_instrument: "MOEX:GAZP",
        include_order_book: true,
        include_trades: false,
      }),
    );
    expect(apiClientMock.dashboardMarketFeedSnapshot).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        selected_instrument: "MOEX:GAZP",
        include_order_book: false,
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
    openAuthenticatedWebSocketMock.mockResolvedValue(new FakeWebSocket());
    apiClientMock.dashboardMarketFeedSnapshot.mockResolvedValue(
      feedSnapshot([instrument({ last_price: "313.10", quote_status: "live" })]),
    );
    apiClientMock.dataShadowStatus.mockResolvedValue({
      enabled: true,
      collector_state: "stopped",
      day_collection_state: "inactive",
      daily_collection_active: false,
      current_window_state: "stopped",
      next_collection_window_at: null,
      remaining_windows_today: 0,
      collector_left_running: false,
      paused_at: null,
      completed_for_day_at: null,
      last_stop_reason: null,
      last_pause_reason: null,
      last_resume_at: null,
      last_window_completed_at: null,
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
      supervisor_enabled: false,
      supervisor_state: "not_configured",
      stream_restart_count: 0,
      last_restart_at: null,
      last_restart_reason: null,
      stream_stale_count: 0,
      last_stream_error: null,
      per_stream_status: {},
      warnings: [],
      warning: null,
    });

    market.startDashboardFeed();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(apiClientMock.dashboardMarketFeedSnapshot).toHaveBeenCalled();
    expect(openAuthenticatedWebSocketMock).toHaveBeenCalledWith(
      expect.stringContaining("/ws/market-feed?"),
    );
    expect(market.currentInstrument?.last_price).toBe("313.10");
    expect(market.dataShadowStatus.collector_state).toBe("stopped");
    market.stopDashboardFeed();
  });

  it("uses dashboard WebSocket snapshots as primary live feed without Start", async () => {
    const market = useMarketStore();
    const socket = new FakeWebSocket();
    openAuthenticatedWebSocketMock.mockResolvedValue(socket);
    apiClientMock.dashboardMarketFeedSnapshot.mockResolvedValue(feedSnapshot([]));
    apiClientMock.dataShadowStatus.mockResolvedValue({
      enabled: true,
      collector_state: "stopped",
      day_collection_state: "inactive",
      daily_collection_active: false,
      current_window_state: "stopped",
      next_collection_window_at: null,
      remaining_windows_today: 0,
      collector_left_running: false,
      paused_at: null,
      completed_for_day_at: null,
      last_stop_reason: null,
      last_pause_reason: null,
      last_resume_at: null,
      last_window_completed_at: null,
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
      supervisor_enabled: false,
      supervisor_state: "not_configured",
      stream_restart_count: 0,
      last_restart_at: null,
      last_restart_reason: null,
      stream_stale_count: 0,
      last_stream_error: null,
      per_stream_status: {},
      warnings: [],
      warning: null,
    });

    market.startDashboardFeed();
    await new Promise((resolve) => setTimeout(resolve, 0));
    socket.onmessage?.({
      data: JSON.stringify({
        type: "market.snapshot",
        payload: {
          data: feedSnapshot([
            instrument({
              instrument_id: "MOEX:SBER",
              ticker: "SBER",
              last_price: "313.10",
              quote_status: "live",
            }),
          ]),
        },
      }),
    } as MessageEvent<string>);

    expect(market.currentInstrument?.last_price).toBe("313.10");
    expect(market.liveConnection).toBe("live");
    market.stopDashboardFeed();
  });

  it("sends selected instrument changes over the market WebSocket", async () => {
    const market = useMarketStore();
    const socket = new FakeWebSocket();
    openAuthenticatedWebSocketMock.mockResolvedValue(socket);
    market.applyOverview({
      generated_at: new Date().toISOString(),
      instruments: [
        instrument({ instrument_id: "MOEX:SBER", ticker: "SBER" }),
        instrument({ instrument_id: "MOEX:GAZP", ticker: "GAZP" }),
      ],
    });

    await market.connectMarketSocket();
    market.selectedInstrumentId = "MOEX:GAZP";
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(JSON.parse(socket.sent[socket.sent.length - 1] ?? "{}")).toEqual({
      type: "market.select",
      selected_instrument: "MOEX:GAZP",
    });
  });

  it("does not keep old trade tape forever when freshness age is unknown", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          recent_market_trades: [{ price: "313.10" }],
          market_trades_source: "tbank_get_last_trades",
          market_trades_age_ms: null,
          trade_tape_status: "stale",
        }),
      ],
    });

    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          market_trades_source: "tbank_get_last_trades",
          market_trades_age_ms: null,
          trade_tape_status: "stale",
        }),
      ],
    });

    expect(market.currentInstrument?.recent_market_trades).toEqual([]);
  });

  it("drops preserved trade tape when backend marks it stale", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          recent_market_trades: [{ price: "75.66", exchange_ts: "2026-06-30T10:56:59Z" }],
          market_trades_source: "tbank_get_last_trades",
          market_trades_age_ms: 30_000,
          trade_tape_status: "stale",
          trade_tape_reason: "trade_exchange_ts_too_old",
        }),
      ],
    });

    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          market_trades_source: "tbank_get_last_trades",
          market_trades_age_ms: null,
          trade_tape_status: "stale",
          trade_tape_reason: "trade_exchange_ts_too_old",
        }),
      ],
    });

    expect(market.currentInstrument?.recent_market_trades).toEqual([]);
    expect(market.currentInstrument?.trade_tape_reason).toBe("trade_exchange_ts_too_old");
  });

  it("keeps short delayed GetLastTrades rows across an intermittent no-samples snapshot", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    const delayedTradeTs = new Date(Date.now() - 30_000).toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          recent_market_trades: [
            { price: "75.66", exchange_ts: delayedTradeTs, quantity_lots: "3", side: "buy" },
          ],
          market_trades_source: "tbank_get_last_trades",
          market_trades_age_ms: 30_000,
          trade_tape_status: "stale",
          trade_tape_reason: "trade_exchange_ts_too_old",
        }),
      ],
    });

    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          market_trades_source: "no_market_trades_samples",
          market_trades_age_ms: null,
          trade_tape_status: "no_market_trades_samples",
          trade_tape_reason: "no_market_trades_samples",
        }),
      ],
    });

    expect(market.currentInstrument?.recent_market_trades).toHaveLength(1);
    expect(market.currentInstrument?.market_trades_source).toBe("tbank_get_last_trades");
    expect(market.currentInstrument?.trade_tape_status).toBe("stale");
  });

  it("keeps fresh trade tape when an intermittent no-samples snapshot arrives", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          recent_market_trades: [
            { price: "307.61", exchange_ts: now, quantity_lots: "3", side: "buy" },
          ],
          market_trades_source: "market_trades_stream",
          market_trades_age_ms: 500,
          trade_tape_status: "live",
          trade_tape_reason: "fresh",
        }),
      ],
    });

    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          market_trades_source: "no_market_trades_samples",
          market_trades_age_ms: null,
          trade_tape_status: "no_market_trades_samples",
          trade_tape_reason: "no_market_trades_samples",
        }),
      ],
    });

    expect(market.currentInstrument?.recent_market_trades).toHaveLength(1);
    expect(market.currentInstrument?.market_trades_source).toBe("market_trades_stream");
    expect(market.currentInstrument?.trade_tape_status).toBe("live");
  });

  it("keeps a full fresh order book when a weaker top-of-book snapshot arrives", () => {
    const market = useMarketStore();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          last_price: "307.61",
          last_price_at: now,
          last_price_source: "live_exchange_order_book",
          is_price_stale: false,
          exchange_age_ms: 0,
          stale_by_exchange_time: false,
          freshness_status: "fresh",
          quote_source: "live_exchange_order_book",
          official_exchange_open: true,
          quote_status: "live",
          best_bid: "307.60",
          best_ask: "307.61",
          bid_depth_lots: "28517",
          ask_depth_lots: "135",
          order_book_source: "live_exchange_order_book",
          order_book_ts: now,
          order_book_age_ms: 300,
          order_book_stale: false,
          order_book_summary: {
            depth_levels: 20,
            bids: [
              { price: "307.60", quantity_lots: "10653" },
              { price: "307.59", quantity_lots: "6857" },
              { price: "307.58", quantity_lots: "1765" },
              { price: "307.57", quantity_lots: "304" },
              { price: "307.56", quantity_lots: "1452" },
            ],
            asks: [
              { price: "307.61", quantity_lots: "20" },
              { price: "307.62", quantity_lots: "25" },
              { price: "307.63", quantity_lots: "25" },
              { price: "307.64", quantity_lots: "30" },
              { price: "307.65", quantity_lots: "35" },
            ],
          },
        }),
      ],
    });

    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          last_price: "307.62",
          last_price_at: now,
          last_price_source: "live_exchange_order_book",
          is_price_stale: false,
          exchange_age_ms: 0,
          stale_by_exchange_time: false,
          freshness_status: "fresh",
          quote_source: "live_exchange_order_book",
          official_exchange_open: true,
          quote_status: "live",
          best_bid: "307.61",
          best_ask: "307.62",
          order_book_source: "live_exchange_order_book",
          order_book_ts: now,
          order_book_age_ms: 300,
          order_book_stale: false,
          order_book_summary: {
            depth_levels: 20,
            bids: [],
            asks: [],
          },
        }),
      ],
    });

    const summary = market.currentInstrument?.order_book_summary ?? {};
    expect(Array.isArray(summary.bids) ? summary.bids.length : 0).toBe(5);
    expect(Array.isArray(summary.asks) ? summary.asks.length : 0).toBe(5);
    expect(market.currentInstrument?.bid_depth_lots).toBe("28517");
  });

  it("does not keep a live exchange order book after the dashboard session closes", () => {
    const market = useMarketStore();
    const oldTs = new Date(Date.now() - 120_000).toISOString();
    const now = new Date().toISOString();
    market.applyOverview({
      generated_at: oldTs,
      instruments: [
        instrument({
          last_price: "307.61",
          last_price_at: oldTs,
          last_price_source: "live_exchange_order_book",
          is_price_stale: false,
          exchange_age_ms: 0,
          stale_by_exchange_time: false,
          freshness_status: "fresh",
          quote_source: "live_exchange_order_book",
          official_exchange_open: true,
          quote_allowed_for_data_collection: true,
          quote_status: "live",
          best_bid: "307.60",
          best_ask: "307.61",
          order_book_source: "live_exchange_order_book",
          order_book_ts: oldTs,
          order_book_age_ms: 300,
          order_book_stale: false,
          order_book_summary: {
            depth_levels: 20,
            bids: [{ price: "307.60", quantity_lots: "10653" }],
            asks: [{ price: "307.61", quantity_lots: "20" }],
          },
        }),
      ],
    });

    market.applyOverview({
      generated_at: now,
      instruments: [
        instrument({
          last_price: "308.05",
          last_price_at: now,
          last_price_source: "broker_indicative_quote",
          quote_source: "broker_indicative_quote",
          venue_type: "broker_indicative",
          trading_mode: "indicative_only",
          official_exchange_open: false,
          official_exchange_closed: true,
          quote_allowed_for_data_collection: false,
          quote_status: "indicative",
          best_bid: null,
          best_ask: null,
          order_book_source: null,
          order_book_ts: null,
          order_book_age_ms: null,
          order_book_stale: true,
          order_book_summary: {},
        }),
      ],
    });

    expect(market.currentInstrument?.quote_source).toBe("broker_indicative_quote");
    expect(market.currentInstrument?.order_book_source).toBeNull();
    expect(market.currentInstrument?.order_book_summary).toEqual({});
    expect(market.currentInstrument?.quote_allowed_for_data_collection).toBe(false);
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
          is_price_stale: false,
          exchange_age_ms: 0,
          stale_by_exchange_time: false,
          freshness_status: "fresh",
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

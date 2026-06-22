import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MarketInstrumentOverview } from "../api/types";
import { useMarketStore } from "../stores/market";

const apiClientMock = vi.hoisted(() => ({
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
    apiClientMock.marketOverview.mockRejectedValue(new Error("temporary API failure"));

    await market.fetchOverview({ silent: true });

    expect(market.currentInstrument?.last_price).toBe("313.10");
  });

  it("loads selected instrument details lazily", async () => {
    const market = useMarketStore();
    const gazp = instrument({ instrument_id: "MOEX:GAZP", ticker: "GAZP", last_price: "144.20" });
    market.applyOverview({ generated_at: new Date().toISOString(), instruments: [gazp] });
    apiClientMock.marketInstrumentDetails.mockResolvedValue({
      ...gazp,
      best_bid: "144.18",
      best_ask: "144.22",
    });
    market.selectedInstrumentId = "MOEX:GAZP";

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(apiClientMock.marketInstrumentDetails).toHaveBeenCalledWith("MOEX:GAZP");
    expect(market.currentInstrument?.best_bid).toBe("144.18");
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

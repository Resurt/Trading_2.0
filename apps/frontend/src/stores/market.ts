import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { apiClient, openAuthenticatedWebSocket } from "../api/client";
import type {
  ConnectionState,
  DataShadowStatusResponse,
  JsonPayload,
  MarketInstrumentOverview,
  MarketOverviewResponse,
  WebSocketEnvelope,
} from "../api/types";

const EMPTY_OVERVIEW: MarketOverviewResponse = {
  generated_at: new Date(0).toISOString(),
  instruments: [],
};

const EMPTY_DATA_SHADOW_STATUS: DataShadowStatusResponse = {
  enabled: false,
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
};

export const useMarketStore = defineStore("market", () => {
  const overview = ref<MarketOverviewResponse>(EMPTY_OVERVIEW);
  const dataShadowStatus = ref<DataShadowStatusResponse>(EMPTY_DATA_SHADOW_STATUS);
  const selectedInstrumentId = ref<string | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const liveConnection = ref<ConnectionState>("idle");
  let marketSocket: WebSocket | null = null;
  let marketPollTimer: number | null = null;

  const currentInstrument = computed<MarketInstrumentOverview | null>(() => {
    if (overview.value.instruments.length === 0) {
      return null;
    }
    return (
      overview.value.instruments.find(
        (instrument) => instrument.instrument_id === selectedInstrumentId.value,
      ) ?? overview.value.instruments[0]
    );
  });

  const topOfBook = computed(() => ({
    bestBid: currentInstrument.value?.best_bid ?? null,
    bestAsk: currentInstrument.value?.best_ask ?? null,
    spread: currentInstrument.value?.spread ?? null,
    midPrice: currentInstrument.value?.mid_price ?? null,
  }));

  const bookSummaryRows = computed(() =>
    Object.entries(currentInstrument.value?.order_book_summary ?? {}).map(([key, value]) => ({
      key,
      value: String(value ?? "Нет данных"),
    })),
  );

  const recentTrades = computed<JsonPayload[]>(() => currentInstrument.value?.recent_market_trades ?? []);

  const quoteRows = computed(() => overview.value.instruments);

  async function fetchOverview(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      applyOverview(await apiClient.marketOverview());
      if (!selectedInstrumentId.value && overview.value.instruments[0]) {
        selectedInstrumentId.value = overview.value.instruments[0].instrument_id;
      }
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Market overview failed";
    } finally {
      loading.value = false;
    }
  }

  async function refreshQuotes(): Promise<void> {
    try {
      applyOverview(await apiClient.refreshMarketQuotes());
    } catch (unknownError) {
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Market quote refresh failed";
      }
    }
  }

  function applyOverview(nextOverview: MarketOverviewResponse): void {
    if (!nextOverview.instruments.length) {
      return;
    }
    error.value = null;
    const previousByInstrument = new Map(
      overview.value.instruments.map((instrument) => [instrument.instrument_id, instrument]),
    );
    overview.value = {
      ...nextOverview,
      instruments: nextOverview.instruments.map((nextInstrument) =>
        mergeInstrumentOverview(previousByInstrument.get(nextInstrument.instrument_id), nextInstrument),
      ),
    };
    if (!selectedInstrumentId.value && overview.value.instruments[0]) {
      selectedInstrumentId.value = overview.value.instruments[0].instrument_id;
    }
  }

  async function fetchDataShadowStatus(): Promise<void> {
    try {
      dataShadowStatus.value = await apiClient.dataShadowStatus();
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Data shadow status failed";
      dataShadowStatus.value = EMPTY_DATA_SHADOW_STATUS;
    }
  }

  async function connectMarketSocket(): Promise<void> {
    if (marketSocket && marketSocket.readyState < WebSocket.CLOSING) {
      return;
    }
    liveConnection.value = "loading";
    try {
      marketSocket = await openAuthenticatedWebSocket("/ws/market");
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Market WS auth failed";
      liveConnection.value = "degraded";
      return;
    }
    marketSocket.onopen = () => {
      liveConnection.value = "live";
    };
    marketSocket.onmessage = (event: MessageEvent<string>) => {
      const envelope = JSON.parse(event.data) as WebSocketEnvelope<{ data?: MarketOverviewResponse }>;
      if (envelope.payload.data) {
        applyOverview(envelope.payload.data);
      }
    };
    marketSocket.onerror = () => {
      liveConnection.value = "degraded";
    };
    marketSocket.onclose = () => {
      liveConnection.value = liveConnection.value === "degraded" ? "degraded" : "snapshot_closed";
      marketSocket = null;
    };
  }

  function startMarketPolling(intervalMs = 15_000): void {
    if (marketPollTimer !== null) {
      return;
    }
    void refreshQuotes();
    window.setTimeout(() => {
      void fetchOverview();
    }, 1500);
    void fetchDataShadowStatus();
    marketPollTimer = window.setInterval(() => {
      void refreshQuotes();
      window.setTimeout(() => {
        void fetchOverview();
      }, 1500);
      void fetchDataShadowStatus();
    }, intervalMs);
  }

  function stopMarketPolling(): void {
    if (marketPollTimer === null) {
      return;
    }
    window.clearInterval(marketPollTimer);
    marketPollTimer = null;
  }

  return {
    overview,
    dataShadowStatus,
    selectedInstrumentId,
    loading,
    error,
    liveConnection,
    currentInstrument,
    quoteRows,
    topOfBook,
    bookSummaryRows,
    recentTrades,
    fetchOverview,
    refreshQuotes,
    applyOverview,
    fetchDataShadowStatus,
    connectMarketSocket,
    startMarketPolling,
    stopMarketPolling,
  };
});

function mergeInstrumentOverview(
  previous: MarketInstrumentOverview | undefined,
  next: MarketInstrumentOverview,
): MarketInstrumentOverview {
  if (!previous) {
    return next;
  }
  if (shouldKeepPreviousQuote(previous, next)) {
    return {
      ...next,
      ...quoteSnapshotFields(previous),
    };
  }
  return next;
}

function quoteSnapshotFields(instrument: MarketInstrumentOverview): Partial<MarketInstrumentOverview> {
  return {
    last_price: instrument.last_price,
    last_price_at: instrument.last_price_at,
    last_price_ts: instrument.last_price_ts,
    last_price_source: instrument.last_price_source,
    is_price_stale: instrument.is_price_stale,
    price_staleness_seconds: instrument.price_staleness_seconds,
    previous_close: instrument.previous_close,
    change_abs: instrument.change_abs,
    change_bps: instrument.change_bps,
    quote_status: instrument.quote_status,
    last_candle_timeframe: instrument.last_candle_timeframe,
    spread: instrument.spread,
    spread_abs: instrument.spread_abs,
    spread_bps: instrument.spread_bps,
    mid_price: instrument.mid_price,
    market_quality: instrument.market_quality,
    best_bid: instrument.best_bid,
    best_ask: instrument.best_ask,
    bid_depth_lots: instrument.bid_depth_lots,
    ask_depth_lots: instrument.ask_depth_lots,
    book_imbalance: instrument.book_imbalance,
    order_book_source: instrument.order_book_source,
    order_book_ts: instrument.order_book_ts,
    order_book_stale: instrument.order_book_stale,
    recent_market_trades: instrument.recent_market_trades,
    order_book_summary: instrument.order_book_summary,
    quote_payload: instrument.quote_payload,
  };
}

function shouldKeepPreviousQuote(
  previous: MarketInstrumentOverview,
  next: MarketInstrumentOverview,
): boolean {
  if (!previous.last_price) {
    return false;
  }
  if (!next.last_price) {
    return true;
  }
  const previousPriority = quoteSourcePriority(previous.last_price_source);
  const nextPriority = quoteSourcePriority(next.last_price_source);
  if (previousPriority > nextPriority) {
    return true;
  }
  if (previousPriority < nextPriority) {
    return false;
  }
  return quoteTimestamp(previous.last_price_at) > quoteTimestamp(next.last_price_at);
}

function quoteSourcePriority(source: string | null): number {
  if (source === "live_order_book_mid") {
    return 4;
  }
  if (source === "tbank_last_price") {
    return 3;
  }
  if (source === "latest_market_candle_close") {
    return 1;
  }
  if (source === "previous_close") {
    return 0;
  }
  return 0;
}

function quoteTimestamp(value: string | null): number {
  if (!value) {
    return 0;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

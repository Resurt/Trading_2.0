import { computed, ref, watch } from "vue";
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
  let quoteRefreshTimer: number | null = null;
  let overviewInFlight = false;
  let quoteRefreshInFlight = false;
  let selectedDetailsInFlight = false;
  let dataShadowStatusInFlight = false;

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

  async function fetchOverview(options: { silent?: boolean } = {}): Promise<void> {
    if (overviewInFlight) {
      return;
    }
    overviewInFlight = true;
    if (!options.silent) {
      loading.value = true;
    }
    error.value = null;
    try {
      applyOverview(await apiClient.marketOverview());
      if (!selectedInstrumentId.value && overview.value.instruments[0]) {
        selectedInstrumentId.value = overview.value.instruments[0].instrument_id;
      }
    } catch (unknownError) {
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Market overview failed";
      }
    } finally {
      if (!options.silent) {
        loading.value = false;
      }
      overviewInFlight = false;
    }
  }

  async function refreshQuotes(): Promise<void> {
    if (quoteRefreshInFlight) {
      return;
    }
    quoteRefreshInFlight = true;
    try {
      applyOverview(await apiClient.refreshMarketQuotes({ details: false }));
      await refreshSelectedInstrumentDetails();
    } catch (unknownError) {
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Market quote refresh failed";
      }
    } finally {
      quoteRefreshInFlight = false;
    }
  }

  async function refreshSelectedInstrumentDetails(): Promise<void> {
    const instrument = currentInstrument.value;
    const ticker = instrument?.ticker ?? instrument?.instrument_id?.replace(/^MOEX:/, "");
    if (!ticker || selectedDetailsInFlight) {
      return;
    }
    selectedDetailsInFlight = true;
    try {
      applyOverview(
        await apiClient.refreshMarketQuotes({
          instruments: ticker,
          details: true,
        }),
      );
    } catch (unknownError) {
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Selected market details failed";
      }
    } finally {
      selectedDetailsInFlight = false;
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
    if (dataShadowStatusInFlight) {
      return;
    }
    dataShadowStatusInFlight = true;
    try {
      dataShadowStatus.value = await apiClient.dataShadowStatus();
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Data shadow status failed";
    } finally {
      dataShadowStatusInFlight = false;
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

  function startMarketPolling(intervalMs = 5_000, quoteRefreshIntervalMs = 30_000): void {
    if (marketPollTimer !== null) {
      return;
    }
    void refreshQuotes();
    void fetchOverview({ silent: true });
    void fetchDataShadowStatus();
    marketPollTimer = window.setInterval(() => {
      void fetchOverview({ silent: true });
      void fetchDataShadowStatus();
    }, intervalMs);
    quoteRefreshTimer = window.setInterval(() => {
      void refreshQuotes();
    }, quoteRefreshIntervalMs);
  }

  function stopMarketPolling(): void {
    if (marketPollTimer === null) {
      return;
    }
    window.clearInterval(marketPollTimer);
    marketPollTimer = null;
    if (quoteRefreshTimer !== null) {
      window.clearInterval(quoteRefreshTimer);
      quoteRefreshTimer = null;
    }
  }

  watch(selectedInstrumentId, () => {
    void refreshSelectedInstrumentDetails();
  });

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
    refreshSelectedInstrumentDetails,
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
  return withPreservedRecentTrades(previous, next);
}

function withPreservedRecentTrades(
  previous: MarketInstrumentOverview,
  next: MarketInstrumentOverview,
): MarketInstrumentOverview {
  if ((next.recent_market_trades?.length ?? 0) > 0) {
    return next;
  }
  if ((previous.recent_market_trades?.length ?? 0) === 0) {
    return next;
  }
  if (!isTradeTapeStillFresh(previous)) {
    return next;
  }
  return {
    ...next,
    recent_market_trades: previous.recent_market_trades,
    market_trades_source: previous.market_trades_source,
    market_trades_age_ms: previous.market_trades_age_ms,
  };
}

function isTradeTapeStillFresh(instrument: MarketInstrumentOverview): boolean {
  if (instrument.market_trades_age_ms === null || instrument.market_trades_age_ms === undefined) {
    return true;
  }
  return Number(instrument.market_trades_age_ms) <= 30 * 60 * 1000;
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
    quote_source: instrument.quote_source,
    quote_allowed_for_data_collection: instrument.quote_allowed_for_data_collection,
    quote_allowed_for_display: instrument.quote_allowed_for_display,
    venue_type: instrument.venue_type,
    trading_mode: instrument.trading_mode,
    official_exchange_open: instrument.official_exchange_open,
    official_exchange_closed: instrument.official_exchange_closed,
    last_candle_timeframe: instrument.last_candle_timeframe,
    spread: instrument.spread,
    spread_abs: instrument.spread_abs,
    spread_bps: instrument.spread_bps,
    spread_abs_rub: instrument.spread_abs_rub,
    spread_units_validated: instrument.spread_units_validated,
    mid_price: instrument.mid_price,
    market_quality: instrument.market_quality,
    market_quality_score: instrument.market_quality_score,
    display_market_quality_score: instrument.display_market_quality_score,
    calibration_market_quality_score: instrument.calibration_market_quality_score,
    market_quality_label: instrument.market_quality_label,
    market_quality_components: instrument.market_quality_components,
    best_bid: instrument.best_bid,
    best_ask: instrument.best_ask,
    bid_depth_lots: instrument.bid_depth_lots,
    ask_depth_lots: instrument.ask_depth_lots,
    book_imbalance: instrument.book_imbalance,
    order_book_source: instrument.order_book_source,
    order_book_ts: instrument.order_book_ts,
    order_book_age_ms: instrument.order_book_age_ms,
    order_book_stale: instrument.order_book_stale,
    recent_market_trades: instrument.recent_market_trades,
    market_trades_source: instrument.market_trades_source,
    market_trades_age_ms: instrument.market_trades_age_ms,
    reason_code: instrument.reason_code,
    warning: instrument.warning,
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
  if (next.official_exchange_closed !== previous.official_exchange_closed) {
    return false;
  }
  if (next.quote_source === "broker_quote_exchange_closed") {
    return false;
  }
  if (!isQuoteSnapshotStillFresh(previous)) {
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

function isQuoteSnapshotStillFresh(instrument: MarketInstrumentOverview): boolean {
  if (
    ![
      "live_order_book_mid",
      "tbank_last_price",
      "live_exchange_order_book",
      "live_exchange_last_price",
      "broker_quote_exchange_closed",
      "broker_otc_order_book",
      "broker_indicative_quote",
    ].includes(instrument.last_price_source ?? "")
  ) {
    return true;
  }
  return Date.now() - quoteTimestamp(instrument.last_price_at) <= 60_000;
}

function quoteSourcePriority(source: string | null): number {
  if (source === "live_exchange_order_book" || source === "live_order_book_mid") {
    return 6;
  }
  if (source === "live_exchange_last_price") {
    return 5;
  }
  if (source === "broker_quote_exchange_closed" || source === "broker_otc_order_book") {
    return 4;
  }
  if (source === "broker_indicative_quote" || source === "tbank_last_price") {
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

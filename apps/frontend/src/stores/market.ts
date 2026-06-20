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
  strategy_trading_disabled: true,
  real_orders_disabled: true,
  stream_alive: false,
  last_message_age_seconds: null,
  candles_received: null,
  order_book_snapshots: 0,
  market_microstructure_snapshots: 0,
  avg_spread_bps: null,
  p95_spread_bps: null,
  avg_market_quality_score: null,
  current_session: null,
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
      overview.value = await apiClient.marketOverview();
      if (!selectedInstrumentId.value && overview.value.instruments[0]) {
        selectedInstrumentId.value = overview.value.instruments[0].instrument_id;
      }
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Market overview failed";
      overview.value = EMPTY_OVERVIEW;
    } finally {
      loading.value = false;
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
        overview.value = envelope.payload.data;
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

  function startMarketPolling(intervalMs = 30_000): void {
    if (marketPollTimer !== null) {
      return;
    }
    marketPollTimer = window.setInterval(() => {
      void fetchOverview();
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
    fetchDataShadowStatus,
    connectMarketSocket,
    startMarketPolling,
    stopMarketPolling,
  };
});

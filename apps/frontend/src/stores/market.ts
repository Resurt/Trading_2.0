import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { apiClient, websocketUrl } from "../api/client";
import type {
  ConnectionState,
  JsonPayload,
  MarketInstrumentOverview,
  MarketOverviewResponse,
  WebSocketEnvelope,
} from "../api/types";

const EMPTY_OVERVIEW: MarketOverviewResponse = {
  generated_at: new Date(0).toISOString(),
  instruments: [],
};

export const useMarketStore = defineStore("market", () => {
  const overview = ref<MarketOverviewResponse>(EMPTY_OVERVIEW);
  const selectedInstrumentId = ref<string | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const liveConnection = ref<ConnectionState>("idle");
  let marketSocket: WebSocket | null = null;

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

  function connectMarketSocket(): void {
    if (marketSocket && marketSocket.readyState < WebSocket.CLOSING) {
      return;
    }
    liveConnection.value = "loading";
    marketSocket = new WebSocket(websocketUrl("/ws/market"));
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

  return {
    overview,
    selectedInstrumentId,
    loading,
    error,
    liveConnection,
    currentInstrument,
    topOfBook,
    bookSummaryRows,
    recentTrades,
    fetchOverview,
    connectMarketSocket,
  };
});

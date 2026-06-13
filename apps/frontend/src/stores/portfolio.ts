import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { apiClient, websocketUrl } from "../api/client";
import type { ConnectionState, OrderResponse, PositionResponse, WebSocketEnvelope } from "../api/types";

export const usePortfolioStore = defineStore("portfolio", () => {
  const positions = ref<PositionResponse[]>([]);
  const openOrders = ref<OrderResponse[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const liveConnection = ref<ConnectionState>("idle");
  let ordersSocket: WebSocket | null = null;

  const activePositions = computed(() => positions.value.filter((position) => position.qty_lots !== 0));
  const ordersWithReason = computed(() =>
    openOrders.value.filter((order) => order.cancel_reason_code || order.reject_reason_code),
  );

  async function fetchSnapshot(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const [nextPositions, nextOpenOrders] = await Promise.all([
        apiClient.positions(),
        apiClient.openOrders(),
      ]);
      positions.value = nextPositions;
      openOrders.value = nextOpenOrders;
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Portfolio snapshot failed";
      positions.value = [];
      openOrders.value = [];
    } finally {
      loading.value = false;
    }
  }

  function connectOrdersSocket(): void {
    if (ordersSocket && ordersSocket.readyState < WebSocket.CLOSING) {
      return;
    }
    liveConnection.value = "loading";
    ordersSocket = new WebSocket(websocketUrl("/ws/orders"));
    ordersSocket.onopen = () => {
      liveConnection.value = "live";
    };
    ordersSocket.onmessage = (event: MessageEvent<string>) => {
      const envelope = JSON.parse(event.data) as WebSocketEnvelope<{ data?: { orders?: OrderResponse[] } }>;
      if (envelope.payload.data?.orders) {
        openOrders.value = envelope.payload.data.orders;
      }
    };
    ordersSocket.onerror = () => {
      liveConnection.value = "degraded";
    };
    ordersSocket.onclose = () => {
      liveConnection.value = liveConnection.value === "degraded" ? "degraded" : "snapshot_closed";
      ordersSocket = null;
    };
  }

  return {
    positions,
    openOrders,
    loading,
    error,
    liveConnection,
    activePositions,
    ordersWithReason,
    fetchSnapshot,
    connectOrdersSocket,
  };
});

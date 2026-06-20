import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { apiClient, openAuthenticatedWebSocket } from "../api/client";
import type {
  ConnectionState,
  DashboardSnapshotPayload,
  RobotStatusResponse,
  SessionSnapshotResponse,
  SignalResponse,
  WebSocketEnvelope,
} from "../api/types";

const EMPTY_STATUS: RobotStatusResponse = {
  balance: {
    currency: "RUB",
    available: "0",
    blocked: "0",
    total_portfolio_value_rub: null,
    available_cash_rub: null,
    blocked_cash_rub: null,
    expected_yield_rub: null,
    free_collateral_rub: null,
    account_id_masked: null,
    account_type: null,
    account_status: null,
    balance_currency: "RUB",
    last_balance_refresh_at: null,
    balance_freshness_seconds: null,
    balance_degraded: true,
    balance_degraded_reason_code: "api_snapshot_unavailable",
  },
  active_instruments: [],
  active_timeframes: [],
  strategy_state: "unknown",
  session_type: "unknown",
  session_phase: "closed",
  broker_trading_status: "unknown",
  open_orders_count: 0,
  active_positions_count: 0,
  degraded_flags: ["api_snapshot_unavailable"],
  robot_control_state: "stopped",
  micro_session_id: null,
};

const EMPTY_SESSION: SessionSnapshotResponse = {
  calendar_date: null,
  trading_date: null,
  session_type: "unknown",
  session_phase: "closed",
  micro_session_id: null,
  broker_trading_status: "unknown",
  observed_at: null,
};

export const useRobotStore = defineStore("robot", () => {
  const status = ref<RobotStatusResponse>(EMPTY_STATUS);
  const session = ref<SessionSnapshotResponse>(EMPTY_SESSION);
  const signals = ref<SignalResponse[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const liveConnection = ref<ConnectionState>("idle");
  const lastDashboardMessageAt = ref<string | null>(null);
  let dashboardSocket: WebSocket | null = null;

  const currentSignal = computed(() => signals.value[0] ?? null);
  const currentBlockerCode = computed(() => currentSignal.value?.final_blocker_code ?? null);
  const degraded = computed(() => status.value.degraded_flags.length > 0 || error.value !== null);

  async function fetchInitialSnapshot(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const [nextStatus, nextSession, nextSignals] = await Promise.all([
        apiClient.robotStatus(),
        apiClient.currentSession(),
        apiClient.currentSignals(),
      ]);
      status.value = nextStatus;
      session.value = nextSession;
      signals.value = nextSignals;
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "API snapshot failed";
      status.value = {
        ...EMPTY_STATUS,
        degraded_flags: ["api_snapshot_unavailable"],
      };
      session.value = EMPTY_SESSION;
      signals.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function connectDashboardSocket(): Promise<void> {
    if (dashboardSocket && dashboardSocket.readyState < WebSocket.CLOSING) {
      return;
    }
    liveConnection.value = "loading";
    try {
      dashboardSocket = await openAuthenticatedWebSocket("/ws/dashboard");
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Dashboard WS auth failed";
      liveConnection.value = "degraded";
      return;
    }

    dashboardSocket.onopen = () => {
      liveConnection.value = "live";
    };
    dashboardSocket.onmessage = (event: MessageEvent<string>) => {
      const envelope = JSON.parse(event.data) as WebSocketEnvelope<DashboardSnapshotPayload>;
      lastDashboardMessageAt.value = envelope.ts_utc;
      applyDashboardSnapshot(envelope.payload);
    };
    dashboardSocket.onerror = () => {
      liveConnection.value = "degraded";
    };
    dashboardSocket.onclose = () => {
      liveConnection.value = liveConnection.value === "degraded" ? "degraded" : "snapshot_closed";
      dashboardSocket = null;
    };
  }

  function applyDashboardSnapshot(payload: DashboardSnapshotPayload): void {
    if (payload.data?.robot_status) {
      status.value = payload.data.robot_status;
      session.value = {
        ...session.value,
        session_type: payload.data.robot_status.session_type,
        session_phase: payload.data.robot_status.session_phase,
        micro_session_id: payload.data.robot_status.micro_session_id,
        broker_trading_status: payload.data.robot_status.broker_trading_status,
      };
    }
    if (payload.data?.signals) {
      signals.value = payload.data.signals;
    }
  }

  async function startRobot(): Promise<void> {
    await apiClient.startRobot();
    await fetchInitialSnapshot();
  }

  async function stopRobot(): Promise<void> {
    await apiClient.stopRobot();
    await fetchInitialSnapshot();
  }

  return {
    status,
    session,
    signals,
    loading,
    error,
    liveConnection,
    lastDashboardMessageAt,
    currentSignal,
    currentBlockerCode,
    degraded,
    fetchInitialSnapshot,
    connectDashboardSocket,
    applyDashboardSnapshot,
    startRobot,
    stopRobot,
  };
});

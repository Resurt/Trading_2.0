import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { apiClient, openAuthenticatedWebSocket } from "../api/client";
import type {
  ConnectionState,
  DashboardSnapshotPayload,
  PortfolioSummaryResponse,
  RobotCommandResponse,
  RobotStatusResponse,
  SessionPreflightResponse,
  SessionSnapshotResponse,
  SignalResponse,
  WebSocketEnvelope,
} from "../api/types";
import { useMarketStore } from "./market";

const CORE_UNIVERSE = "SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR";

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
    balance_degraded_reason_code: "broker_balance_unavailable",
  },
  active_instruments: [],
  active_timeframes: [],
  strategy_state: "unknown",
  session_type: "unknown",
  session_phase: "closed",
  broker_trading_status: "unknown",
  open_orders_count: 0,
  active_positions_count: 0,
  degraded_flags: ["balance_unavailable"],
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
  const snapshotWarnings = ref<string[]>([]);
  const liveConnection = ref<ConnectionState>("idle");
  const lastDashboardMessageAt = ref<string | null>(null);
  const lastCommandStatus = ref<string | null>(null);
  const lastCommandMessage = ref<string | null>(null);
  const lastCommandReasonCode = ref<string | null>(null);
  const lastCommandAt = ref<string | null>(null);
  const lastCommandNextSessionAt = ref<string | null>(null);
  const startLoading = ref(false);
  const stopLoading = ref(false);
  const commandPhase = ref<string | null>(null);
  const commandLoading = computed(() => startLoading.value || stopLoading.value);
  const balanceRefreshLoading = ref(false);
  const lastSessionPreflight = ref<SessionPreflightResponse | null>(null);
  let dashboardSocket: WebSocket | null = null;
  let balancePollTimer: number | null = null;
  let snapshotInFlight = false;

  const currentSignal = computed(() => signals.value[0] ?? null);
  const currentBlockerCode = computed(() => currentSignal.value?.final_blocker_code ?? null);
  const degraded = computed(() => status.value.degraded_flags.length > 0 || error.value !== null);

  async function fetchInitialSnapshot(): Promise<void> {
    if (snapshotInFlight) {
      return;
    }
    snapshotInFlight = true;
    loading.value = true;
    error.value = null;
    snapshotWarnings.value = [];
    try {
      const dashboard = await apiClient.dashboardState();
      applyDashboardSnapshot(dashboard);
      loading.value = false;
      snapshotInFlight = false;
      return;
    } catch (unknownError) {
      snapshotWarnings.value.push(`dashboard_state_unavailable: ${errorMessage(unknownError)}`);
    }
    const [statusResult, sessionResult, signalsResult, portfolioResult] =
      await Promise.allSettled([
        apiClient.robotStatus(),
        apiClient.currentSession(),
        apiClient.currentSignals(),
        apiClient.portfolioSummary(),
      ]);

    if (statusResult.status === "fulfilled") {
      status.value = statusResult.value;
    } else {
      snapshotWarnings.value.push(`robot_status_unavailable: ${errorMessage(statusResult.reason)}`);
      status.value = {
        ...status.value,
        degraded_flags: Array.from(new Set([...status.value.degraded_flags, "dashboard_unavailable"])),
      };
    }

    if (sessionResult.status === "fulfilled") {
      session.value = sessionResult.value;
    } else {
      snapshotWarnings.value.push(`session_snapshot_unavailable: ${errorMessage(sessionResult.reason)}`);
      if (statusResult.status !== "fulfilled") {
        session.value = EMPTY_SESSION;
      }
    }
    if (signalsResult.status === "fulfilled") {
      signals.value = signalsResult.value;
    } else {
      snapshotWarnings.value.push(`signals_unavailable: ${errorMessage(signalsResult.reason)}`);
      signals.value = [];
    }

    if (portfolioResult.status === "fulfilled") {
      applyPortfolioSummary(portfolioResult.value);
    } else {
      snapshotWarnings.value.push(
        `balance_summary_unavailable: ${errorMessage(portfolioResult.reason)}`,
      );
    }

    error.value = snapshotWarnings.value.length ? snapshotWarnings.value.join("; ") : null;
    loading.value = false;
    snapshotInFlight = false;
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
    if (payload.data?.robot_status || payload.data?.signals || payload.data?.session_preflight) {
      error.value = null;
      snapshotWarnings.value = [];
    }
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
    if (payload.data?.session_preflight) {
      applySessionPreflight(payload.data.session_preflight);
    }
    if (lastSessionPreflight.value) {
      applySessionPreflight(lastSessionPreflight.value);
    }
  }

  function applySessionPreflight(preflight: SessionPreflightResponse): void {
    lastSessionPreflight.value = preflight;
    const brokerStatus =
      preflight.broker_trading_status !== "unknown" || status.value.broker_trading_status === "unknown"
        ? preflight.broker_trading_status
        : status.value.broker_trading_status;
    const microSessionId = preflight.market_open
      ? (status.value.micro_session_id ?? session.value.micro_session_id)
      : null;
    session.value = {
      calendar_date: preflight.calendar_date,
      trading_date: preflight.trading_date,
      session_type: preflight.session_type,
      session_phase: preflight.session_phase,
      micro_session_id: microSessionId,
      broker_trading_status: brokerStatus,
      observed_at: preflight.now_msk,
    };
    status.value = {
      ...status.value,
      session_type: preflight.session_type,
      session_phase: preflight.session_phase,
      broker_trading_status: brokerStatus,
      micro_session_id: microSessionId,
    };
  }

  async function startRobot(): Promise<void> {
    startLoading.value = true;
    commandPhase.value = "preflight";
    setCommandState({
      status: "checking_preflight",
      message: "Проверяю торговую сессию. Data-only сбор пока не запускается, заявки не выставляются.",
      reasonCode: "session_preflight_running",
    });
    try {
      const preflight = await apiClient.sessionPreflight({
        instruments: CORE_UNIVERSE,
        mode: "data_shadow",
        cache: false,
      });
      applySessionPreflight(preflight);
      if (!preflight.market_open || !preflight.data_only_collection_allowed) {
        setCommandFromPreflight(preflight);
        return;
      }
      commandPhase.value = "start_command";
      setCommandState({
        status: "start_requesting",
        message: "Запускаем data-only сбор. Торговля отключена, заявки не выставляются.",
        reasonCode: "data_only_start_requested",
      });
      const response = await apiClient.startRobot({
        mode: "data_shadow",
        reason: "frontend_operator_data_only_start",
        instruments: CORE_UNIVERSE,
        requested_instruments: CORE_UNIVERSE,
        real_orders_disabled: true,
        strategy_trading_disabled: true,
        preflight_result: preflight,
      });
      setCommandFromResponse(response);
      void pollDataShadowStatusUntilSettled();
    } catch (unknownError) {
      setCommandState({
        status: "preflight_failed",
        message: `Не удалось проверить торговую сессию. Сбор не запущен. Причина: preflight_unavailable. ${errorMessage(unknownError)}`,
        reasonCode: "preflight_unavailable",
      });
    } finally {
      startLoading.value = false;
      commandPhase.value = null;
      void fetchInitialSnapshot();
    }
  }

  async function stopRobot(): Promise<void> {
    stopLoading.value = true;
    commandPhase.value = "stop_command";
    setCommandState({
      status: "stop_requesting",
      message: "Остановка запрошена. Реальные заявки не трогаю.",
      reasonCode: "controlled_stop_requested",
    });
    try {
      const response = await apiClient.stopRobot();
      setCommandFromResponse({
        ...response,
        message: response.message || "Остановка запрошена. Жду подтверждение runtime.",
      });
      void pollDataShadowStatusUntilSettled();
    } catch (unknownError) {
      setCommandState({
        status: "stop_failed",
        message: `Остановка не подтверждена: ${errorMessage(unknownError)}`,
        reasonCode: "stop_command_failed",
      });
    } finally {
      stopLoading.value = false;
      commandPhase.value = null;
      void fetchInitialSnapshot();
    }
  }

  async function refreshBalance(options: { silent?: boolean } = {}): Promise<void> {
    if (balanceRefreshLoading.value) {
      return;
    }
    balanceRefreshLoading.value = true;
    try {
      const summary = await apiClient.refreshPortfolio();
      applyPortfolioSummary(summary);
      if (!options.silent) {
        setCommandState({
          status: summary.balance.balance_degraded
            ? "balance_refresh_degraded"
            : "balance_refresh_completed",
          message: summary.balance.balance_degraded
            ? `Баланс не получен: ${summary.balance.balance_degraded_reason_code ?? "broker_balance_unavailable"}`
            : "Баланс обновлен",
          reasonCode: summary.balance.balance_degraded
            ? summary.balance.balance_degraded_reason_code
            : "balance_refresh_completed",
        });
      }
    } catch (unknownError) {
      if (!options.silent) {
        setCommandState({
          status: "balance_refresh_failed",
          message: `Баланс недоступен: ${errorMessage(unknownError)}`,
          reasonCode: "broker_balance_refresh_failed",
        });
      }
    } finally {
      balanceRefreshLoading.value = false;
    }
  }

  function startBalancePolling(intervalMs = 60_000): void {
    if (balancePollTimer !== null) {
      return;
    }
    window.setTimeout(() => {
      void refreshBalance({ silent: true });
    }, 500);
    balancePollTimer = window.setInterval(() => {
      void refreshBalance({ silent: true });
    }, intervalMs);
  }

  function stopBalancePolling(): void {
    if (balancePollTimer === null) {
      return;
    }
    window.clearInterval(balancePollTimer);
    balancePollTimer = null;
  }

  function applyPortfolioSummary(summary: PortfolioSummaryResponse): void {
    status.value = {
      ...status.value,
      balance: summary.balance,
      degraded_flags: summary.balance.balance_degraded
        ? Array.from(new Set([...status.value.degraded_flags, "balance_unavailable"]))
        : status.value.degraded_flags.filter((flag) => flag !== "balance_unavailable"),
    };
  }

  function setCommandFromPreflight(preflight: SessionPreflightResponse): void {
    const nextSession = preflight.next_session_at
      ? ` Следующая сессия: ${preflight.next_session_at}.`
      : "";
    const prefix =
      preflight.market_closed_expected || !preflight.market_open
        ? "Сбор не запущен: рынок закрыт или нет торгового окна."
        : "Сбор не запущен: data-only collection запрещён preflight.";
    setCommandState({
      status: "blocked_by_preflight",
      message: `${prefix} Причина: ${preflight.reason_code}.${nextSession}`,
      reasonCode: preflight.reason_code,
      nextSessionAt: preflight.next_session_at,
    });
  }

  function setCommandFromResponse(response: RobotCommandResponse): void {
    const preflight = response.preflight_result as Partial<SessionPreflightResponse> | null;
    setCommandState({
      status: response.status,
      message: commandMessageFromResponse(response),
      reasonCode: response.reason_code,
      nextSessionAt: preflight?.next_session_at ?? null,
    });
  }

  function setCommandState(payload: {
    status: string;
    message: string;
    reasonCode?: string | null;
    nextSessionAt?: string | null;
  }): void {
    lastCommandStatus.value = payload.status;
    lastCommandMessage.value = payload.message;
    lastCommandReasonCode.value = payload.reasonCode ?? null;
    lastCommandNextSessionAt.value = payload.nextSessionAt ?? null;
    lastCommandAt.value = new Date().toISOString();
  }

  function errorMessage(value: unknown): string {
    return value instanceof Error ? value.message : String(value);
  }

  function commandMessageFromResponse(response: RobotCommandResponse): string {
    if (!response.accepted) {
      return response.message || "Команда отклонена preflight.";
    }
    if (response.command === "start" && response.status === "requested") {
      return "Команда Start отправлена в trade-core. Data-only режим активируется без торговли и без заявок.";
    }
    if (response.command === "stop" && response.status === "requested") {
      return "Команда Stop отправлена в trade-core. Collector должен перейти в controlled stop.";
    }
    return response.message || "Команда принята.";
  }

  async function pollDataShadowStatusUntilSettled(): Promise<void> {
    const market = useMarketStore();
    for (let attempt = 0; attempt < 8; attempt += 1) {
      await market.fetchDataShadowStatus();
      const state = market.dataShadowStatus.collector_state;
      if (
        state === "collecting" ||
        state === "stopped_by_operator" ||
        state === "preflight_blocked" ||
        state === "degraded"
      ) {
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  }

  return {
    status,
    session,
    signals,
    loading,
    error,
    snapshotWarnings,
    liveConnection,
    lastDashboardMessageAt,
    lastSessionPreflight,
    lastCommandStatus,
    lastCommandMessage,
    lastCommandReasonCode,
    lastCommandAt,
    lastCommandNextSessionAt,
    startLoading,
    stopLoading,
    commandLoading,
    commandPhase,
    balanceRefreshLoading,
    currentSignal,
    currentBlockerCode,
    degraded,
    fetchInitialSnapshot,
    connectDashboardSocket,
    applyDashboardSnapshot,
    startRobot,
    stopRobot,
    refreshBalance,
    startBalancePolling,
    stopBalancePolling,
  };
});

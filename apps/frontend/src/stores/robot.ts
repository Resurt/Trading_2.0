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

const CORE_UNIVERSE = "SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T";
const COMMAND_AUTO_DISMISS_MS = 12_000;

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
  const lastRuntimeStatusAt = ref<string | null>(null);
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
  let statusPollTimer: number | null = null;
  let commandDismissTimer: number | null = null;
  let snapshotInFlight = false;
  let statusSnapshotInFlight = false;

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

  async function fetchRuntimeStatusSnapshot(): Promise<void> {
    if (statusSnapshotInFlight || snapshotInFlight) {
      return;
    }
    statusSnapshotInFlight = true;
    const previousWarnings = snapshotWarnings.value.filter(
      (warning) =>
        !warning.startsWith("robot_status_unavailable:") &&
        !warning.startsWith("session_snapshot_unavailable:"),
    );
    try {
      const [statusResult, sessionResult] = await Promise.allSettled([
        apiClient.robotStatus(),
        apiClient.currentSession(),
      ]);
      const nextWarnings = [...previousWarnings];
      if (statusResult.status === "fulfilled") {
        status.value = statusResult.value;
        lastRuntimeStatusAt.value = new Date().toISOString();
      } else {
        nextWarnings.push(`robot_status_unavailable: ${errorMessage(statusResult.reason)}`);
      }
      if (sessionResult.status === "fulfilled") {
        session.value = sessionResult.value;
        lastRuntimeStatusAt.value = new Date().toISOString();
      } else {
        nextWarnings.push(`session_snapshot_unavailable: ${errorMessage(sessionResult.reason)}`);
      }
      snapshotWarnings.value = nextWarnings;
      error.value = nextWarnings.length ? nextWarnings.join("; ") : null;
    } finally {
      statusSnapshotInFlight = false;
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
    if (payload.data?.robot_status || payload.data?.signals || payload.data?.session_preflight) {
      error.value = null;
      snapshotWarnings.value = [];
    }
    if (payload.data?.robot_status) {
      status.value = payload.data.robot_status;
      useMarketStore().applyRobotRuntimeStatus(payload.data.robot_status);
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
    commandPhase.value = "start_command";
    setCommandState({
      status: "preflight_pending",
      message: "Запуск сбора логов запрошен. Проверяю сессию...",
      reasonCode: "preflight_pending",
    });
    let advisoryPreflight: SessionPreflightResponse | null = null;
    try {
      advisoryPreflight = await apiClient.sessionPreflightFast({
        instruments: CORE_UNIVERSE,
        mode: "data_shadow",
        cache: true,
      });
      applySessionPreflight(advisoryPreflight);
    } catch {
      advisoryPreflight = null;
      setCommandState({
        status: "preflight_pending",
        message: "Брокер временно не ответил, повторяю проверку...",
        reasonCode: "preflight_retrying",
      });
    }
    try {
      commandPhase.value = "start_command";
      setCommandState({
        status: "start_requesting",
        message: "Запускаю сбор логов.",
        reasonCode: "data_only_start_requested",
      });
      const response = await apiClient.startRobot({
        mode: "data_shadow",
        reason: "frontend_operator_data_only_start",
        instruments: CORE_UNIVERSE,
        requested_instruments: CORE_UNIVERSE,
        real_orders_disabled: true,
        strategy_trading_disabled: true,
        preflight_result: advisoryPreflight,
      });
      setCommandFromResponse(response);
      if (!commandResponseIsFinal(response)) {
        void pollDataShadowStatusUntilSettled();
      }
    } catch (unknownError) {
      setCommandState({
        status: "start_command_failed",
        message: `Не удалось отправить команду Start: ${errorMessage(unknownError)}`,
        reasonCode: "start_command_unavailable",
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
    const market = useMarketStore();
    market.markDataShadowStopping();
    setCommandState({
      status: "stop_requesting",
      message: "Останавливаю сбор логов.",
      reasonCode: "controlled_stop_requested",
    });
    try {
      const response = await apiClient.stopRobot();
      setCommandFromResponse({
        ...response,
        message: response.message || "Сбор логов остановлен.",
      });
      if (response.accepted) {
        market.markDataShadowStopped(response.command_id);
      } else {
        void market.fetchDataShadowStatus();
      }
      void pollDataShadowStatusUntilSettled();
    } catch (unknownError) {
      void market.fetchDataShadowStatus();
      setCommandState({
        status: "stop_failed",
        message: `Сбор логов не остановлен: ${errorMessage(unknownError)}`,
        reasonCode: "stop_command_failed",
        autoDismissMs: COMMAND_AUTO_DISMISS_MS,
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

  function startStatusPolling(intervalMs = 5_000): void {
    if (statusPollTimer !== null) {
      return;
    }
    void fetchRuntimeStatusSnapshot();
    statusPollTimer = window.setInterval(() => {
      void fetchRuntimeStatusSnapshot();
    }, intervalMs);
  }

  function stopStatusPolling(): void {
    if (statusPollTimer === null) {
      return;
    }
    window.clearInterval(statusPollTimer);
    statusPollTimer = null;
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
    setCommandState({
      status: "blocked_by_preflight",
      message: `Сбор логов не запущен: ${preflight.reason_code}.${nextSession}`,
      reasonCode: preflight.reason_code,
      nextSessionAt: preflight.next_session_at,
      autoDismissMs: COMMAND_AUTO_DISMISS_MS,
    });
  }

  function blockedCommandMessage(reasonCode: string | null, nextSessionAt: string | null): string {
    const reason = reasonCode ?? "preflight_blocked";
    const nextSession = nextSessionAt ? ` Следующая сессия: ${nextSessionAt}.` : "";
    return `Сбор логов не запущен: ${reason}.${nextSession}`;
  }

  function setCommandFromResponse(response: RobotCommandResponse): void {
    const preflight = response.preflight_result as Partial<SessionPreflightResponse> | null;
    setCommandState({
      status: response.status,
      message: commandMessageFromResponse(response),
      reasonCode: response.reason_code,
      nextSessionAt: preflight?.next_session_at ?? null,
      autoDismissMs: commandAutoDismissMs(response.status),
    });
  }

  function commandAutoDismissMs(commandStatus: string): number | null {
    return ["collecting", "start_applied", "already_running", "already_collecting", "stop_requested"]
      .includes(commandStatus)
      ? COMMAND_AUTO_DISMISS_MS
      : null;
  }

  function commandResponseIsFinal(response: RobotCommandResponse): boolean {
    return (
      response.status === "already_running" ||
      response.status === "already_collecting" ||
      response.status === "collecting" ||
      response.status === "start_applied"
    );
  }

  function setCommandState(payload: {
    status: string;
    message: string;
    reasonCode?: string | null;
    nextSessionAt?: string | null;
    autoDismissMs?: number | null;
  }): void {
    clearCommandDismissTimer();
    lastCommandStatus.value = payload.status;
    lastCommandMessage.value = payload.message;
    lastCommandReasonCode.value = payload.reasonCode ?? null;
    lastCommandNextSessionAt.value = payload.nextSessionAt ?? null;
    lastCommandAt.value = new Date().toISOString();
    if (payload.autoDismissMs && payload.autoDismissMs > 0) {
      commandDismissTimer = window.setTimeout(() => {
        dismissCommand();
      }, payload.autoDismissMs);
    }
  }

  function clearCommandDismissTimer(): void {
    if (commandDismissTimer === null) {
      return;
    }
    window.clearTimeout(commandDismissTimer);
    commandDismissTimer = null;
  }

  function dismissCommand(): void {
    clearCommandDismissTimer();
    lastCommandStatus.value = null;
    lastCommandMessage.value = null;
    lastCommandReasonCode.value = null;
    lastCommandNextSessionAt.value = null;
  }

  function errorMessage(value: unknown): string {
    return value instanceof Error ? value.message : String(value);
  }

  function commandMessageFromResponse(response: RobotCommandResponse): string {
    if (
      response.status === "preflight_pending" ||
      response.status === "start_requested" ||
      response.reason_code === "preflight_pending"
    ) {
      return "Запуск сбора логов запрошен. Проверяю сессию...";
    }
    if (
      response.status === "preflight_retrying" ||
      response.reason_code === "preflight_retrying"
    ) {
      return "Брокер временно не ответил, повторяю проверку...";
    }
    if (
      response.status === "collecting" ||
      response.status === "start_applied" ||
      response.reason_code === "data_only_collection_started"
    ) {
      return "Сбор логов запущен.";
    }
    if (
      response.reason_code === "data_only_collection_already_collecting" ||
      response.reason_code === "data_only_collection_already_running" ||
      response.status === "already_running" ||
      response.status === "already_collecting"
    ) {
      return "Сбор логов уже запущен.";
    }
    if (!response.accepted) {
      return `Сбор логов не запущен: ${response.reason_code ?? "команда отклонена"}.`;
    }
    if (response.command === "start") {
      return "Сбор логов запущен.";
    }
    if (response.command === "stop") {
      return "Сбор логов остановлен.";
    }
    return "Команда принята.";
  }

  async function pollDataShadowStatusUntilSettled(): Promise<void> {
    const market = useMarketStore();
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await market.fetchDataShadowStatus();
      const state = market.dataShadowStatus.collector_state;
      const commandStatus = market.dataShadowStatus.command_status;
      const preflightPhase = market.dataShadowStatus.preflight_phase;
      if (
        commandStatus === "preflight_retrying" ||
        preflightPhase === "preflight_retrying"
      ) {
        setCommandState({
          status: "preflight_retrying",
          message: "Брокер временно не ответил, повторяю проверку...",
          reasonCode: market.dataShadowStatus.reason_code ?? "preflight_retrying",
        });
      } else if (
        commandStatus === "preflight_pending" ||
        preflightPhase === "preflight_pending" ||
        preflightPhase === "preflight_running"
      ) {
        setCommandState({
          status: "preflight_pending",
          message: "Запуск сбора логов запрошен. Проверяю сессию...",
          reasonCode: market.dataShadowStatus.reason_code ?? "preflight_pending",
        });
      }
      if (
        state === "collecting" ||
        state === "stopped_by_operator" ||
        state === "stopped" ||
        state === "idle" ||
        state === "preflight_blocked" ||
        state === "degraded"
      ) {
        if (state === "collecting") {
          setCommandState({
            status: "collecting",
            message: "Сбор логов запущен.",
            reasonCode: market.dataShadowStatus.reason_code,
            autoDismissMs: COMMAND_AUTO_DISMISS_MS,
          });
        } else if (
          state === "stopped_by_operator" ||
          state === "stopped" ||
          state === "idle"
        ) {
          setCommandState({
            status: state,
            message: "Сбор логов остановлен.",
            reasonCode: market.dataShadowStatus.reason_code ?? "data_only_collection_stopped",
            autoDismissMs: COMMAND_AUTO_DISMISS_MS,
          });
        } else if (state === "preflight_blocked") {
          setCommandState({
            status: "blocked_by_preflight",
            message: blockedCommandMessage(market.dataShadowStatus.reason_code, market.dataShadowStatus.next_session_at),
            reasonCode: market.dataShadowStatus.reason_code,
            nextSessionAt: market.dataShadowStatus.next_session_at,
          });
        }
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
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
    lastRuntimeStatusAt,
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
    fetchRuntimeStatusSnapshot,
    connectDashboardSocket,
    applyDashboardSnapshot,
    startRobot,
    stopRobot,
    refreshBalance,
    dismissCommand,
    startBalancePolling,
    stopBalancePolling,
    startStatusPolling,
    stopStatusPolling,
  };
});

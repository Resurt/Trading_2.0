import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  RobotCommandResponse,
  RobotStatusResponse,
  SessionPreflightResponse,
  SessionSnapshotResponse,
  SignalResponse,
} from "../api/types";
import { useRobotStore } from "../stores/robot";

const apiClientMock = vi.hoisted(() => ({
  dashboardState: vi.fn(),
  robotStatus: vi.fn(),
  currentSession: vi.fn(),
  currentSignals: vi.fn(),
  portfolioSummary: vi.fn(),
  sessionPreflight: vi.fn(),
  sessionPreflightFast: vi.fn(),
  startRobot: vi.fn(),
  stopRobot: vi.fn(),
  refreshPortfolio: vi.fn(),
  dataShadowStatus: vi.fn(),
}));

vi.mock("../api/client", () => ({
  apiClient: apiClientMock,
  openAuthenticatedWebSocket: vi.fn(),
}));

describe("robot store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    Object.values(apiClientMock).forEach((mock) => mock.mockReset());
    apiClientMock.dashboardState.mockRejectedValue(new Error("dashboard down"));
    apiClientMock.portfolioSummary.mockResolvedValue(portfolioFixture());
    apiClientMock.sessionPreflight.mockResolvedValue(preflightFixture(true));
    apiClientMock.sessionPreflightFast.mockResolvedValue(preflightFixture(true));
    apiClientMock.dataShadowStatus.mockResolvedValue(dataShadowStatusFixture("stopped"));
  });

  it("preserves robot status and balance when signals snapshot fails", async () => {
    apiClientMock.robotStatus.mockResolvedValue(statusFixture());
    apiClientMock.currentSession.mockResolvedValue(sessionFixture());
    apiClientMock.currentSignals.mockRejectedValue(new Error("signals down"));
    const robot = useRobotStore();

    await robot.fetchInitialSnapshot();

    expect(robot.status.balance.total_portfolio_value_rub).toBe("250000");
    expect(robot.status.balance.balance_degraded).toBe(false);
    expect(robot.signals).toEqual([]);
    expect(robot.error).toContain("signals_unavailable");
    expect(robot.error).not.toContain("robot_status_unavailable");
  });

  it("keeps portfolio balance when robot status fails but portfolio summary succeeds", async () => {
    apiClientMock.robotStatus.mockRejectedValue(new Error("status down"));
    apiClientMock.currentSession.mockResolvedValue(sessionFixture());
    apiClientMock.currentSignals.mockResolvedValue([]);
    const robot = useRobotStore();

    await robot.fetchInitialSnapshot();

    expect(robot.status.balance.balance_degraded).toBe(false);
    expect(robot.status.balance.total_portfolio_value_rub).toBe("250000");
    expect(robot.error).toContain("robot_status_unavailable");
  });

  it("keeps balance when session snapshot fails", async () => {
    apiClientMock.robotStatus.mockResolvedValue(statusFixture());
    apiClientMock.currentSession.mockRejectedValue(new Error("session down"));
    apiClientMock.currentSignals.mockResolvedValue(signalFixtures());
    const robot = useRobotStore();

    await robot.fetchInitialSnapshot();

    expect(robot.status.balance.total_portfolio_value_rub).toBe("250000");
    expect(robot.signals).toHaveLength(1);
    expect(robot.error).toContain("session_snapshot_unavailable");
  });

  it("persists rejected start command on closed market preflight", async () => {
    apiClientMock.sessionPreflightFast.mockResolvedValue(preflightFixture(false));
    apiClientMock.startRobot.mockResolvedValue({
      ...commandFixture("start", "rejected"),
      accepted: false,
      reason_code: "market_closed_expected",
      preflight_result: preflightFixture(false),
      message: "Рынок закрыт. Data-only сбор не запущен.",
    });
    apiClientMock.dataShadowStatus.mockResolvedValue(dataShadowStatusFixture("preflight_blocked"));
    apiClientMock.robotStatus.mockResolvedValue(statusFixture());
    apiClientMock.currentSession.mockResolvedValue(sessionFixture());
    apiClientMock.currentSignals.mockResolvedValue([]);
    const robot = useRobotStore();

    await robot.startRobot();

    expect(apiClientMock.startRobot).toHaveBeenCalledTimes(1);
    expect(robot.lastCommandStatus).toBe("rejected");
    expect(robot.lastCommandMessage).toContain("Рынок закрыт");
    expect(robot.lastCommandReasonCode).toBe("market_closed_expected");
  });

  it("shows stop command result", async () => {
    apiClientMock.stopRobot.mockResolvedValue(commandFixture("stop", "requested"));
    apiClientMock.robotStatus.mockResolvedValue(statusFixture());
    apiClientMock.currentSession.mockResolvedValue(sessionFixture());
    apiClientMock.currentSignals.mockResolvedValue([]);
    const robot = useRobotStore();

    await robot.stopRobot();

    expect(apiClientMock.stopRobot).toHaveBeenCalledTimes(1);
    expect(robot.lastCommandStatus).toBe("requested");
    expect(robot.lastCommandMessage).toContain("Команда stop отправлена");
  });
});

function portfolioFixture() {
  return {
    balance: statusFixture().balance,
    positions_count: 0,
    source: "test",
  };
}

function statusFixture(): RobotStatusResponse {
  return {
    balance: {
      currency: "RUB",
      available: "150000",
      blocked: "0",
      total_portfolio_value_rub: "250000",
      available_cash_rub: "150000",
      blocked_cash_rub: "0",
      expected_yield_rub: "1200",
      free_collateral_rub: "100000",
      account_id_masked: "acc***001",
      account_type: "broker",
      account_status: "open",
      balance_currency: "RUB",
      last_balance_refresh_at: "2026-06-20T10:00:00Z",
      balance_freshness_seconds: 10,
      balance_degraded: false,
      balance_degraded_reason_code: null,
    },
    active_instruments: ["MOEX:SBER"],
    active_timeframes: ["5m"],
    strategy_state: "wait",
    session_type: "weekday_main",
    session_phase: "continuous_trading",
    broker_trading_status: "normal_trading",
    open_orders_count: 0,
    active_positions_count: 0,
    degraded_flags: [],
    robot_control_state: "stopped",
    micro_session_id: "2026-06-20:weekday_main:1000",
  };
}

function sessionFixture(): SessionSnapshotResponse {
  return {
    calendar_date: "2026-06-20",
    trading_date: "2026-06-20",
    session_type: "weekday_main",
    session_phase: "continuous_trading",
    micro_session_id: "2026-06-20:weekday_main:1000",
    broker_trading_status: "normal_trading",
    observed_at: "2026-06-20T10:00:00Z",
  };
}

function signalFixtures(): SignalResponse[] {
  return [
    {
      candidate_id: "candidate-1",
      instrument_id: "MOEX:SBER",
      strategy_id: "baseline",
      timeframe: "5m",
      side: "buy",
      signal_type: "entry",
      candidate_status: "blocked",
      expected_edge_bps: "1",
      expected_holding_minutes: 5,
      final_blocker_code: "spread_too_wide",
      payload: {},
    },
  ];
}

function preflightFixture(marketOpen: boolean): SessionPreflightResponse {
  return {
    market_open: marketOpen,
    market_closed_expected: !marketOpen,
    now_msk: "2026-06-20T22:00:00+03:00",
    trading_date: "2026-06-20",
    calendar_date: "2026-06-20",
    session_type: "weekend",
    session_phase: marketOpen ? "continuous_trading" : "closed",
    broker_trading_status: marketOpen ? "normal_trading" : "closed",
    api_trade_available: marketOpen,
    next_session_at: marketOpen ? null : "2026-06-21T10:00:00+03:00",
    next_session_type: marketOpen ? null : "weekend",
    current_window_start_at: null,
    current_window_end_at: null,
    reason_code: marketOpen ? "market_open" : "market_closed_expected",
    source: "test_preflight",
    instruments_checked: ["MOEX:SBER"],
    per_instrument_status: {},
    warnings: [],
  };
}

function commandFixture(command: string, status: string): RobotCommandResponse {
  return {
    accepted: true,
    command_id: "command-1",
    command,
    command_type: command,
    requested_by_role: "operator",
    requested_by: "frontend-test",
    requested_at: "2026-06-20T10:00:00Z",
    status,
    reason_code: "operator_requested",
    payload: {},
    preflight_result: null,
    message: command === "stop" ? "Остановка запрошена" : "Команда принята",
  };
}

function dataShadowStatusFixture(collectorState: string) {
  return {
    enabled: true,
    collector_state: collectorState,
    strategy_trading_disabled: true,
    real_orders_disabled: true,
    market_open: collectorState !== "preflight_blocked",
    market_closed_expected: collectorState === "preflight_blocked",
    reason_code: collectorState === "preflight_blocked" ? "market_closed_expected" : "market_open",
    next_session_at: collectorState === "preflight_blocked" ? "2026-06-21T10:00:00+03:00" : null,
    stream_alive: collectorState === "collecting",
    last_message_age_seconds: null,
    candles_received: null,
    order_book_snapshots: 0,
    market_microstructure_snapshots: 0,
    avg_spread_bps: null,
    p95_spread_bps: null,
    avg_market_quality_score: null,
    current_session: "weekend",
    started_at: null,
    stopped_at: null,
    last_command_id: "command-1",
    last_command_status: collectorState === "preflight_blocked" ? "rejected" : "applied",
    last_command_reason_code: collectorState === "preflight_blocked" ? "market_closed_expected" : "market_open",
    instruments: ["MOEX:SBER"],
    stream_batches: [],
    warnings: [],
    warning: "Strategy trading disabled: data-only shadow mode",
  };
}

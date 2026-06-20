import { afterEach, describe, expect, it, vi } from "vitest";

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client auth", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("sends bearer token for production-like REST requests", async () => {
    vi.resetModules();
    vi.stubGlobal("__TRADING_FRONTEND_CONFIG__", {
      apiAuthMode: "static_bearer",
      apiBearerToken: "frontend-test-token",
    });
    const fetchMock = vi.fn(async () =>
      jsonResponse({
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
          balance_degraded_reason_code: "test_fixture",
        },
        active_instruments: [],
        active_timeframes: [],
        strategy_state: "wait",
        session_type: "weekday_main",
        session_phase: "continuous",
        broker_trading_status: "normal_trading",
        open_orders_count: 0,
        active_positions_count: 0,
        degraded_flags: [],
        robot_control_state: "stopped",
        micro_session_id: null,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { apiClient } = await import("../api/client");
    await apiClient.robotStatus();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = init.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer frontend-test-token");
    expect(headers.has("X-API-Role")).toBe(false);
  });

  it("uses a signed ticket for production-like WebSocket connections", async () => {
    vi.resetModules();
    vi.stubGlobal("__TRADING_FRONTEND_CONFIG__", {
      apiAuthMode: "static_bearer",
      apiBearerToken: "frontend-test-token",
    });
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        ticket: "signed-ticket",
        expires_at: "2026-06-17T12:00:00Z",
        auth_mode: "static_bearer",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    class FakeWebSocket {
      static lastUrl = "";
      readonly url: string;

      constructor(url: string) {
        this.url = url;
        FakeWebSocket.lastUrl = url;
      }
    }

    vi.stubGlobal("WebSocket", FakeWebSocket);

    const { openAuthenticatedWebSocket } = await import("../api/client");
    await openAuthenticatedWebSocket("/ws/dashboard");

    expect(FakeWebSocket.lastUrl).toContain("/ws/dashboard");
    expect(FakeWebSocket.lastUrl).toContain("ticket=signed-ticket");
  });
});

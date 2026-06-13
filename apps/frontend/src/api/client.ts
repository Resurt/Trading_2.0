import type {
  BlockerAnalyticsResponse,
  CandidateFunnelResponse,
  CanceledOrderDiagnosticsResponse,
  CounterfactualResponse,
  DailyReportResponse,
  DailyReportRunRequest,
  HourlyReportResponse,
  MarketOverviewResponse,
  OrderResponse,
  PositionResponse,
  ReportJobResponse,
  ReportJobStatusResponse,
  ReportRebuildRequest,
  RobotStatusResponse,
  SessionSnapshotResponse,
  SignalResponse,
  StrategyConfigResponse,
  StrategyConfigUpdateRequest,
} from "./types";

const DEFAULT_API_BASE_URL = "http://localhost:8000";
const DEFAULT_WS_BASE_URL = "ws://localhost:8000";

export const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
export const wsBaseUrl = import.meta.env.VITE_WS_BASE_URL ?? DEFAULT_WS_BASE_URL;

export type QueryValue = string | number | boolean | null | undefined;

export function withQuery(path: string, query: Record<string, QueryValue>): string {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") {
      params.set(key, String(value));
    }
  });
  const suffix = params.toString();
  return suffix ? `${path}?${suffix}` : path;
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  role: "observer" | "operator" | "admin" = "observer",
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  headers.set("X-API-Role", role);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${apiBaseUrl}${path}`, { ...init, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  return (await response.json()) as T;
}

export const apiClient = {
  robotStatus: () => requestJson<RobotStatusResponse>("/robot/status"),
  currentSession: () => requestJson<SessionSnapshotResponse>("/session/current"),
  positions: () => requestJson<PositionResponse[]>("/positions"),
  openOrders: () => requestJson<OrderResponse[]>("/orders/open"),
  currentSignals: () => requestJson<SignalResponse[]>("/signals/current"),
  marketOverview: () => requestJson<MarketOverviewResponse>("/market/overview"),
  hourlyReports: (query: Record<string, QueryValue>) =>
    requestJson<HourlyReportResponse[]>(withQuery("/reports/hourly", query)),
  dailyReports: (query: Record<string, QueryValue>) =>
    requestJson<DailyReportResponse[]>(withQuery("/reports/daily", query)),
  counterfactualReports: (query: Record<string, QueryValue>) =>
    requestJson<CounterfactualResponse[]>(withQuery("/reports/counterfactual", query)),
  blockerAnalytics: (query: Record<string, QueryValue>) =>
    requestJson<BlockerAnalyticsResponse>(withQuery("/analytics/blockers", query)),
  candidateFunnel: (query: Record<string, QueryValue>) =>
    requestJson<CandidateFunnelResponse>(withQuery("/analytics/candidate-funnel", query)),
  canceledOrderDiagnostics: (query: Record<string, QueryValue>) =>
    requestJson<CanceledOrderDiagnosticsResponse>(withQuery("/analytics/canceled-orders", query)),
  reportJobStatus: (jobId: string) =>
    requestJson<ReportJobStatusResponse>(`/reports/jobs/${encodeURIComponent(jobId)}`),
  strategyConfig: (strategyId: string, sessionTemplate: string) =>
    requestJson<StrategyConfigResponse>(
      withQuery("/config/strategy", {
        strategy_id: strategyId,
        session_template: sessionTemplate,
      }),
    ),
  updateStrategyConfig: (payload: StrategyConfigUpdateRequest) =>
    requestJson<StrategyConfigResponse>(
      "/config/strategy",
      { method: "PUT", body: JSON.stringify(payload) },
      "operator",
    ),
  startRobot: () => requestJson("/robot/start", { method: "POST" }, "operator"),
  stopRobot: () => requestJson("/robot/stop", { method: "POST" }, "operator"),
  rebuildDailyReport: (payload: DailyReportRunRequest) =>
    requestJson<ReportJobResponse>(
      "/reports/daily/run",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  rebuildReport: (payload: ReportRebuildRequest) =>
    requestJson<ReportJobResponse>(
      "/reports/rebuild/run",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
};

export function websocketUrl(path: string): string {
  return `${wsBaseUrl}${path}`;
}

import type {
  AuthStatusResponse,
  BlockerAnalyticsResponse,
  CalibrationDiagnosticRunResponse,
  CalibrationObservatoryRunRequest,
  CalibrationObservatoryRunResponse,
  CalibrationObservatoryStatusResponse,
  CalibrationResponse,
  CandidateFunnelResponse,
  CanceledOrderDiagnosticsResponse,
  CorporateActionResponse,
  CounterfactualResponse,
  DailyReportResponse,
  DataShadowStatusResponse,
  DividendSyncStatusResponse,
  DailyReportRunRequest,
  HistoricalQualityResponse,
  HistoricalRunResponse,
  HourlyReportResponse,
  InstrumentRegistryResponse,
  IntradayAnalyticsSnapshotResponse,
  MarketMicrostructureSnapshotResponse,
  MarketMicrostructureSummaryResponse,
  MarketOverviewResponse,
  MarketRegimeSnapshotResponse,
  MarketSpecialDayClassificationResponse,
  MarketSpecialDayResponse,
  OrderResponse,
  PositionResponse,
  ReportJobResponse,
  ReportJobStatusResponse,
  ReportRebuildRequest,
  RobotStatusResponse,
  RollingPerformanceCubeResponse,
  SessionSnapshotResponse,
  SignalResponse,
  StrategyConfigCandidateRejectRequest,
  StrategyConfigCandidateResponse,
  StrategyConfigResponse,
  StrategyConfigUpdateRequest,
  WebSocketTicketResponse,
} from "./types";

const DEFAULT_API_BASE_URL = "http://localhost:8000";
const DEFAULT_WS_BASE_URL = "ws://localhost:8000";
const DEFAULT_RUNTIME_MODE = "historical_replay";
const DEFAULT_API_AUTH_MODE = "dev";

export const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
export const wsBaseUrl = import.meta.env.VITE_WS_BASE_URL ?? DEFAULT_WS_BASE_URL;
export const runtimeMode =
  import.meta.env.VITE_TRADING_RUNTIME_MODE ?? import.meta.env.VITE_RUNTIME_MODE ?? DEFAULT_RUNTIME_MODE;

type FrontendRuntimeConfig = {
  apiAuthMode?: string;
  apiBearerToken?: string;
  apiActor?: string;
};

type GlobalWithRuntimeConfig = typeof globalThis & {
  __TRADING_FRONTEND_CONFIG__?: FrontendRuntimeConfig;
};

const runtimeConfig = (globalThis as GlobalWithRuntimeConfig).__TRADING_FRONTEND_CONFIG__ ?? {};
export const apiAuthMode =
  runtimeConfig.apiAuthMode ?? import.meta.env.VITE_API_AUTH_MODE ?? DEFAULT_API_AUTH_MODE;
export const apiActor =
  runtimeConfig.apiActor ?? import.meta.env.VITE_API_ACTOR ?? "frontend_operator";

const apiBearerToken = runtimeConfig.apiBearerToken ?? import.meta.env.VITE_API_BEARER_TOKEN ?? "";

export type ApiRole = "observer" | "operator" | "admin";

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

function isProductionRuntime(): boolean {
  return runtimeMode === "production";
}

function shouldUseDevRoleHeader(): boolean {
  return apiAuthMode === "dev" && !isProductionRuntime();
}

function shouldUseBearer(): boolean {
  return apiAuthMode === "static_bearer" && apiBearerToken.length > 0;
}

function applyAuthHeaders(headers: Headers, role: ApiRole): void {
  if (shouldUseBearer()) {
    headers.set("Authorization", `Bearer ${apiBearerToken}`);
    return;
  }
  if (shouldUseDevRoleHeader()) {
    headers.set("X-API-Role", role);
    headers.set("X-API-Actor", apiActor);
  }
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  role: ApiRole = "observer",
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  applyAuthHeaders(headers, role);
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
  authStatus: () => requestJson<AuthStatusResponse>("/auth/status"),
  wsTicket: (role: ApiRole = "observer") =>
    requestJson<WebSocketTicketResponse>("/auth/ws-ticket", { method: "POST" }, role),
  robotStatus: () => requestJson<RobotStatusResponse>("/robot/status"),
  currentSession: () => requestJson<SessionSnapshotResponse>("/session/current"),
  positions: () => requestJson<PositionResponse[]>("/positions"),
  openOrders: () => requestJson<OrderResponse[]>("/orders/open"),
  currentSignals: () => requestJson<SignalResponse[]>("/signals/current"),
  marketOverview: () => requestJson<MarketOverviewResponse>("/market/overview"),
  latestMicrostructure: (query: Record<string, QueryValue>) =>
    requestJson<MarketMicrostructureSnapshotResponse[]>(
      withQuery("/market/microstructure/latest", query),
    ),
  microstructureSummary: (query: Record<string, QueryValue>) =>
    requestJson<MarketMicrostructureSummaryResponse>(
      withQuery("/market/microstructure/summary", query),
    ),
  dataShadowStatus: () =>
    requestJson<DataShadowStatusResponse>("/runtime/data-shadow/status"),
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
  intradayToday: (query: Record<string, QueryValue> = {}) =>
    requestJson<IntradayAnalyticsSnapshotResponse>(
      withQuery("/analytics/intraday/today", query),
    ),
  intradayAnalytics: (query: Record<string, QueryValue>) =>
    requestJson<IntradayAnalyticsSnapshotResponse>(withQuery("/analytics/intraday", query)),
  intradaySession: (query: Record<string, QueryValue>) =>
    requestJson<IntradayAnalyticsSnapshotResponse>(
      withQuery("/analytics/intraday/session", query),
    ),
  intradayMicroSession: (microSessionId: string, query: Record<string, QueryValue> = {}) =>
    requestJson<IntradayAnalyticsSnapshotResponse>(
      withQuery(`/analytics/intraday/micro-session/${encodeURIComponent(microSessionId)}`, query),
    ),
  historicalDataQuality: (query: Record<string, QueryValue>) =>
    requestJson<HistoricalQualityResponse>(withQuery("/historical/data-quality", query)),
  instrumentsRegistry: () =>
    requestJson<InstrumentRegistryResponse[]>("/instruments/registry"),
  unresolvedInstruments: () =>
    requestJson<InstrumentRegistryResponse[]>("/instruments/unresolved"),
  resolveInstruments: (payload: Record<string, unknown>) =>
    requestJson<Record<string, unknown>>(
      "/instruments/resolve",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  corporateActions: (query: Record<string, QueryValue>) =>
    requestJson<CorporateActionResponse[]>(withQuery("/corporate-actions", query)),
  dividends: (query: Record<string, QueryValue>) =>
    requestJson<CorporateActionResponse[]>(withQuery("/corporate-actions/dividends", query)),
  dividendSyncStatus: (query: Record<string, QueryValue>) =>
    requestJson<DividendSyncStatusResponse>(
      withQuery("/corporate-actions/dividends/sync/status", query),
    ),
  syncTbankDividends: (payload: Record<string, unknown>) =>
    requestJson<Record<string, unknown>>(
      "/corporate-actions/dividends/sync",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  importCorporateActions: (payload: Record<string, unknown>) =>
    requestJson<{ rows_imported: number; corporate_action_ids: string[] }>(
      "/corporate-actions/import",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  marketSpecialDays: (query: Record<string, QueryValue>) =>
    requestJson<MarketSpecialDayResponse[]>(withQuery("/market-special-days", query)),
  futureMarketSpecialDays: (query: Record<string, QueryValue>) =>
    requestJson<MarketSpecialDayResponse[]>(withQuery("/market-special-days/future", query)),
  classifyMarketSpecialDays: (payload: Record<string, unknown>) =>
    requestJson<MarketSpecialDayClassificationResponse>(
      "/market-special-days/classify",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  runHistoricalReplay: (payload: Record<string, unknown>) =>
    requestJson<HistoricalRunResponse>(
      "/historical/replay/run",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  runHistoricalCounterfactual: (payload: Record<string, unknown>) =>
    requestJson<HistoricalRunResponse>(
      "/historical/counterfactual/run",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  runHistoricalReportRebuild: (payload: Record<string, unknown>) =>
    requestJson<HistoricalRunResponse>(
      "/historical/reports/rebuild",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  calibrationReport: (query: Record<string, QueryValue>) =>
    requestJson<CalibrationResponse>(withQuery("/analytics/calibration", query)),
  calibrationObservatoryStatus: () =>
    requestJson<CalibrationObservatoryStatusResponse>("/calibration/observatory/status"),
  runCalibrationObservatory: (payload: CalibrationObservatoryRunRequest) =>
    requestJson<CalibrationObservatoryRunResponse>(
      "/calibration/observatory/run",
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
  calibrationDiagnostics: (query: Record<string, QueryValue> = {}) =>
    requestJson<CalibrationDiagnosticRunResponse[]>(
      withQuery("/calibration/diagnostics", query),
    ),
  calibrationDiagnostic: (diagnosticRunId: string) =>
    requestJson<CalibrationDiagnosticRunResponse>(
      `/calibration/diagnostics/${encodeURIComponent(diagnosticRunId)}`,
    ),
  rollingPerformance: (query: Record<string, QueryValue> = {}) =>
    requestJson<RollingPerformanceCubeResponse[]>(
      withQuery("/calibration/rolling-performance", query),
    ),
  calibrationRegime: (query: Record<string, QueryValue> = {}) =>
    requestJson<MarketRegimeSnapshotResponse[]>(withQuery("/calibration/regime", query)),
  configCandidates: (query: Record<string, QueryValue> = {}) =>
    requestJson<StrategyConfigCandidateResponse[]>(
      withQuery("/calibration/config-candidates", query),
    ),
  configCandidate: (candidateConfigId: string) =>
    requestJson<StrategyConfigCandidateResponse>(
      `/calibration/config-candidates/${encodeURIComponent(candidateConfigId)}`,
    ),
  approveConfigCandidateForShadow: (candidateConfigId: string) =>
    requestJson<StrategyConfigCandidateResponse>(
      `/calibration/config-candidates/${encodeURIComponent(
        candidateConfigId,
      )}/approve-for-shadow`,
      { method: "POST" },
      "admin",
    ),
  rejectConfigCandidate: (
    candidateConfigId: string,
    payload: StrategyConfigCandidateRejectRequest,
  ) =>
    requestJson<StrategyConfigCandidateResponse>(
      `/calibration/config-candidates/${encodeURIComponent(candidateConfigId)}/reject`,
      { method: "POST", body: JSON.stringify(payload) },
      "operator",
    ),
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

export async function openAuthenticatedWebSocket(
  path: string,
  role: ApiRole = "observer",
): Promise<WebSocket> {
  const url = new URL(websocketUrl(path));
  if (apiAuthMode === "static_bearer") {
    const ticket = await apiClient.wsTicket(role);
    url.searchParams.set("ticket", ticket.ticket);
  }
  return new WebSocket(url.toString());
}

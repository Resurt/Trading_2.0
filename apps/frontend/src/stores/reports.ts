import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";

import { apiClient, websocketUrl } from "../api/client";
import type {
  BlockerAnalyticsResponse,
  CandidateFunnelResponse,
  CanceledOrderDiagnosticsResponse,
  ConnectionState,
  CounterfactualResponse,
  DailyReportResponse,
  HourlyReportResponse,
  ReportJobResponse,
  ReportJobStatusResponse,
  ReportScope,
  ReportsSnapshotPayload,
  WebSocketEnvelope,
} from "../api/types";
import { nestedRecord } from "../utils/format";

export interface ReportFilters {
  tradingDate: string;
  instrumentId: string;
  timeframe: string;
  sessionType: string;
  blockerCode: string;
  strategyId: string;
  strategyVersion: string;
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

export const useReportsStore = defineStore("reports", () => {
  const filters = reactive<ReportFilters>({
    tradingDate: todayIso(),
    instrumentId: "",
    timeframe: "",
    sessionType: "",
  blockerCode: "",
  strategyId: "baseline",
  strategyVersion: "",
});
  const hourlyReports = ref<HourlyReportResponse[]>([]);
  const dailyReports = ref<DailyReportResponse[]>([]);
  const counterfactuals = ref<CounterfactualResponse[]>([]);
  const blockerAnalytics = ref<BlockerAnalyticsResponse>({
    generated_at: new Date(0).toISOString(),
    filters: {},
    rows: [],
  });
  const candidateFunnel = ref<CandidateFunnelResponse>({
    generated_at: new Date(0).toISOString(),
    filters: {},
    stages: [],
    totals: {},
  });
  const canceledDiagnostics = ref<CanceledOrderDiagnosticsResponse>({
    generated_at: new Date(0).toISOString(),
    filters: {},
    rows: [],
  });
  const latestJob = ref<ReportJobResponse | null>(null);
  const latestJobStatus = ref<ReportJobStatusResponse | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const liveConnection = ref<ConnectionState>("idle");
  let reportsSocket: WebSocket | null = null;

  const latestDaily = computed(() => dailyReports.value[0] ?? null);
  const latestHourly = computed(() => hourlyReports.value[0] ?? null);

  const blockerRanking = computed(() => {
    if (blockerAnalytics.value.rows.length) {
      return blockerAnalytics.value.rows.map((row) => ({
        ...row,
        reason_code: row.blocker_code,
      }));
    }
    const ranking = nestedRecord(latestDaily.value?.payload ?? {}, "blocker_ranking");
    if (Array.isArray(latestDaily.value?.payload.blocker_ranking)) {
      return latestDaily.value.payload.blocker_ranking as Array<Record<string, unknown>>;
    }
    return Object.entries(ranking).map(([reason_code, count]) => ({ reason_code, count }));
  });

  const missedOpportunities = computed(() =>
    counterfactuals.value.filter(
      (result) => result.would_profit_5m || result.would_profit_10m || result.would_profit_15m,
    ),
  );

  const summaryBySession = computed(() =>
    nestedRecord(latestDaily.value?.payload ?? {}, "summary_by_session_type"),
  );
  const summaryByInstrument = computed(() =>
    nestedRecord(latestDaily.value?.payload ?? {}, "summary_by_instrument"),
  );
  const summaryByTimeframe = computed(() =>
    nestedRecord(latestDaily.value?.payload ?? {}, "summary_by_timeframe"),
  );
  const hourlyTimelineBars = computed(() =>
    hourlyReports.value.map((report) => ({
      label: report.micro_session_id,
      value: report.signal_count,
      code: `${report.session_type}${report.timeframe ? `/${report.timeframe}` : ""}`,
    })),
  );
  const candidateFunnelBars = computed(() =>
    candidateFunnel.value.stages.map((stage) => ({
      label: stage.stage_name,
      value: stage.count,
      code: stage.percentage_of_created
        ? `${Math.round(Number(stage.percentage_of_created) * 100)}%`
        : undefined,
    })),
  );
  const canceledDiagnosticsRows = computed(() => canceledDiagnostics.value.rows);
  const counterfactualHorizonRows = computed(() =>
    [
      {
        label: "+5m",
        value: counterfactuals.value.filter((result) => result.would_profit_5m).length,
        mfe: average(counterfactuals.value.map((result) => result.mfe_5m_bps)),
        mae: average(counterfactuals.value.map((result) => result.mae_5m_bps)),
      },
      {
        label: "+10m",
        value: counterfactuals.value.filter((result) => result.would_profit_10m).length,
        mfe: average(counterfactuals.value.map((result) => result.mfe_10m_bps)),
        mae: average(counterfactuals.value.map((result) => result.mae_10m_bps)),
      },
      {
        label: "+15m",
        value: counterfactuals.value.filter((result) => result.would_profit_15m).length,
        mfe: average(counterfactuals.value.map((result) => result.mfe_15m_bps)),
        mae: average(counterfactuals.value.map((result) => result.mae_15m_bps)),
      },
    ],
  );

  function numericStrategyVersion(): number | null {
    const value = Number(filters.strategyVersion);
    return Number.isFinite(value) && filters.strategyVersion !== "" ? value : null;
  }

  function filtersQuery() {
    return {
      trading_date: filters.tradingDate,
      strategy_id: filters.strategyId,
      instrument_id: filters.instrumentId,
      timeframe: filters.timeframe,
      session_type: filters.sessionType,
      blocker_code: filters.blockerCode,
      strategy_version: numericStrategyVersion(),
    };
  }

  async function fetchReports(): Promise<void> {
    loading.value = true;
    error.value = null;
    const query = filtersQuery();
    try {
      const [hourly, daily, counterfactual, blockers, funnel, canceled] = await Promise.all([
        apiClient.hourlyReports(query),
        apiClient.dailyReports(query),
        apiClient.counterfactualReports(query),
        apiClient.blockerAnalytics(query),
        apiClient.candidateFunnel(query),
        apiClient.canceledOrderDiagnostics(query),
      ]);
      hourlyReports.value = hourly;
      dailyReports.value = daily;
      counterfactuals.value = counterfactual;
      blockerAnalytics.value = blockers;
      candidateFunnel.value = funnel;
      canceledDiagnostics.value = canceled;
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Reports snapshot failed";
      hourlyReports.value = [];
      dailyReports.value = [];
      counterfactuals.value = [];
      blockerAnalytics.value = { generated_at: new Date(0).toISOString(), filters: {}, rows: [] };
      candidateFunnel.value = {
        generated_at: new Date(0).toISOString(),
        filters: {},
        stages: [],
        totals: {},
      };
      canceledDiagnostics.value = { generated_at: new Date(0).toISOString(), filters: {}, rows: [] };
    } finally {
      loading.value = false;
    }
  }

  async function rebuildDailyReport(): Promise<void> {
    latestJob.value = await apiClient.rebuildReport({
      scope: "daily",
      trading_date: filters.tradingDate,
      strategy_id: filters.strategyId,
      instrument_id: filters.instrumentId || null,
      timeframe: filters.timeframe || null,
      session_type: filters.sessionType || null,
      strategy_version: numericStrategyVersion(),
      include_counterfactual: true,
      force_rebuild: true,
    });
    latestJobStatus.value = null;
  }

  async function rebuildReport(scope: ReportScope, microSessionId?: string): Promise<void> {
    latestJob.value = await apiClient.rebuildReport({
      scope,
      trading_date: filters.tradingDate,
      strategy_id: filters.strategyId,
      micro_session_id: microSessionId ?? null,
      instrument_id: filters.instrumentId || null,
      timeframe: filters.timeframe || null,
      session_type: filters.sessionType || null,
      strategy_version: numericStrategyVersion(),
      include_counterfactual: true,
      force_rebuild: true,
    });
    latestJobStatus.value = null;
  }

  async function refreshLatestJobStatus(): Promise<void> {
    if (!latestJob.value) {
      return;
    }
    latestJobStatus.value = await apiClient.reportJobStatus(latestJob.value.job_id);
  }

  function connectReportsSocket(): void {
    if (reportsSocket && reportsSocket.readyState < WebSocket.CLOSING) {
      return;
    }
    liveConnection.value = "loading";
    reportsSocket = new WebSocket(websocketUrl("/ws/reports"));
    reportsSocket.onopen = () => {
      liveConnection.value = "live";
    };
    reportsSocket.onmessage = (event: MessageEvent<string>) => {
      const envelope = JSON.parse(event.data) as WebSocketEnvelope<ReportsSnapshotPayload>;
      if (envelope.payload.data?.hourly) {
        hourlyReports.value = envelope.payload.data.hourly;
      }
      if (envelope.payload.data?.daily) {
        dailyReports.value = envelope.payload.data.daily;
      }
      if (envelope.payload.data?.counterfactual) {
        counterfactuals.value = envelope.payload.data.counterfactual;
      }
      if (envelope.payload.data?.blockers) {
        blockerAnalytics.value = envelope.payload.data.blockers;
      }
      if (envelope.payload.data?.candidate_funnel) {
        candidateFunnel.value = envelope.payload.data.candidate_funnel;
      }
      if (envelope.payload.data?.canceled_orders) {
        canceledDiagnostics.value = envelope.payload.data.canceled_orders;
      }
    };
    reportsSocket.onerror = () => {
      liveConnection.value = "degraded";
    };
    reportsSocket.onclose = () => {
      liveConnection.value = liveConnection.value === "degraded" ? "degraded" : "snapshot_closed";
      reportsSocket = null;
    };
  }

  return {
    filters,
    hourlyReports,
    dailyReports,
    counterfactuals,
    blockerAnalytics,
    candidateFunnel,
    canceledDiagnostics,
    latestJob,
    latestJobStatus,
    loading,
    error,
    liveConnection,
    latestDaily,
    latestHourly,
    blockerRanking,
    missedOpportunities,
    summaryBySession,
    summaryByInstrument,
    summaryByTimeframe,
    hourlyTimelineBars,
    candidateFunnelBars,
    canceledDiagnosticsRows,
    counterfactualHorizonRows,
    fetchReports,
    rebuildDailyReport,
    rebuildReport,
    refreshLatestJobStatus,
    connectReportsSocket,
  };
});

function average(values: Array<string | null>): string | null {
  const numericValues = values
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value));
  if (numericValues.length === 0) {
    return null;
  }
  const sum = numericValues.reduce((total, value) => total + value, 0);
  return String(sum / numericValues.length);
}

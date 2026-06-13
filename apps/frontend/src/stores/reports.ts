import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";

import { apiClient, websocketUrl } from "../api/client";
import type {
  ConnectionState,
  CounterfactualResponse,
  DailyReportResponse,
  HourlyReportResponse,
  ReportJobResponse,
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
  });
  const hourlyReports = ref<HourlyReportResponse[]>([]);
  const dailyReports = ref<DailyReportResponse[]>([]);
  const counterfactuals = ref<CounterfactualResponse[]>([]);
  const latestJob = ref<ReportJobResponse | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const liveConnection = ref<ConnectionState>("idle");
  let reportsSocket: WebSocket | null = null;

  const latestDaily = computed(() => dailyReports.value[0] ?? null);
  const latestHourly = computed(() => hourlyReports.value[0] ?? null);

  const blockerRanking = computed(() => {
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

  async function fetchReports(): Promise<void> {
    loading.value = true;
    error.value = null;
    const query = {
      trading_date: filters.tradingDate,
      strategy_id: filters.strategyId,
      instrument_id: filters.instrumentId,
      timeframe: filters.timeframe,
      session_type: filters.sessionType,
      blocker_code: filters.blockerCode,
    };
    try {
      const [hourly, daily, counterfactual] = await Promise.all([
        apiClient.hourlyReports(query),
        apiClient.dailyReports(query),
        apiClient.counterfactualReports(query),
      ]);
      hourlyReports.value = hourly;
      dailyReports.value = daily;
      counterfactuals.value = counterfactual;
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Reports snapshot failed";
      hourlyReports.value = [];
      dailyReports.value = [];
      counterfactuals.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function rebuildDailyReport(): Promise<void> {
    latestJob.value = await apiClient.rebuildDailyReport({
      trading_date: filters.tradingDate,
      strategy_id: filters.strategyId,
      include_counterfactual: true,
    });
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
    latestJob,
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
    fetchReports,
    rebuildDailyReport,
    connectReportsSocket,
  };
});

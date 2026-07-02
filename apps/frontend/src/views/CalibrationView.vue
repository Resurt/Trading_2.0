<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Check, RefreshCw, X } from "@lucide/vue";

import { apiClient } from "../api/client";
import type {
  CalibrationObservatoryRunResponse,
  CalibrationObservatoryStatusResponse,
  CalibrationResponse,
  JsonPayload,
  MarketRegimeSnapshotResponse,
  RollingPerformanceCubeResponse,
  StrategyConfigCandidateResponse,
} from "../api/types";
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import MiniBars from "../components/ui/MiniBars.vue";
import StatusPill from "../components/ui/StatusPill.vue";

type LooseRow = Record<string, unknown>;

const lookbackDays = ref(10);
const instruments = ref("SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T");
const timeframes = ref("5m,10m,15m");
const strategyId = ref("baseline");
const calibrationScope = ref("primary_normal_days");
const loading = ref(false);
const error = ref("");
const report = ref<CalibrationResponse | null>(null);

const observatoryUniverse = ref("SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T");
const observatoryLookbackDays = ref(20);
const observatoryWindows = ref("7d,20d,60d,90d,180d,365d");
const observatoryMode = ref("data_shadow");
const createCandidateConfig = ref(false);
const observatoryLoading = ref(false);
const observatoryError = ref("");
const observatoryStatus = ref<CalibrationObservatoryStatusResponse | null>(null);
const lastObservatoryRun = ref<CalibrationObservatoryRunResponse | null>(null);
const cubeRows = ref<RollingPerformanceCubeResponse[]>([]);
const regimes = ref<MarketRegimeSnapshotResponse[]>([]);
const candidates = ref<StrategyConfigCandidateResponse[]>([]);

const cubeWindow = ref("20d");
const cubeInstrument = ref("");
const cubeSession = ref("");
const cubeTimeframe = ref("");
const cubeSide = ref("");
const cubeMode = ref("data_shadow");

const blockerRows = computed(() => report.value?.blocker_ranking ?? []);
const blockerBars = computed(() =>
  blockerRows.value.map((row) => ({
    label: String(row.blocker_code ?? "unknown"),
    value: Number(row.count ?? 0),
    code: String(row.false_positive_proxy ?? ""),
  })),
);

const thresholdRows = computed(() =>
  Object.entries(report.value?.recommended_threshold_changes ?? {}).map(([key, value]) => ({
    key,
    value: String(value),
  })),
);

const safeRecommendations = computed(() => {
  const value = report.value?.recommendations.safe_from_historical_candles;
  return value && typeof value === "object" ? Object.entries(value) : [];
});

const shadowRecommendations = computed(() => {
  const value = report.value?.recommendations.requires_shadow_confirmation;
  return value && typeof value === "object" ? Object.entries(value) : [];
});

const filteredCubeRows = computed(() =>
  cubeRows.value.filter(
    (row) =>
      (!cubeWindow.value || row.window_name === cubeWindow.value) &&
      (!cubeInstrument.value || row.instrument_id === cubeInstrument.value) &&
      (!cubeSession.value || row.session_type === cubeSession.value) &&
      (!cubeTimeframe.value || row.timeframe === cubeTimeframe.value) &&
      (!cubeSide.value || row.side === cubeSide.value) &&
      (!cubeMode.value || row.mode === cubeMode.value),
  ),
);

const cubeInstruments = computed(() => unique(cubeRows.value.map((row) => row.instrument_id)));
const cubeSessions = computed(() => unique(cubeRows.value.map((row) => row.session_type)));
const cubeTimeframes = computed(() => unique(cubeRows.value.map((row) => row.timeframe)));
const cubeSides = computed(() => unique(cubeRows.value.map((row) => row.side)));

const topContours = computed<LooseRow[]>(() => {
  if (lastObservatoryRun.value?.top_contours.length) {
    return rowsFromPayload(lastObservatoryRun.value.top_contours).slice(0, 8);
  }
  return [...cubeRows.value]
    .sort((a, b) => Number(b.avg_net_pnl_proxy) - Number(a.avg_net_pnl_proxy))
    .slice(0, 8);
});

const deadContours = computed<LooseRow[]>(() => {
  if (lastObservatoryRun.value?.dead_contours.length) {
    return rowsFromPayload(lastObservatoryRun.value.dead_contours).slice(0, 8);
  }
  return cubeRows.value
    .filter((row) => row.candidate_count === 0 || row.sample_warning)
    .slice(0, 8);
});

const regimeRows = computed(() => regimes.value.slice(0, 10));
const draftCandidates = computed(() => candidates.value.filter((row) => row.status === "draft"));
const warnings = computed(() =>
  lastObservatoryRun.value?.warnings.length
    ? lastObservatoryRun.value.warnings
    : stringList(observatoryStatus.value?.latest_diagnostic?.warnings),
);

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    report.value = await apiClient.calibrationReport({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
      timeframes: timeframes.value,
      strategy_id: strategyId.value,
      calibration_scope: calibrationScope.value,
      require_special_day_classification: calibrationScope.value === "primary_normal_days",
    });
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

async function refreshObservatory() {
  observatoryLoading.value = true;
  observatoryError.value = "";
  try {
    const [status, cube, regime, configCandidates] = await Promise.all([
      apiClient.calibrationObservatoryStatus(),
      apiClient.rollingPerformance({ limit: 300 }),
      apiClient.calibrationRegime({ limit: 100 }),
      apiClient.configCandidates({ limit: 100 }),
    ]);
    observatoryStatus.value = status;
    cubeRows.value = cube;
    regimes.value = regime;
    candidates.value = configCandidates;
  } catch (err) {
    observatoryError.value = err instanceof Error ? err.message : String(err);
  } finally {
    observatoryLoading.value = false;
  }
}

async function runDiagnostics() {
  observatoryLoading.value = true;
  observatoryError.value = "";
  try {
    lastObservatoryRun.value = await apiClient.runCalibrationObservatory({
      universe: splitCsv(observatoryUniverse.value).join(","),
      lookback_days: observatoryLookbackDays.value,
      windows: splitCsv(observatoryWindows.value).join(","),
      mode: observatoryMode.value,
      trigger_type: "manual",
      create_candidate_config: createCandidateConfig.value,
      requested_by: "frontend_operator",
    });
    await refreshObservatory();
  } catch (err) {
    observatoryError.value = err instanceof Error ? err.message : String(err);
  } finally {
    observatoryLoading.value = false;
  }
}

async function approveForShadow(candidate: StrategyConfigCandidateResponse) {
  observatoryLoading.value = true;
  observatoryError.value = "";
  try {
    await apiClient.approveConfigCandidateForShadow(candidate.candidate_config_id);
    await refreshObservatory();
  } catch (err) {
    observatoryError.value = err instanceof Error ? err.message : String(err);
  } finally {
    observatoryLoading.value = false;
  }
}

async function rejectCandidate(candidate: StrategyConfigCandidateResponse) {
  observatoryLoading.value = true;
  observatoryError.value = "";
  try {
    await apiClient.rejectConfigCandidate(candidate.candidate_config_id, {
      reason: "rejected_from_calibration_center",
    });
    await refreshObservatory();
  } catch (err) {
    observatoryError.value = err instanceof Error ? err.message : String(err);
  } finally {
    observatoryLoading.value = false;
  }
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))].sort();
}

function rowsFromPayload(value: unknown): LooseRow[] {
  return Array.isArray(value)
    ? value.filter((row): row is LooseRow => row !== null && typeof row === "object")
    : [];
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item));
  }
  if (value && typeof value === "object") {
    const values = (value as { values?: unknown }).values;
    return Array.isArray(values) ? values.map((item) => String(item)) : [];
  }
  return [];
}

function value(row: JsonPayload | LooseRow, key: string, fallback = "-"): string {
  const raw = row[key];
  if (raw === null || raw === undefined || raw === "") {
    return fallback;
  }
  return String(raw);
}

onMounted(() => {
  void refreshObservatory();
});
</script>

<template>
  <section class="page-stack" data-testid="calibration-page">
    <div class="page-heading">
      <h1>Calibration Center</h1>
      <div class="heading-status">
        <StatusPill :code="lastObservatoryRun?.diagnosis ?? 'not_loaded'" />
      </div>
    </div>

    <DataPanel>
      <template #eyebrow>observatory</template>
      <template #title>Diagnostics run</template>
      <template #action>
        <button class="icon-button" type="button" :disabled="observatoryLoading" @click="runDiagnostics">
          <RefreshCw :size="16" aria-hidden="true" />
          <span>Run Diagnostics</span>
        </button>
      </template>
      <p class="diagnostic-caveat">
        Candidate configs are not applied to live trading automatically.
      </p>
      <form class="filter-grid filter-grid--dense" @submit.prevent="runDiagnostics">
        <label>
          <span>universe</span>
          <input v-model="observatoryUniverse" />
        </label>
        <label>
          <span>lookback_days</span>
          <input v-model.number="observatoryLookbackDays" type="number" min="1" max="3650" />
        </label>
        <label>
          <span>windows</span>
          <input v-model="observatoryWindows" />
        </label>
        <label>
          <span>mode</span>
          <select v-model="observatoryMode">
            <option value="data_shadow">data_shadow</option>
            <option value="historical">historical</option>
            <option value="strategy_shadow">strategy_shadow</option>
            <option value="all">all</option>
          </select>
        </label>
        <label class="checkbox-row">
          <input v-model="createCandidateConfig" type="checkbox" />
          <span>create draft candidate config</span>
        </label>
        <div class="filter-actions">
          <button class="icon-button" type="button" :disabled="observatoryLoading" @click="refreshObservatory">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Refresh</span>
          </button>
        </div>
      </form>
      <EmptyState v-if="observatoryLoading" title="Running calibration observatory" />
      <EmptyState
        v-if="observatoryError"
        title="Calibration observatory API degraded"
        :detail="observatoryError"
        tone="warn"
      />
    </DataPanel>

    <div class="metric-grid">
      <MetricTile label="diagnosis" :value="lastObservatoryRun?.diagnosis ?? value(observatoryStatus?.latest_diagnostic ?? {}, 'diagnosis')" />
      <MetricTile label="confidence" :value="lastObservatoryRun?.confidence ?? value(observatoryStatus?.latest_diagnostic ?? {}, 'confidence')" />
      <MetricTile label="rolling cube rows" :value="cubeRows.length" />
      <MetricTile label="draft candidates" :value="draftCandidates.length" />
      <MetricTile label="regime rows" :value="regimes.length" />
      <MetricTile label="calibration recommended" :value="lastObservatoryRun?.calibration_recommended ? 'true' : 'false'" />
    </div>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>diagnosis</template>
        <template #title>Robot health</template>
        <dl class="definition-grid">
          <dt>diagnostic_run_id</dt>
          <dd>{{ lastObservatoryRun?.diagnostic_run_id ?? value(observatoryStatus?.latest_diagnostic ?? {}, "diagnostic_run_id") }}</dd>
          <dt>diagnosis</dt>
          <dd>{{ lastObservatoryRun?.diagnosis ?? value(observatoryStatus?.latest_diagnostic ?? {}, "diagnosis") }}</dd>
          <dt>confidence</dt>
          <dd>{{ lastObservatoryRun?.confidence ?? value(observatoryStatus?.latest_diagnostic ?? {}, "confidence") }}</dd>
          <dt>blocking issues</dt>
          <dd>{{ lastObservatoryRun?.blocking_issues.length ?? 0 }}</dd>
          <dt>warnings</dt>
          <dd>{{ warnings.join(", ") || "-" }}</dd>
        </dl>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>market regime</template>
        <template #title>Regime summary</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>instrument</th>
                <th>session</th>
                <th>regime</th>
                <th>spread</th>
                <th>depth</th>
                <th>volatility</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in regimeRows" :key="row.regime_snapshot_id">
                <td>{{ row.instrument_id ?? "all" }}</td>
                <td>{{ row.session_type ?? "all" }}</td>
                <td><StatusPill :code="row.market_regime" compact /></td>
                <td>{{ row.spread_score ?? "-" }}</td>
                <td>{{ row.depth_score ?? "-" }}</td>
                <td>{{ row.volatility_score ?? "-" }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="regimeRows.length === 0" title="No regime snapshots yet" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>rolling cube</template>
        <template #title>Filters</template>
        <form class="filter-grid filter-grid--dense">
          <label>
            <span>window</span>
            <select v-model="cubeWindow">
              <option value="">all</option>
              <option value="7d">7d</option>
              <option value="20d">20d</option>
              <option value="60d">60d</option>
              <option value="90d">90d</option>
              <option value="180d">180d</option>
              <option value="365d">365d</option>
            </select>
          </label>
          <label>
            <span>instrument</span>
            <select v-model="cubeInstrument">
              <option value="">all</option>
              <option v-for="item in cubeInstruments" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
          <label>
            <span>session</span>
            <select v-model="cubeSession">
              <option value="">all</option>
              <option v-for="item in cubeSessions" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
          <label>
            <span>timeframe</span>
            <select v-model="cubeTimeframe">
              <option value="">all</option>
              <option v-for="item in cubeTimeframes" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
          <label>
            <span>side</span>
            <select v-model="cubeSide">
              <option value="">all</option>
              <option v-for="item in cubeSides" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
          <label>
            <span>mode</span>
            <select v-model="cubeMode">
              <option value="">all</option>
              <option value="data_shadow">data_shadow</option>
              <option value="historical">historical</option>
              <option value="strategy_shadow">strategy_shadow</option>
              <option value="sandbox">sandbox</option>
              <option value="live">live</option>
            </select>
          </label>
        </form>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>window</th>
                <th>instrument</th>
                <th>session</th>
                <th>timeframe</th>
                <th>side</th>
                <th>mode</th>
                <th>candidates</th>
                <th>blocked</th>
                <th>avg pnl</th>
                <th>confidence</th>
                <th>status</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in filteredCubeRows.slice(0, 50)" :key="row.cube_id">
                <td>{{ row.window_name }}</td>
                <td>{{ row.instrument_id }}</td>
                <td>{{ row.session_type }}</td>
                <td>{{ row.timeframe }}</td>
                <td>{{ row.side }}</td>
                <td>{{ row.mode }}</td>
                <td>{{ row.candidate_count }}</td>
                <td>{{ row.blocked_count }}</td>
                <td>{{ row.avg_net_pnl_proxy }}</td>
                <td>{{ row.confidence }}</td>
                <td><StatusPill :code="row.contour_status" compact /></td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="filteredCubeRows.length === 0" title="No rolling cube rows yet" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>contours</template>
        <template #title>Top contours</template>
        <div class="table-wrap">
          <table>
            <tbody>
              <tr v-for="row in topContours" :key="`${value(row, 'instrument_id')}-${value(row, 'timeframe')}-${value(row, 'side')}`">
                <td>{{ value(row, "instrument_id") }}</td>
                <td>{{ value(row, "session_type") }}</td>
                <td>{{ value(row, "timeframe") }}</td>
                <td>{{ value(row, "side") }}</td>
                <td>{{ value(row, "avg_net_pnl_proxy", value(row, "net_pnl_proxy")) }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="topContours.length === 0" title="No top contours yet" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>contours</template>
        <template #title>Dead contours</template>
        <div class="table-wrap">
          <table>
            <tbody>
              <tr v-for="row in deadContours" :key="`${value(row, 'instrument_id')}-${value(row, 'timeframe')}-${value(row, 'side')}`">
                <td>{{ value(row, "instrument_id") }}</td>
                <td>{{ value(row, "session_type") }}</td>
                <td>{{ value(row, "timeframe") }}</td>
                <td>{{ value(row, "side") }}</td>
                <td>{{ value(row, "sample_warning", "no signals") }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="deadContours.length === 0" title="No dead contours yet" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>proposals</template>
        <template #title>Candidate config proposals</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>candidate</th>
                <th>base</th>
                <th>proposed</th>
                <th>status</th>
                <th>approval</th>
                <th>actions</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="candidate in candidates" :key="candidate.candidate_config_id">
                <td>{{ candidate.candidate_config_id.slice(0, 8) }}</td>
                <td>{{ candidate.base_strategy_id }}</td>
                <td>{{ candidate.proposed_strategy_id }}</td>
                <td><StatusPill :code="candidate.status" compact /></td>
                <td>{{ candidate.approval_required ? "required" : "not_required" }}</td>
                <td>
                  <div class="row-actions">
                    <button
                      class="icon-button icon-button--good"
                      type="button"
                      :disabled="candidate.status !== 'draft' || observatoryLoading"
                      title="Approve for shadow only"
                      @click="approveForShadow(candidate)"
                    >
                      <Check :size="15" aria-hidden="true" />
                    </button>
                    <button
                      class="icon-button icon-button--danger"
                      type="button"
                      :disabled="candidate.status !== 'draft' || observatoryLoading"
                      title="Reject candidate"
                      @click="rejectCandidate(candidate)"
                    >
                      <X :size="15" aria-hidden="true" />
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="candidates.length === 0" title="No candidate proposals yet" />
        </div>
      </DataPanel>
    </div>

    <DataPanel>
      <template #eyebrow>historical analytics</template>
      <template #title>Calibration report filters</template>
      <form class="filter-grid" @submit.prevent="refresh">
        <label>
          <span>lookback_days</span>
          <input v-model.number="lookbackDays" type="number" min="1" max="3660" />
        </label>
        <label>
          <span>instruments</span>
          <input v-model="instruments" />
        </label>
        <label>
          <span>timeframes</span>
          <input v-model="timeframes" />
        </label>
        <label>
          <span>strategy_id</span>
          <input v-model="strategyId" />
        </label>
        <label>
          <span>calibration_scope</span>
          <select v-model="calibrationScope">
            <option value="primary_normal_days">primary_normal_days</option>
            <option value="special_days_only">special_days_only</option>
            <option value="all_days">all_days</option>
          </select>
        </label>
        <div class="filter-actions">
          <button class="icon-button" type="submit" :disabled="loading">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Calibration report</span>
          </button>
        </div>
      </form>
      <EmptyState v-if="loading" title="Building calibration report" />
      <EmptyState v-if="error" title="Calibration API degraded" :detail="error" tone="warn" />
    </DataPanel>

    <div class="metric-grid">
      <MetricTile label="candidates" :value="report?.candidate_count ?? 0" />
      <MetricTile label="approved" :value="report?.approved_count ?? 0" />
      <MetricTile label="blocked" :value="report?.blocked_count ?? 0" />
      <MetricTile label="pseudo orders" :value="report?.pseudo_order_count ?? 0" />
      <MetricTile label="gross pnl proxy" :value="report?.gross_simulated_pnl ?? '-'" />
      <MetricTile label="net pnl proxy" :value="report?.net_simulated_pnl ?? '-'" />
      <MetricTile label="clean" :value="report?.calibration_clean ? 'true' : 'false'" />
      <MetricTile label="special days" :value="report?.special_days_count ?? 0" />
      <MetricTile label="dividend sync" :value="report?.dividend_sync_status ?? '-'" />
      <MetricTile label="sync clean" :value="report?.dividend_sync_clean ? 'true' : 'false'" />
      <MetricTile label="future windows" :value="report?.future_dividend_windows_count ?? 0" />
    </div>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>scope</template>
        <template #title>Calibration cleanliness</template>
        <dl v-if="report" class="definition-grid">
          <dt>scope</dt>
          <dd>{{ report.calibration_scope }}</dd>
          <dt>data mode</dt>
          <dd>{{ report.calibration_data_mode }}</dd>
          <dt>clean</dt>
          <dd>{{ report.calibration_clean }}</dd>
          <dt>requires shadow</dt>
          <dd>{{ report.requires_shadow_live_calibration }}</dd>
          <dt>dividend sync</dt>
          <dd>{{ report.dividend_sync_status }}</dd>
          <dt>sync clean</dt>
          <dd>{{ report.dividend_sync_clean }}</dd>
          <dt>sync age hours</dt>
          <dd>{{ report.dividend_sync_age_hours ?? "-" }}</dd>
          <dt>sync failed instruments</dt>
          <dd>{{ report.dividend_sync_failed_instruments }}</dd>
          <dt>sync errors</dt>
          <dd>{{ report.dividend_sync_error_count }}</dd>
          <dt>ready for shadow</dt>
          <dd>{{ report.ready_for_shadow }}</dd>
          <dt>api dividends</dt>
          <dd>{{ report.api_import_dividend_events_count }}</dd>
          <dt>manual allowed</dt>
          <dd>{{ report.allow_manual_corporate_actions }}</dd>
          <dt>warnings</dt>
          <dd>{{ report.calibration_warnings.join(", ") || "-" }}</dd>
        </dl>
        <EmptyState v-else title="Run calibration report first" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>special days</template>
        <template #title>Normal vs special stats</template>
        <dl v-if="report" class="definition-grid">
          <dt>normal days</dt>
          <dd>{{ report.normal_days_count }}</dd>
          <dt>special days</dt>
          <dd>{{ report.special_days_count }}</dd>
          <dt>dividend gaps</dt>
          <dd>{{ report.dividend_gap_days_count }}</dd>
          <dt>corporate actions</dt>
          <dd>{{ report.corporate_action_days_count }}</dd>
          <dt>abnormal gaps</dt>
          <dd>{{ report.abnormal_gap_days_count }}</dd>
          <dt>excluded</dt>
          <dd>{{ report.excluded_from_primary_calibration_count }}</dd>
        </dl>
        <EmptyState v-else title="No special-day stats yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>blockers</template>
        <template #title>Blocker ranking</template>
        <MiniBars v-if="blockerBars.length" :rows="blockerBars" />
        <EmptyState v-else title="No blocker ranking yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>best / worst</template>
        <template #title>Scope summary</template>
        <dl v-if="report" class="definition-grid">
          <dt>best session</dt>
          <dd>{{ report.best_session_type ?? "-" }}</dd>
          <dt>worst session</dt>
          <dd>{{ report.worst_session_type ?? "-" }}</dd>
          <dt>best timeframe</dt>
          <dd>{{ report.best_timeframe ?? "-" }}</dd>
          <dt>worst timeframe</dt>
          <dd>{{ report.worst_timeframe ?? "-" }}</dd>
          <dt>best instrument</dt>
          <dd>{{ report.best_instrument ?? "-" }}</dd>
          <dt>worst instrument</dt>
          <dd>{{ report.worst_instrument ?? "-" }}</dd>
        </dl>
        <EmptyState v-else title="Run calibration report first" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>counterfactual</template>
        <template #title>Missed / avoided</template>
        <dl v-if="report" class="definition-grid">
          <dt>would_profit_15m</dt>
          <dd>{{ report.missed_opportunity_summary.would_profit_15m ?? 0 }}</dd>
          <dt>missed_net_pnl</dt>
          <dd>{{ report.missed_opportunity_summary.missed_net_pnl ?? "0" }}</dd>
          <dt>avoided_loss</dt>
          <dd>{{ report.avoided_loss_summary.avoided_loss ?? "0" }}</dd>
          <dt>assumed fees</dt>
          <dd>{{ report.total_assumed_fees }}</dd>
          <dt>assumed slippage</dt>
          <dd>{{ report.total_assumed_slippage }}</dd>
        </dl>
        <EmptyState v-else title="No counterfactual calibration yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>recommendations</template>
        <template #title>Threshold changes</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>threshold</th>
                <th>recommendation</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in thresholdRows" :key="row.key">
                <td>{{ row.key }}</td>
                <td><StatusPill :code="row.value" compact /></td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="thresholdRows.length === 0" title="No threshold recommendations" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>recommendations</template>
        <template #title>Safe from candles</template>
        <div class="table-wrap">
          <table>
            <tbody>
              <tr v-for="[key, item] in safeRecommendations" :key="String(key)">
                <td>{{ key }}</td>
                <td>{{ item }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="safeRecommendations.length === 0" title="No candle-only recommendations" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>recommendations</template>
        <template #title>Needs shadow confirmation</template>
        <div class="table-wrap">
          <table>
            <tbody>
              <tr v-for="[key, item] in shadowRecommendations" :key="String(key)">
                <td>{{ key }}</td>
                <td>{{ item }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="shadowRecommendations.length === 0" title="No shadow-only recommendations" />
        </div>
      </DataPanel>
    </div>
  </section>
</template>

<style scoped>
.diagnostic-caveat {
  color: var(--color-text-secondary);
  margin: 0 0 var(--space-4);
}

.checkbox-row {
  align-items: center;
  grid-template-columns: auto 1fr;
}

.checkbox-row input {
  width: 16px;
}

.row-actions {
  display: inline-flex;
  gap: var(--space-2);
}
</style>

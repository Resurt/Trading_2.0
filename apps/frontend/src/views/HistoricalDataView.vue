<script setup lang="ts">
import { computed, ref } from "vue";
import { Play, RefreshCw } from "@lucide/vue";

import { apiClient } from "../api/client";
import type {
  DividendSyncStatusResponse,
  HistoricalQualityResponse,
  HistoricalRunResponse,
  InstrumentRegistryResponse,
  MarketSpecialDayClassificationResponse,
  MarketSpecialDayResponse,
} from "../api/types";
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import MiniBars from "../components/ui/MiniBars.vue";

const lookbackDays = ref(10);
const instruments = ref("SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T");
const timeframes = ref("1m,5m,10m,15m");
const strategyId = ref("baseline");
const loading = ref(false);
const error = ref("");
const quality = ref<HistoricalQualityResponse | null>(null);
const lastRun = ref<HistoricalRunResponse | null>(null);
const specialDays = ref<MarketSpecialDayResponse[]>([]);
const futureSpecialDays = ref<MarketSpecialDayResponse[]>([]);
const classification = ref<MarketSpecialDayClassificationResponse | null>(null);
const dividendSyncStatus = ref<DividendSyncStatusResponse | null>(null);
const dividendSyncSummary = ref<Record<string, unknown> | null>(null);
const instrumentRegistry = ref<InstrumentRegistryResponse[]>([]);
const instrumentResolveSummary = ref<Record<string, unknown> | null>(null);

const sourceBars = computed(() =>
  Object.entries(quality.value?.source_distribution ?? {}).map(([label, value]) => ({
    label,
    value,
    code: label,
  })),
);

const sessionBars = computed(() =>
  Object.entries(quality.value?.session_type_distribution ?? {}).map(([label, value]) => ({
    label,
    value,
    code: label,
  })),
);

const specialBars = computed(() =>
  Object.entries(quality.value?.special_day_distribution ?? {}).map(([label, value]) => ({
    label,
    value,
    code: label,
  })),
);

async function runQuality() {
  await withLoading(async () => {
    quality.value = await apiClient.historicalDataQuality({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
      timeframes: timeframes.value,
    });
    specialDays.value = await apiClient.marketSpecialDays({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
    });
    futureSpecialDays.value = await apiClient.futureMarketSpecialDays({
      instruments: instruments.value,
    });
    dividendSyncStatus.value = await apiClient.dividendSyncStatus({
      lookback_days: Math.max(lookbackDays.value, 730),
      instruments: instruments.value,
    });
    instrumentRegistry.value = await apiClient.instrumentsRegistry();
  });
}

async function resolveInstruments() {
  await withLoading(async () => {
    instrumentResolveSummary.value = await apiClient.resolveInstruments({
      instruments: instruments.value,
      class_code: "TQBR",
    });
    instrumentRegistry.value = await apiClient.instrumentsRegistry();
  });
}

async function syncDividends(dryRun: boolean) {
  await withLoading(async () => {
    dividendSyncSummary.value = await apiClient.syncTbankDividends({
      instruments: instruments.value,
      lookback_days: 730,
      lookahead_days: 365,
      dry_run: dryRun,
      classify_special_days: true,
    });
    dividendSyncStatus.value = await apiClient.dividendSyncStatus({
      lookback_days: 730,
      instruments: instruments.value,
    });
    futureSpecialDays.value = await apiClient.futureMarketSpecialDays({
      instruments: instruments.value,
    });
  });
}

async function classifySpecialDays() {
  await withLoading(async () => {
    classification.value = await apiClient.classifyMarketSpecialDays({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
      force_rebuild: true,
      include_future: true,
      lookahead_days: 365,
    });
    specialDays.value = await apiClient.marketSpecialDays({
      instruments: instruments.value,
    });
    quality.value = await apiClient.historicalDataQuality({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
      timeframes: timeframes.value,
    });
  });
}

async function runReplay(dryRun: boolean) {
  await withLoading(async () => {
    lastRun.value = await apiClient.runHistoricalReplay({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
      timeframes: "5m,10m,15m",
      strategy_id: strategyId.value,
      dry_run: dryRun,
    });
  });
}

async function runCounterfactual() {
  await withLoading(async () => {
    lastRun.value = await apiClient.runHistoricalCounterfactual({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
      timeframes: "5m,10m,15m",
      strategy_id: strategyId.value,
      force_rebuild: true,
    });
  });
}

async function runReports() {
  await withLoading(async () => {
    lastRun.value = await apiClient.runHistoricalReportRebuild({
      lookback_days: lookbackDays.value,
      strategy_id: strategyId.value,
      include_counterfactual: false,
      force_rebuild: true,
    });
  });
}

async function withLoading(action: () => Promise<void>) {
  loading.value = true;
  error.value = "";
  try {
    await action();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <section class="page-stack" data-testid="historical-data-page">
    <div class="page-heading">
      <h1>Historical Data</h1>
    </div>

    <DataPanel>
      <template #eyebrow>historical replay</template>
      <template #title>Data window</template>
      <form class="filter-grid" @submit.prevent="runQuality">
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
        <div class="filter-actions">
          <button class="icon-button" type="submit" :disabled="loading">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Quality</span>
          </button>
          <button class="icon-button" type="button" :disabled="loading" @click="classifySpecialDays">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Classify special days</span>
          </button>
          <button class="icon-button" type="button" :disabled="loading" @click="syncDividends(true)">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Dividend sync dry-run</span>
          </button>
          <button class="icon-button" type="button" :disabled="loading" @click="resolveInstruments">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Resolve T-Bank instruments</span>
          </button>
          <button class="icon-button" type="button" :disabled="loading" @click="syncDividends(false)">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Sync T-Bank dividends</span>
          </button>
          <button class="icon-button" type="button" :disabled="loading" @click="runReplay(true)">
            <Play :size="16" aria-hidden="true" />
            <span>Replay dry-run</span>
          </button>
          <button class="icon-button icon-button--good" type="button" :disabled="loading" @click="runReplay(false)">
            <Play :size="16" aria-hidden="true" />
            <span>Run replay</span>
          </button>
          <button class="icon-button" type="button" :disabled="loading" @click="runCounterfactual">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Counterfactual</span>
          </button>
          <button class="icon-button" type="button" :disabled="loading" @click="runReports">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Reports</span>
          </button>
        </div>
      </form>
      <EmptyState v-if="loading" title="Historical job is running" />
      <EmptyState v-if="error" title="Historical API degraded" :detail="error" tone="warn" />
    </DataPanel>

    <div class="metric-grid">
      <MetricTile label="coverage" :value="quality?.coverage_pct ?? '-'" />
      <MetricTile label="expected" :value="quality?.expected_candles ?? 0" />
      <MetricTile label="actual" :value="quality?.actual_candles ?? 0" />
      <MetricTile label="missing" :value="quality?.missing_intervals ?? 0" />
      <MetricTile label="duplicates" :value="quality?.duplicate_count ?? 0" />
      <MetricTile label="invalid OHLC" :value="quality?.invalid_ohlc_count ?? 0" />
      <MetricTile label="special days" :value="quality?.corporate_action_days_count ?? 0" />
      <MetricTile label="dividend gaps" :value="quality?.dividend_gap_days_count ?? 0" />
      <MetricTile label="api dividends" :value="quality?.api_import_dividend_events_count ?? 0" />
      <MetricTile label="future windows" :value="futureSpecialDays.length" />
    </div>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>instruments</template>
        <template #title>Registry readiness</template>
        <dl class="definition-grid">
          <dt>ready</dt>
          <dd>{{ instrumentRegistry.filter((item) => item.ready_for_broker_calls).length }}/{{ instrumentRegistry.length }}</dd>
          <dt>last resolve</dt>
          <dd>{{ instrumentResolveSummary?.source ?? "-" }}</dd>
        </dl>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ticker</th>
                <th>source</th>
                <th>status</th>
                <th>uid</th>
                <th>figi</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in instrumentRegistry" :key="item.instrument_id">
                <td>{{ item.ticker }}</td>
                <td>{{ item.source }}</td>
                <td>{{ item.resolution_status }}</td>
                <td>{{ item.instrument_uid_present ? "present" : "missing" }}</td>
                <td>{{ item.figi_present ? "present" : "missing" }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="instrumentRegistry.length === 0" title="No registry rows loaded" />
        </div>
        <EmptyState
          v-if="instrumentRegistry.some((item) => !item.ready_for_broker_calls)"
          title="Unresolved enabled instruments"
          detail="Resolve T-Bank instruments before dividend sync, real backfill, shadow or production."
          tone="warn"
        />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>corporate actions</template>
        <template #title>Special-day classification</template>
        <dl v-if="quality" class="definition-grid">
          <dt>status</dt>
          <dd>{{ quality.corporate_action_classification_status }}</dd>
          <dt>excluded days</dt>
          <dd>{{ quality.excluded_days_count }}</dd>
          <dt>included days</dt>
          <dd>{{ quality.included_days_count }}</dd>
          <dt>last classify</dt>
          <dd>{{ classification?.classification_status ?? "-" }}</dd>
          <dt>dividend sync</dt>
          <dd>{{ quality.dividend_sync_status }}</dd>
          <dt>api dividends</dt>
          <dd>{{ quality.api_import_dividend_events_count }}</dd>
        </dl>
        <MiniBars v-if="specialBars.length" :rows="specialBars" />
        <EmptyState
          v-if="quality?.corporate_action_classification_status === 'missing'"
          title="Special-day classification missing"
          detail="Run classification before final calibration."
          tone="warn"
        />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>quality</template>
        <template #title>Source distribution</template>
        <MiniBars v-if="sourceBars.length" :rows="sourceBars" />
        <EmptyState v-else title="Run quality report first" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>corporate actions</template>
        <template #title>Dividend sync</template>
        <dl class="definition-grid">
          <dt>status</dt>
          <dd>{{ dividendSyncStatus?.status ?? quality?.dividend_sync_status ?? "-" }}</dd>
          <dt>clean</dt>
          <dd>{{ dividendSyncStatus?.clean ?? quality?.dividend_sync_clean ?? "-" }}</dd>
          <dt>last sync</dt>
          <dd>{{ dividendSyncStatus?.finished_at ?? "-" }}</dd>
          <dt>age hours</dt>
          <dd>{{ dividendSyncStatus?.age_hours ?? "-" }}</dd>
          <dt>failed</dt>
          <dd>{{ dividendSyncStatus?.failed_instruments ?? quality?.dividend_sync_failed_instruments ?? 0 }}</dd>
          <dt>errors</dt>
          <dd>{{ dividendSyncStatus?.error_count ?? quality?.dividend_sync_error_count ?? 0 }}</dd>
          <dt>api_import</dt>
          <dd>{{ dividendSyncStatus?.api_import_dividend_events_count ?? 0 }}</dd>
          <dt>manual</dt>
          <dd>{{ dividendSyncStatus?.manual_dividend_events_count ?? 0 }}</dd>
          <dt>last result</dt>
          <dd>{{ dividendSyncSummary?.source ?? "-" }}</dd>
        </dl>
        <EmptyState
          v-if="quality?.quality_warnings?.includes('dividend_sync_missing')"
          title="Dividend sync missing"
          detail="Run T-Bank dividend sync before final calibration."
          tone="warn"
        />
        <EmptyState
          v-if="quality?.quality_warnings?.includes('manual_corporate_actions_only')"
          title="Manual corporate actions only"
          detail="Manual CSV/JSON is fallback; api_import is the primary path."
          tone="warn"
        />
        <EmptyState
          v-if="quality?.quality_warnings?.includes('dividend_sync_completed_with_errors')"
          title="Dividend sync partial"
          detail="At least one instrument failed; this is not clean for calibration."
          tone="warn"
        />
        <EmptyState
          v-if="quality?.quality_warnings?.includes('dividend_sync_failed')"
          title="Dividend sync failed"
          detail="Clean dividend calendar is required before shadow or production."
          tone="warn"
        />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>corporate actions</template>
        <template #title>Special days</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>date</th>
                <th>instrument</th>
                <th>type</th>
                <th>policy</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in specialDays" :key="item.special_day_id">
                <td>{{ item.trading_date }}</td>
                <td>{{ item.instrument_id }}</td>
                <td>{{ item.special_day_type }}</td>
                <td>{{ item.trade_policy }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="specialDays.length === 0" title="No special days loaded" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>corporate actions</template>
        <template #title>Future dividend risk windows</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>date</th>
                <th>instrument</th>
                <th>type</th>
                <th>policy</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in futureSpecialDays" :key="item.special_day_id">
                <td>{{ item.trading_date }}</td>
                <td>{{ item.instrument_id }}</td>
                <td>{{ item.special_day_type }}</td>
                <td>{{ item.trade_policy }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="futureSpecialDays.length === 0" title="No future risk windows" />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>quality</template>
        <template #title>Session split</template>
        <MiniBars v-if="sessionBars.length" :rows="sessionBars" />
        <EmptyState v-else title="No session distribution yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>last run</template>
        <template #title>Replay / rebuild summary</template>
        <dl v-if="lastRun" class="definition-grid">
          <dt>source</dt>
          <dd>{{ lastRun.source }}</dd>
          <dt>dry_run</dt>
          <dd>{{ lastRun.dry_run ?? false }}</dd>
          <dt>bars_processed</dt>
          <dd>{{ lastRun.bars_processed ?? 0 }}</dd>
          <dt>candidates_created</dt>
          <dd>{{ lastRun.candidates_created ?? 0 }}</dd>
          <dt>pseudo_orders_created</dt>
          <dd>{{ lastRun.pseudo_orders_created ?? 0 }}</dd>
          <dt>real_orders_disabled</dt>
          <dd>{{ lastRun.real_orders_disabled ?? true }}</dd>
        </dl>
        <EmptyState v-else title="No historical run yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>quality</template>
        <template #title>Instrument/timeframe checks</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>instrument</th>
                <th>timeframe</th>
                <th>coverage</th>
                <th>missing</th>
                <th>invalid</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in quality?.instrument_timeframes ?? []" :key="`${item.instrument_id}-${item.timeframe}`">
                <td>{{ item.instrument_id }}</td>
                <td>{{ item.timeframe }}</td>
                <td>{{ item.coverage_pct }}</td>
                <td>{{ item.missing_count }}</td>
                <td>{{ item.invalid_ohlc_count }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="!quality?.instrument_timeframes?.length" title="No quality rows" />
        </div>
      </DataPanel>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { Play, RefreshCw } from "@lucide/vue";

import { apiClient } from "../api/client";
import type { HistoricalQualityResponse, HistoricalRunResponse } from "../api/types";
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import MiniBars from "../components/ui/MiniBars.vue";

const lookbackDays = ref(10);
const instruments = ref("SBER,GAZP");
const timeframes = ref("1m,5m,10m,15m");
const strategyId = ref("baseline");
const loading = ref(false);
const error = ref("");
const quality = ref<HistoricalQualityResponse | null>(null);
const lastRun = ref<HistoricalRunResponse | null>(null);

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

async function runQuality() {
  await withLoading(async () => {
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
    </div>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>quality</template>
        <template #title>Source distribution</template>
        <MiniBars v-if="sourceBars.length" :rows="sourceBars" />
        <EmptyState v-else title="Run quality report first" />
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

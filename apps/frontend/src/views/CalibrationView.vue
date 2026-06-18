<script setup lang="ts">
import { computed, ref } from "vue";
import { RefreshCw } from "@lucide/vue";

import { apiClient } from "../api/client";
import type { CalibrationResponse } from "../api/types";
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import MiniBars from "../components/ui/MiniBars.vue";
import StatusPill from "../components/ui/StatusPill.vue";

const lookbackDays = ref(10);
const instruments = ref("SBER,GAZP");
const timeframes = ref("5m,10m,15m");
const strategyId = ref("baseline");
const loading = ref(false);
const error = ref("");
const report = ref<CalibrationResponse | null>(null);

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

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    report.value = await apiClient.calibrationReport({
      lookback_days: lookbackDays.value,
      instruments: instruments.value,
      timeframes: timeframes.value,
      strategy_id: strategyId.value,
    });
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <section class="page-stack" data-testid="calibration-page">
    <div class="page-heading">
      <h1>Calibration</h1>
      <div class="heading-status">
        <StatusPill :code="report?.source ?? 'not_loaded'" />
      </div>
    </div>

    <DataPanel>
      <template #eyebrow>historical analytics</template>
      <template #title>Calibration filters</template>
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
    </div>

    <div class="reports-grid">
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
    </div>
  </section>
</template>

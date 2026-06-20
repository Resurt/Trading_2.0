<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RefreshCw } from "@lucide/vue";

import { apiClient } from "../api/client";
import type { IntradayAnalyticsSnapshotResponse, JsonPayload } from "../api/types";
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import StatusPill from "../components/ui/StatusPill.vue";

type Row = Record<string, unknown>;

const sessionTabs = [
  { label: "morning", value: "weekday_morning" },
  { label: "main", value: "weekday_main" },
  { label: "evening", value: "weekday_evening" },
  { label: "weekend", value: "weekend" },
];

const selectedSession = ref("weekday_main");
const tradingDate = ref(new Date().toISOString().slice(0, 10));
const selectedMode = ref("all");
const loading = ref(false);
const error = ref("");
const snapshot = ref<IntradayAnalyticsSnapshotResponse | null>(null);

const sessionStatuses = computed<Record<string, string>>(() => {
  const payload = snapshot.value?.payload.session_statuses;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    return payload as Record<string, string>;
  }
  return {};
});

const topInstruments = computed(() =>
  rowsFromPayload(snapshot.value?.payload.top_instruments).slice(0, 6),
);

const weakInstruments = computed(() =>
  rowsFromPayload(snapshot.value?.payload.weak_instruments).slice(0, 6),
);

const contourRows = computed(() => snapshot.value?.contour_rows ?? []);
const hourRows = computed(() => snapshot.value?.hour_summaries ?? []);
const microSessionRows = computed(() => snapshot.value?.micro_sessions ?? []);

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    snapshot.value = await apiClient.intradaySession({
      trading_date: tradingDate.value,
      session_type: selectedSession.value,
      mode: selectedMode.value,
    });
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

async function refreshToday() {
  loading.value = true;
  error.value = "";
  try {
    snapshot.value = await apiClient.intradayToday({ mode: selectedMode.value });
    if (snapshot.value.trading_date) {
      tradingDate.value = snapshot.value.trading_date;
    }
    if (snapshot.value.session_type) {
      selectedSession.value = snapshot.value.session_type;
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function statusFor(sessionType: string): string {
  return sessionStatuses.value[sessionType] ?? "not_started";
}

function value(row: JsonPayload, key: string, fallback = "-"): string {
  const raw = row[key];
  if (raw === null || raw === undefined || raw === "") {
    return fallback;
  }
  return String(raw);
}

function rowsFromPayload(value: unknown): Row[] {
  return Array.isArray(value)
    ? value.filter((row): row is Row => row !== null && typeof row === "object")
    : [];
}

onMounted(() => {
  void refreshToday();
});
</script>

<template>
  <section class="page-stack" data-testid="intraday-analytics-page">
    <div class="page-heading">
      <h1>Intraday Analytics</h1>
      <div class="heading-status">
        <StatusPill :code="snapshot?.market_activity ?? 'not_loaded'" />
      </div>
    </div>

    <DataPanel>
      <template #eyebrow>diagnostic only</template>
      <template #title>Current trading day</template>
      <template #action>
        <button class="icon-button" type="button" :disabled="loading" @click="refreshToday">
          <RefreshCw :size="16" aria-hidden="true" />
          <span>Today</span>
        </button>
      </template>
      <p class="diagnostic-caveat">
        Intraday analytics is diagnostic only. It does not enable trading.
      </p>
      <form class="filter-grid filter-grid--dense" @submit.prevent="refresh">
        <label>
          <span>trading_date</span>
          <input v-model="tradingDate" type="date" />
        </label>
        <label>
          <span>mode</span>
          <select v-model="selectedMode">
            <option value="all">all</option>
            <option value="data_shadow">data_shadow</option>
            <option value="historical">historical</option>
            <option value="strategy_shadow">strategy_shadow</option>
          </select>
        </label>
        <div class="filter-actions">
          <button class="icon-button" type="submit" :disabled="loading">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Refresh</span>
          </button>
        </div>
      </form>
      <div class="session-tabs" aria-label="intraday sessions">
        <button
          v-for="tab in sessionTabs"
          :key="tab.value"
          type="button"
          class="session-tab"
          :class="{ 'session-tab--active': selectedSession === tab.value }"
          @click="
            selectedSession = tab.value;
            refresh();
          "
        >
          <span>{{ tab.label }}</span>
          <StatusPill :code="statusFor(tab.value)" compact />
        </button>
      </div>
      <EmptyState v-if="loading" title="Building intraday analytics" />
      <EmptyState v-if="error" title="Intraday API degraded" :detail="error" tone="warn" />
    </DataPanel>

    <div class="metric-grid">
      <MetricTile label="trading date" :value="snapshot?.trading_date ?? tradingDate" />
      <MetricTile label="session" :value="snapshot?.session_type ?? selectedSession" />
      <MetricTile label="bias" :value="snapshot?.market_bias ?? '-'" />
      <MetricTile label="activity" :value="snapshot?.market_activity ?? '-'" />
      <MetricTile label="trend strength" :value="snapshot?.trend_strength ?? '-'" />
      <MetricTile label="candidates" :value="snapshot?.candidate_count ?? 0" />
      <MetricTile label="near misses" :value="snapshot?.near_miss_count ?? 0" />
      <MetricTile label="blockers" :value="snapshot?.blocked_count ?? 0" />
      <MetricTile label="avg spread bps" :value="snapshot?.avg_spread_bps ?? '-'" />
      <MetricTile label="avg depth" :value="snapshot?.avg_depth ?? '-'" />
      <MetricTile label="avg imbalance" :value="snapshot?.avg_imbalance ?? '-'" />
      <MetricTile label="stale incidents" :value="snapshot?.stale_incidents ?? 0" />
    </div>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>session</template>
        <template #title>Market summary</template>
        <dl v-if="snapshot" class="definition-grid">
          <dt>phase</dt>
          <dd>{{ snapshot.session_phase ?? "-" }}</dd>
          <dt>market bias</dt>
          <dd>{{ snapshot.market_bias }}</dd>
          <dt>market activity</dt>
          <dd>{{ snapshot.market_activity }}</dd>
          <dt>spread p95</dt>
          <dd>{{ snapshot.p95_spread_bps ?? "-" }}</dd>
          <dt>quality</dt>
          <dd>{{ snapshot.avg_market_quality ?? "-" }}</dd>
          <dt>no-trade reason</dt>
          <dd>{{ snapshot.no_trade_reason ?? "-" }}</dd>
          <dt>warnings</dt>
          <dd>{{ snapshot.warnings.join(", ") || "-" }}</dd>
        </dl>
        <EmptyState v-else title="No intraday snapshot yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>instruments</template>
        <template #title>Top instruments</template>
        <div class="compact-list" v-if="topInstruments.length">
          <div v-for="row in topInstruments" :key="value(row, 'instrument_id')">
            <strong>{{ value(row, "instrument_id") }}</strong>
            <span>{{ value(row, "market_bias") }} / {{ value(row, "market_activity") }}</span>
          </div>
        </div>
        <EmptyState v-else title="No top instruments yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>instruments</template>
        <template #title>Weak instruments</template>
        <div class="compact-list" v-if="weakInstruments.length">
          <div v-for="row in weakInstruments" :key="value(row, 'instrument_id')">
            <strong>{{ value(row, "instrument_id") }}</strong>
            <span>{{ value(row, "no_trade_reason") }}</span>
          </div>
        </div>
        <EmptyState v-else title="No weak instruments yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>micro sessions</template>
        <template #title>Hour and micro-session summaries</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>bucket</th>
                <th>activity</th>
                <th>bias</th>
                <th>candidates</th>
                <th>near misses</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in hourRows" :key="value(row, 'hour_bucket')">
                <td>{{ value(row, "hour_bucket") }}</td>
                <td><StatusPill :code="value(row, 'market_activity')" compact /></td>
                <td>{{ value(row, "market_bias") }}</td>
                <td>{{ value(row, "candidate_count", "0") }}</td>
                <td>{{ value(row, "near_miss_count", "0") }}</td>
              </tr>
              <tr v-for="row in microSessionRows" :key="value(row, 'micro_session_id')">
                <td>{{ value(row, "micro_session_id") }}</td>
                <td><StatusPill :code="value(row, 'market_activity')" compact /></td>
                <td>{{ value(row, "market_bias") }}</td>
                <td>{{ value(row, "candidate_count", "0") }}</td>
                <td>{{ value(row, "near_miss_count", "0") }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-if="hourRows.length === 0 && microSessionRows.length === 0"
            title="No hour or micro-session rows"
          />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>contours</template>
        <template #title>Instrument x timeframe x side</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>instrument</th>
                <th>timeframe</th>
                <th>side</th>
                <th>bias</th>
                <th>activity</th>
                <th>candidates</th>
                <th>blocked</th>
                <th>near miss</th>
                <th>spread</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in contourRows"
                :key="`${value(row, 'instrument_id')}-${value(row, 'timeframe')}-${value(row, 'side')}`"
              >
                <td>{{ value(row, "instrument_id") }}</td>
                <td>{{ value(row, "timeframe") }}</td>
                <td>{{ value(row, "side") }}</td>
                <td>{{ value(row, "market_bias") }}</td>
                <td><StatusPill :code="value(row, 'market_activity')" compact /></td>
                <td>{{ value(row, "candidate_count", "0") }}</td>
                <td>{{ value(row, "blocked_count", "0") }}</td>
                <td>{{ value(row, "near_miss_count", "0") }}</td>
                <td>{{ value(row, "avg_spread_bps") }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-if="contourRows.length === 0" title="No contour rows yet" />
        </div>
      </DataPanel>
    </div>
  </section>
</template>

<style scoped>
.diagnostic-caveat {
  margin: 0 0 16px;
  color: var(--color-text-secondary);
}

.session-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
}

.session-tab {
  align-items: center;
  background: var(--color-surface-raised);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  color: var(--color-text-primary);
  display: inline-flex;
  gap: 8px;
  min-height: 38px;
  padding: 6px 10px;
}

.session-tab--active {
  border-color: var(--color-active);
}

.compact-list {
  display: grid;
  gap: 10px;
}

.compact-list > div {
  align-items: center;
  border-bottom: 1px solid var(--color-border);
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 8px;
}
</style>

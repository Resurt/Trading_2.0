<script setup lang="ts">
import { computed, onMounted } from "vue";
import { RefreshCw } from "@lucide/vue";

import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import MiniBars from "../components/ui/MiniBars.vue";
import StatusPill from "../components/ui/StatusPill.vue";
import { useReportsStore } from "../stores/reports";
import { formatDecimal, formatMoney, formatPercentRatio, nestedRecord } from "../utils/format";

const reports = useReportsStore();

const trend = computed(() => nestedRecord(reports.latestDaily?.payload ?? {}, "trend"));
const executionQuality = computed(() =>
  nestedRecord(reports.latestDaily?.payload ?? {}, "execution_quality"),
);
const funnel = computed(() => nestedRecord(reports.latestDaily?.payload ?? {}, "funnel"));

const sessionBars = computed(() => toBars(reports.summaryBySession));
const instrumentBars = computed(() => toBars(reports.summaryByInstrument));
const timeframeBars = computed(() => toBars(reports.summaryByTimeframe));
const blockerBars = computed(() =>
  reports.blockerRanking.map((row) => ({
    label: String(row.reason_code ?? "unknown"),
    value: Number(row.count ?? 0),
    code: String(row.reason_code ?? "unknown"),
  })),
);
const counterfactualBars = computed(() =>
  reports.counterfactualHorizonRows.map((row) => ({
    label: row.label,
    value: row.value,
    code: `MFE ${formatDecimal(row.mfe, 1)} / MAE ${formatDecimal(row.mae, 1)}`,
  })),
);

function toBars(record: Record<string, unknown>) {
  return Object.entries(record).map(([label, value]) => {
    const payload = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
    return {
      label,
      value: Number(payload.signal_count ?? payload.count ?? 0),
      code: label,
    };
  });
}

onMounted(() => {
  void reports.fetchReports();
  void reports.connectReportsSocket();
});
</script>

<template>
  <section class="page-stack" data-testid="reports-page">
    <div class="page-heading">
      <h1>Reports</h1>
      <div class="heading-status">
        <StatusPill :code="reports.liveConnection" label="reports ws" />
        <StatusPill :code="reports.latestDaily?.market_regime" />
      </div>
    </div>

    <DataPanel>
      <template #eyebrow>filters</template>
      <template #title>Report filters</template>
      <form class="filter-grid" @submit.prevent="reports.fetchReports">
        <label>
          <span>trading_date</span>
          <input v-model="reports.filters.tradingDate" type="date" />
        </label>
        <label>
          <span>instrument</span>
          <input v-model="reports.filters.instrumentId" placeholder="MOEX:SBER" />
        </label>
        <label>
          <span>timeframe</span>
          <select v-model="reports.filters.timeframe">
            <option value="">all</option>
            <option value="5m">5m</option>
            <option value="10m">10m</option>
            <option value="15m">15m</option>
          </select>
        </label>
        <label>
          <span>session_type</span>
          <select v-model="reports.filters.sessionType">
            <option value="">all</option>
            <option value="weekday_morning">weekday_morning</option>
            <option value="weekday_main">weekday_main</option>
            <option value="weekday_evening">weekday_evening</option>
            <option value="weekend">weekend</option>
          </select>
        </label>
        <label>
          <span>blocker_code</span>
          <input v-model="reports.filters.blockerCode" placeholder="spread_too_wide" />
        </label>
        <label>
          <span>strategy_id</span>
          <input v-model="reports.filters.strategyId" />
        </label>
        <label>
          <span>strategy_version</span>
          <input v-model="reports.filters.strategyVersion" inputmode="numeric" placeholder="all" />
        </label>
        <div class="filter-actions">
          <button class="icon-button" type="submit" :disabled="reports.loading">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>{{ reports.loading ? "Loading" : "Refresh" }}</span>
          </button>
          <button class="icon-button icon-button--good" type="button" @click="reports.rebuildDailyReport">
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Rebuild daily</span>
          </button>
          <button
            v-if="reports.latestHourly"
            class="icon-button"
            type="button"
            @click="reports.rebuildReport('hourly', reports.latestHourly.micro_session_id)"
          >
            <RefreshCw :size="16" aria-hidden="true" />
            <span>Rebuild hour</span>
          </button>
        </div>
      </form>
      <EmptyState
        v-if="reports.loading"
        title="Loading reports"
        detail="BFF is reading report snapshots and analytics marts."
      />
      <EmptyState v-if="reports.error" title="Reports degraded" :detail="reports.error" tone="warn" />
      <p v-if="reports.latestJob" class="job-status">
        <button class="inline-action" type="button" @click="reports.refreshLatestJobStatus">check status</button>
        {{ reports.latestJob.status }} · {{ reports.latestJob.task_name }} · {{ reports.latestJob.job_id }}
      </p>
      <p v-if="reports.latestJobStatus" class="job-status">
        job status: {{ reports.latestJobStatus.status }}
        <span v-if="reports.latestJobStatus.error">error: {{ reports.latestJobStatus.error }}</span>
      </p>
    </DataPanel>

    <div class="metric-grid">
      <MetricTile
        label="Market regime"
        :value="String(trend.market_regime ?? reports.latestDaily?.market_regime ?? 'Нет данных')"
        :code="reports.latestDaily?.market_regime"
        :detail="String(trend.algorithm ?? 'daily regime card')"
      />
      <MetricTile
        label="Average return"
        :value="formatDecimal(String(trend.average_return_bps ?? ''), 2)"
        detail="bps"
      />
      <MetricTile
        label="Fill ratio"
        :value="formatPercentRatio(String(executionQuality.fill_ratio ?? reports.latestDaily?.fill_ratio ?? ''))"
      />
      <MetricTile
        label="Candidates"
        :value="Number(funnel.candidates ?? reports.latestDaily?.signal_count ?? 0)"
        :detail="`blocked ${Number(funnel.blockers ?? reports.latestDaily?.blocked_count ?? 0)}`"
      />
    </div>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>daily regime</template>
        <template #title>Daily regime card</template>
        <dl v-if="reports.latestDaily" class="definition-grid">
          <dt>regime</dt>
          <dd><StatusPill :code="reports.latestDaily.market_regime" /></dd>
          <dt>instrument</dt>
          <dd>{{ reports.latestDaily.instrument_id ?? "all" }}</dd>
          <dt>timeframe</dt>
          <dd>{{ reports.latestDaily.timeframe ?? "all" }}</dd>
          <dt>explainability</dt>
          <dd>
            {{ String(trend.explanation ?? reports.latestDaily.payload.explainability ?? "stored in report payload") }}
          </dd>
        </dl>
        <EmptyState v-else title="No daily regime yet" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>daily</template>
        <template #title>Daily reports</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>trading_date</th>
                <th>market_regime</th>
                <th>signals</th>
                <th>blocked</th>
                <th>realised_pnl</th>
                <th>commission</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="report in reports.dailyReports" :key="report.daily_report_id">
                <td>{{ report.trading_date }}</td>
                <td><StatusPill :code="report.market_regime" compact /></td>
                <td>{{ report.signal_count }}</td>
                <td>{{ report.blocked_count }}</td>
                <td>{{ formatMoney(report.realised_pnl) }}</td>
                <td>{{ formatMoney(report.commission) }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-if="reports.dailyReports.length === 0"
            title="Daily reports отсутствуют"
            detail="Запустите rebuild daily report или проверьте фильтры."
          />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>hourly</template>
        <template #title>Hourly reports</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>micro_session_id</th>
                <th>session</th>
                <th>signals</th>
                <th>blocked</th>
                <th>fill_ratio</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="report in reports.hourlyReports" :key="report.hourly_report_id">
                <td>{{ report.micro_session_id }}</td>
                <td>{{ report.session_type }}</td>
                <td>{{ report.signal_count }}</td>
                <td>{{ report.blocked_count }}</td>
                <td>{{ formatPercentRatio(report.fill_ratio) }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-if="reports.hourlyReports.length === 0"
            title="Hourly reports отсутствуют"
            detail="Они появляются после закрытия micro-session."
          />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>hourly</template>
        <template #title>Micro-session timeline</template>
        <MiniBars v-if="reports.hourlyTimelineBars.length" :rows="reports.hourlyTimelineBars" />
        <EmptyState
          v-else
          title="Timeline is empty"
          detail="Hourly reports will form a micro-session timeline after rollovers."
        />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>funnel</template>
        <template #title>Candidate funnel</template>
        <MiniBars v-if="reports.candidateFunnelBars.length" :rows="reports.candidateFunnelBars" />
        <EmptyState
          v-else
          title="Candidate funnel is empty"
          detail="signal_candidate and order lifecycle facts are not available for these filters."
        />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>blockers</template>
        <template #title>Blocker ranking</template>
        <MiniBars v-if="blockerBars.length" :rows="blockerBars" />
        <EmptyState
          v-else
          title="Blocker ranking пуст"
          detail="Нет заблокированных candidates в выбранных отчетах."
        />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>counterfactual</template>
        <template #title>Missed opportunities</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>instrument</th>
                <th>source</th>
                <th>reason</th>
                <th>5m</th>
                <th>10m</th>
                <th>15m</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="result in reports.missedOpportunities" :key="result.counterfactual_result_id">
                <td>{{ result.instrument_id }}</td>
                <td>{{ result.source_event_type }}</td>
                <td>{{ result.blocker_code ?? result.cancel_reason_code ?? "Нет причины" }}</td>
                <td>{{ result.would_profit_5m ? "profit" : "no" }}</td>
                <td>{{ result.would_profit_10m ? "profit" : "no" }}</td>
                <td>{{ result.would_profit_15m ? "profit" : "no" }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-if="reports.missedOpportunities.length === 0"
            title="Упущенных возможностей нет"
            detail="counterfactual_result пока не показал profitable windows."
          />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>blockers</template>
        <template #title>Что нас тормозит</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>blocker_code</th>
                <th>count</th>
                <th>missed net</th>
                <th>false positive</th>
                <th>explainability</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in reports.blockerAnalytics.rows" :key="row.blocker_code">
                <td><StatusPill :code="row.blocker_code" compact /></td>
                <td>{{ row.count }}</td>
                <td>{{ formatMoney(row.missed_pnl_net) }}</td>
                <td>{{ formatPercentRatio(row.false_positive_rate) }}</td>
                <td>
                  {{
                    String(
                      row.explanation_payload.summary ??
                        row.explanation_payload.reason ??
                        row.blocker_family ??
                        "structured details",
                    )
                  }}
                </td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-if="reports.blockerAnalytics.rows.length === 0"
            title="No blocker analytics"
            detail="No blocker_event facts matched the selected filters."
          />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>execution</template>
        <template #title>Canceled order diagnostics</template>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>cancel_reason</th>
                <th>count</th>
                <th>missed net</th>
                <th>+5m</th>
                <th>+10m</th>
                <th>+15m</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in reports.canceledDiagnosticsRows" :key="row.cancel_reason_code">
                <td><StatusPill :code="row.cancel_reason_code" compact /></td>
                <td>{{ row.count }}</td>
                <td>{{ formatMoney(row.missed_pnl_net) }}</td>
                <td>{{ row.would_profit_5m_count }}</td>
                <td>{{ row.would_profit_10m_count }}</td>
                <td>{{ row.would_profit_15m_count }}</td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-if="reports.canceledDiagnosticsRows.length === 0"
            title="No canceled-order diagnostics"
            detail="Canceled order counterfactuals are not available for these filters."
          />
        </div>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>counterfactual</template>
        <template #title>Counterfactual horizons</template>
        <MiniBars v-if="counterfactualBars.length" :rows="counterfactualBars" />
        <EmptyState
          v-else
          title="No horizon analytics"
          detail="Counterfactual results for +5m / +10m / +15m are not available yet."
        />
      </DataPanel>
    </div>

    <div class="reports-grid reports-grid--three">
      <DataPanel>
        <template #eyebrow>summary</template>
        <template #title>By session</template>
        <MiniBars v-if="sessionBars.length" :rows="sessionBars" />
        <EmptyState v-else title="Нет session summary" />
      </DataPanel>
      <DataPanel>
        <template #eyebrow>summary</template>
        <template #title>By instrument</template>
        <MiniBars v-if="instrumentBars.length" :rows="instrumentBars" />
        <EmptyState v-else title="Нет instrument summary" />
      </DataPanel>
      <DataPanel>
        <template #eyebrow>summary</template>
        <template #title>By timeframe</template>
        <MiniBars v-if="timeframeBars.length" :rows="timeframeBars" />
        <EmptyState v-else title="Нет timeframe summary" />
      </DataPanel>
    </div>
  </section>
</template>

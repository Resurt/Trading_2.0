<script setup lang="ts">
import { computed, reactive } from "vue";

import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import StatusPill from "../components/ui/StatusPill.vue";
import { useMarketStore } from "../stores/market";
import { usePortfolioStore } from "../stores/portfolio";
import { useReportsStore } from "../stores/reports";
import { useRobotStore } from "../stores/robot";
import { compactDateTime } from "../utils/format";

const robot = useRobotStore();
const market = useMarketStore();
const portfolio = usePortfolioStore();
const reports = useReportsStore();

const correlation = reactive({
  run_id: "",
  micro_session_id: "",
  candidate_id: "",
  order_intent_id: "",
  request_order_id: "",
  exchange_order_id: "",
});

const degradedSources = computed(() => {
  const sources = [
    { source: "robot", error: robot.error },
    { source: "market", error: market.error },
    { source: "portfolio", error: portfolio.error },
    { source: "reports", error: reports.error },
  ];
  return sources.filter((item): item is { source: string; error: string } => Boolean(item.error));
});
</script>

<template>
  <section class="page-stack" data-testid="diagnostics-page">
    <div class="page-heading">
      <h1>Logs/Diagnostics</h1>
      <div class="heading-status">
        <StatusPill :code="robot.liveConnection" label="dashboard ws" />
        <StatusPill :code="market.liveConnection" label="market ws" />
        <StatusPill :code="reports.liveConnection" label="reports ws" />
      </div>
    </div>

    <div class="metric-grid">
      <MetricTile
        label="Market stream"
        :value="market.liveConnection"
        :code="market.liveConnection"
        :tone="market.liveConnection === 'degraded' ? 'bad' : 'info'"
      />
      <MetricTile
        label="Order stream"
        :value="portfolio.liveConnection"
        :code="portfolio.liveConnection"
        :tone="portfolio.liveConnection === 'degraded' ? 'bad' : 'info'"
      />
      <MetricTile
        label="Open orders"
        :value="portfolio.openOrders.length"
        :detail="`${portfolio.ordersWithReason.length} with reason codes`"
      />
      <MetricTile
        label="Active positions"
        :value="portfolio.activePositions.length"
        :detail="`${robot.status.active_positions_count} from robot status`"
      />
    </div>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>correlation</template>
        <template #title>Correlation search</template>
        <form class="filter-grid filter-grid--dense">
          <label v-for="(_, key) in correlation" :key="key">
            <span>{{ key }}</span>
            <input v-model="correlation[key]" :placeholder="String(key)" />
          </label>
        </form>
        <p class="diagnostic-note">
          Поля используются для поиска в Loki и сверки с PostgreSQL domain events.
        </p>
      </DataPanel>

      <DataPanel>
        <template #eyebrow>degraded</template>
        <template #title>Degraded sources</template>
        <div v-if="degradedSources.length" class="event-list">
          <div v-for="item in degradedSources" :key="item.source" class="event-row">
            <div>
              <strong>{{ item.source }}</strong>
              <span>{{ item.error }}</span>
            </div>
            <StatusPill code="degraded" compact />
          </div>
        </div>
        <EmptyState
          v-else
          title="Ошибок snapshot-загрузки нет"
          detail="Смотрите Loki для технических ошибок контейнеров."
        />
      </DataPanel>
    </div>

    <DataPanel>
      <template #eyebrow>operational</template>
      <template #title>Operational signals</template>
      <div class="diagnostics-grid">
        <dl class="definition-grid">
          <dt>session_type</dt>
          <dd><StatusPill :code="robot.status.session_type" compact /></dd>
          <dt>session_phase</dt>
          <dd><StatusPill :code="robot.status.session_phase" compact /></dd>
          <dt>broker_trading_status</dt>
          <dd><StatusPill :code="robot.status.broker_trading_status" compact /></dd>
          <dt>micro_session_id</dt>
          <dd>{{ robot.status.micro_session_id ?? "Нет данных" }}</dd>
        </dl>
        <dl class="definition-grid">
          <dt>dashboard_message_at</dt>
          <dd>{{ compactDateTime(robot.lastDashboardMessageAt) }}</dd>
          <dt>market_generated_at</dt>
          <dd>{{ compactDateTime(market.overview.generated_at) }}</dd>
          <dt>latest_daily_job</dt>
          <dd>{{ reports.latestJob?.status ?? "Нет jobs" }}</dd>
          <dt>degraded_flags</dt>
          <dd>{{ robot.status.degraded_flags.join(", ") || "none" }}</dd>
        </dl>
      </div>
    </DataPanel>

    <DataPanel>
      <template #eyebrow>reason codes</template>
      <template #title>Cancelled/rejected orders</template>
      <div v-if="portfolio.ordersWithReason.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>request_order_id</th>
              <th>exchange_order_id</th>
              <th>status</th>
              <th>cancel_reason_code</th>
              <th>reject_reason_code</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="order in portfolio.ordersWithReason" :key="order.request_order_id">
              <td><code>{{ order.request_order_id }}</code></td>
              <td>{{ order.exchange_order_id ?? "Нет данных" }}</td>
              <td><StatusPill :code="order.broker_status" compact /></td>
              <td>{{ order.cancel_reason_code ?? "none" }}</td>
              <td>{{ order.reject_reason_code ?? "none" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <EmptyState
        v-else
        title="Нет отмененных/отклоненных заявок"
        detail="Когда появятся cancel/reject reason codes, они будут видны здесь."
      />
    </DataPanel>
  </section>
</template>

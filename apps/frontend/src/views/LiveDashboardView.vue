<script setup lang="ts">
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import StatusPill from "../components/ui/StatusPill.vue";
import OrderBookWidget from "../components/dashboard/OrderBookWidget.vue";
import RiskEventsList from "../components/dashboard/RiskEventsList.vue";
import SignalReasonCard from "../components/dashboard/SignalReasonCard.vue";
import { useMarketStore } from "../stores/market";
import { usePortfolioStore } from "../stores/portfolio";
import { useReportsStore } from "../stores/reports";
import { useRobotStore } from "../stores/robot";
import {
  compactDateTime,
  countdownFromMicroSession,
  formatDecimal,
  formatMoney,
  stringifyValue,
} from "../utils/format";
import { codeWithLabel } from "../utils/labels";

const robot = useRobotStore();
const market = useMarketStore();
const portfolio = usePortfolioStore();
const reports = useReportsStore();
</script>

<template>
  <section class="page-stack" data-testid="live-dashboard">
    <div class="page-heading">
      <h1>Live Dashboard</h1>
      <div class="heading-status">
        <StatusPill :code="robot.liveConnection" label="dashboard ws" />
        <StatusPill :code="market.liveConnection" label="market ws" />
        <StatusPill :code="portfolio.liveConnection" label="orders ws" />
      </div>
    </div>

    <div v-if="robot.error || market.error || portfolio.error" class="alert-row">
      <EmptyState
        v-if="robot.error"
        title="Robot snapshot degraded"
        :detail="robot.error"
        tone="warn"
      />
      <EmptyState
        v-if="market.error"
        title="Market snapshot degraded"
        :detail="market.error"
        tone="warn"
      />
      <EmptyState
        v-if="portfolio.error"
        title="Portfolio snapshot degraded"
        :detail="portfolio.error"
        tone="warn"
      />
    </div>

    <div class="metric-grid">
      <MetricTile
        label="Баланс"
        :value="formatMoney(robot.status.balance.available, robot.status.balance.currency)"
        :detail="`blocked ${formatMoney(robot.status.balance.blocked, robot.status.balance.currency)}`"
        :tone="robot.status.degraded_flags.includes('balance_unavailable') ? 'warn' : 'good'"
      />
      <MetricTile
        label="Session"
        :value="codeWithLabel(robot.status.session_type)"
        :code="robot.status.session_type"
      />
      <MetricTile
        label="Phase"
        :value="codeWithLabel(robot.status.session_phase)"
        :code="robot.status.session_phase"
      />
      <MetricTile
        label="Broker status"
        :value="codeWithLabel(robot.status.broker_trading_status)"
        :code="robot.status.broker_trading_status"
      />
      <MetricTile
        label="Micro-session"
        :value="robot.status.micro_session_id ?? 'Нет активной'"
        :detail="countdownFromMicroSession(robot.status.micro_session_id)"
      />
      <MetricTile
        label="Strategy state"
        :value="codeWithLabel(robot.status.strategy_state)"
        :code="robot.status.strategy_state"
      />
    </div>

    <div class="dashboard-layout">
      <div class="dashboard-layout__main">
        <DataPanel>
          <template #eyebrow>market</template>
          <template #title>Market overview</template>
          <template #action>
            <select v-model="market.selectedInstrumentId" class="compact-input">
              <option v-if="market.overview.instruments.length === 0" value="">Нет инструментов</option>
              <option
                v-for="instrument in market.overview.instruments"
                :key="instrument.instrument_id"
                :value="instrument.instrument_id"
              >
                {{ instrument.instrument_id }}
              </option>
            </select>
          </template>

          <div class="metric-grid metric-grid--compact">
            <MetricTile
              label="Spread"
              :value="formatDecimal(market.currentInstrument?.spread, 4)"
              tone="info"
            />
            <MetricTile
              label="Mid price"
              :value="formatDecimal(market.currentInstrument?.mid_price, 2)"
            />
            <MetricTile
              label="Market quality"
              :value="formatDecimal(market.currentInstrument?.market_quality, 3)"
              :tone="Number(market.currentInstrument?.market_quality ?? 0) >= 0.7 ? 'good' : 'warn'"
            />
            <MetricTile
              label="Top of book"
              :value="`${formatDecimal(market.topOfBook.bestBid, 2)} / ${formatDecimal(market.topOfBook.bestAsk, 2)}`"
            />
          </div>

          <OrderBookWidget :instrument="market.currentInstrument" />

          <div class="two-column">
            <div>
              <h3>Order book summary</h3>
              <div v-if="market.bookSummaryRows.length" class="kv-list">
                <div v-for="row in market.bookSummaryRows" :key="row.key">
                  <span>{{ row.key }}</span>
                  <strong>{{ row.value }}</strong>
                </div>
              </div>
              <EmptyState
                v-else
                title="Нет агрегатов стакана"
                detail="Ожидается order_book_summary из BFF."
              />
            </div>
            <div>
              <h3>Recent market trades</h3>
              <div v-if="market.recentTrades.length" class="tape">
                <div v-for="(trade, index) in market.recentTrades.slice(0, 8)" :key="index">
                  <span>{{ stringifyValue(trade.side) }}</span>
                  <strong>{{ stringifyValue(trade.price) }}</strong>
                  <small>{{ stringifyValue(trade.qty_lots) }} lots</small>
                </div>
              </div>
              <EmptyState
                v-else
                title="Лента сделок пуста"
                detail="market trades feed еще не прислал read model."
              />
            </div>
          </div>
        </DataPanel>

        <DataPanel>
          <template #eyebrow>portfolio</template>
          <template #title>Positions and active orders</template>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>instrument</th>
                  <th>side</th>
                  <th>lots</th>
                  <th>avg</th>
                  <th>market</th>
                  <th>unrealized</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="position in portfolio.positions" :key="`${position.instrument_id}:${position.account_id}`">
                  <td>{{ position.instrument_id }}</td>
                  <td>{{ position.position_side }}</td>
                  <td>{{ position.qty_lots }}</td>
                  <td>{{ formatDecimal(position.avg_price, 2) }}</td>
                  <td>{{ formatDecimal(position.market_price, 2) }}</td>
                  <td>{{ formatMoney(position.unrealized_pnl) }}</td>
                </tr>
              </tbody>
            </table>
            <EmptyState
              v-if="portfolio.positions.length === 0"
              title="Позиции отсутствуют"
              detail="position_snapshot еще не вернул активные позиции."
            />
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>request_order_id</th>
                  <th>instrument</th>
                  <th>side</th>
                  <th>status</th>
                  <th>reason</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="order in portfolio.openOrders" :key="order.request_order_id">
                  <td><code>{{ order.request_order_id }}</code></td>
                  <td>{{ order.instrument_id ?? "Нет данных" }}</td>
                  <td>{{ order.side ?? "Нет данных" }}</td>
                  <td><StatusPill :code="order.broker_status" compact /></td>
                  <td>{{ order.cancel_reason_code ?? order.reject_reason_code ?? "Нет причины" }}</td>
                </tr>
              </tbody>
            </table>
            <EmptyState
              v-if="portfolio.openOrders.length === 0"
              title="Открытых заявок нет"
              detail="broker_order не содержит активных ордеров."
            />
          </div>
        </DataPanel>
      </div>

      <div class="dashboard-layout__side">
        <DataPanel>
          <template #eyebrow>strategy</template>
          <template #title>Current signal</template>
          <SignalReasonCard :signal="robot.currentSignal" />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>risk</template>
          <template #title>Recent risk events</template>
          <RiskEventsList :signals="robot.signals" />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>health</template>
          <template #title>Degraded flags</template>
          <div v-if="robot.status.degraded_flags.length" class="pill-list">
            <StatusPill
              v-for="flag in robot.status.degraded_flags"
              :key="flag"
              :code="flag"
            />
          </div>
          <EmptyState v-else title="Деградаций нет" detail="BFF не вернул degraded flags." />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>reports</template>
          <template #title>Latest hourly report</template>
          <dl v-if="reports.latestHourly" class="definition-grid">
            <dt>trading_date</dt>
            <dd>{{ reports.latestHourly.trading_date }}</dd>
            <dt>micro_session_id</dt>
            <dd>{{ reports.latestHourly.micro_session_id }}</dd>
            <dt>signals</dt>
            <dd>{{ reports.latestHourly.signal_count }}</dd>
            <dt>blocked</dt>
            <dd>{{ reports.latestHourly.blocked_count }}</dd>
            <dt>fill_ratio</dt>
            <dd>{{ reports.latestHourly.fill_ratio ?? "Нет данных" }}</dd>
          </dl>
          <EmptyState
            v-else
            title="Hourly report еще не готов"
            detail="После закрытия micro-session отчет появится здесь."
          />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>timestamps</template>
          <template #title>Freshness</template>
          <dl class="definition-grid">
            <dt>dashboard ws</dt>
            <dd>{{ compactDateTime(robot.lastDashboardMessageAt) }}</dd>
            <dt>market generated</dt>
            <dd>{{ compactDateTime(market.overview.generated_at) }}</dd>
            <dt>session observed</dt>
            <dd>{{ compactDateTime(robot.session.observed_at) }}</dd>
          </dl>
        </DataPanel>
      </div>
    </div>
  </section>
</template>

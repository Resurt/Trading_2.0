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

function balanceValue(): string {
  if (robot.status.balance.balance_degraded) {
    return "Баланс недоступен";
  }
  return formatMoney(
    robot.status.balance.total_portfolio_value_rub ?? robot.status.balance.available,
    robot.status.balance.balance_currency ?? robot.status.balance.currency,
  );
}

function balanceDetail(): string {
  const currency = robot.status.balance.balance_currency ?? robot.status.balance.currency;
  if (robot.status.balance.balance_degraded) {
    return robot.status.balance.balance_degraded_reason_code ?? "broker_balance_unavailable";
  }
  const available = formatMoney(
    robot.status.balance.available_cash_rub ?? robot.status.balance.available,
    currency,
  );
  const blocked = formatMoney(
    robot.status.balance.blocked_cash_rub ?? robot.status.balance.blocked,
    currency,
  );
  const expected = formatMoney(robot.status.balance.expected_yield_rub, currency);
  const freshness = compactDateTime(robot.status.balance.last_balance_refresh_at);
  const account = robot.status.balance.account_id_masked ?? "account masked";
  return `свободно ${available} / блок ${blocked} / доход ${expected} / ${account} / ${freshness}`;
}

function balanceCode(): string | null {
  return robot.status.balance.balance_degraded
    ? robot.status.balance.balance_degraded_reason_code
    : robot.status.balance.account_id_masked;
}
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
        label="Портфель"
        :value="balanceValue()"
        :detail="balanceDetail()"
        :code="balanceCode()"
        :tone="robot.status.balance.balance_degraded ? 'warn' : 'good'"
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

    <div class="dashboard-command-row">
      <button
        class="icon-button"
        type="button"
        data-testid="refresh-balance"
        :disabled="robot.balanceRefreshLoading"
        @click="robot.refreshBalance"
      >
        {{ robot.balanceRefreshLoading ? "Обновление..." : "Обновить баланс" }}
      </button>
      <span>Баланс readonly для data-only режима. Торговлю не включает.</span>
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
          <template #eyebrow>collector</template>
          <template #title>Data-only Shadow Status</template>
          <dl class="definition-grid">
            <dt>enabled</dt>
            <dd><StatusPill :code="market.dataShadowStatus.enabled ? 'enabled' : 'disabled'" /></dd>
            <dt>strategy</dt>
            <dd>Strategy trading disabled: data-only shadow mode</dd>
            <dt>stream</dt>
            <dd><StatusPill :code="market.dataShadowStatus.stream_alive ? 'live' : 'idle'" /></dd>
            <dt>last age</dt>
            <dd>{{ market.dataShadowStatus.last_message_age_seconds ?? "no samples" }}</dd>
            <dt>snapshots</dt>
            <dd>{{ market.dataShadowStatus.market_microstructure_snapshots }}</dd>
            <dt>order books</dt>
            <dd>{{ market.dataShadowStatus.order_book_snapshots }}</dd>
            <dt>avg spread</dt>
            <dd>{{ formatDecimal(market.dataShadowStatus.avg_spread_bps, 4) }}</dd>
            <dt>p95 spread</dt>
            <dd>{{ formatDecimal(market.dataShadowStatus.p95_spread_bps, 4) }}</dd>
            <dt>quality</dt>
            <dd>{{ formatDecimal(market.dataShadowStatus.avg_market_quality_score, 3) }}</dd>
            <dt>session</dt>
            <dd>{{ market.dataShadowStatus.current_session ?? "unknown" }}</dd>
          </dl>
          <EmptyState
            v-if="market.dataShadowStatus.warning"
            title="Collector mode"
            :detail="market.dataShadowStatus.warning"
            tone="warn"
          />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>streams</template>
          <template #title>Stream health / reconnect</template>
          <dl class="definition-grid">
            <dt>dashboard</dt>
            <dd><StatusPill :code="robot.liveConnection" /></dd>
            <dt>market</dt>
            <dd><StatusPill :code="market.liveConnection" /></dd>
            <dt>orders</dt>
            <dd><StatusPill :code="portfolio.liveConnection" /></dd>
            <dt>reports</dt>
            <dd><StatusPill :code="reports.liveConnection" /></dd>
            <dt>reconnect status</dt>
            <dd>
              {{
                [robot.liveConnection, market.liveConnection, portfolio.liveConnection, reports.liveConnection].includes(
                  "degraded",
                )
                  ? "reconnect attention required"
                  : "no reconnect pressure"
              }}
            </dd>
          </dl>
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

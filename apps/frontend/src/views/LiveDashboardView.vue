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
    return robot.balanceRefreshLoading ? "Обновляю счёт..." : "Счёт не получен";
  }
  return formatMoney(
    robot.status.balance.total_portfolio_value_rub ?? robot.status.balance.available,
    robot.status.balance.balance_currency ?? robot.status.balance.currency,
  );
}

function balanceUnavailableReason(): string {
  const reason = robot.status.balance.balance_degraded_reason_code;
  const labels: Record<string, string> = {
    api_snapshot_unavailable: "API не успел вернуть данные. Автообновление счёта активно.",
    broker_balance_unavailable: "Нет сохранённых данных счёта. Запрашиваю T-Invest read-only.",
    broker_accounts_empty: "T-Invest не вернул брокерские счета для текущего токена.",
    broker_balance_timeout: "T-Invest не ответил за отведённое время.",
    broker_gateway_unavailable: "Broker gateway недоступен в API container.",
    broker_balance_refresh_failed: "Readonly refresh счёта завершился ошибкой.",
    position_snapshot_missing: "В базе ещё нет данных портфеля.",
  };
  return labels[reason ?? ""] ?? "Нет актуальных данных счёта от брокера.";
}

function balanceDetail(): string {
  const currency = robot.status.balance.balance_currency ?? robot.status.balance.currency;
  if (robot.status.balance.balance_degraded) {
    return balanceUnavailableReason();
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
  return `Свободно ${available} / блок ${blocked} / доход ${expected} / ${account} / ${freshness}`;
}

function balanceCode(): string | null {
  return robot.status.balance.balance_degraded ? null : robot.status.balance.account_id_masked;
}

function sessionValue(): string {
  if (robot.status.session_type === "weekend") {
    return "Выходная сессия";
  }
  if (robot.status.session_type === "closed") {
    return "Рынок закрыт";
  }
  if (robot.status.session_type === "unknown") {
    return "Сессия уточняется";
  }
  return codeWithLabel(robot.status.session_type);
}

function sessionDetail(): string {
  if (robot.status.session_type === "unknown") {
    return "Жду данные сессии или preflight календаря.";
  }
  return `Дата торгов: ${robot.session.trading_date ?? robot.session.calendar_date ?? "нет данных"}`;
}

function phaseValue(): string {
  if (robot.status.session_phase === "closed") {
    return "Торги закрыты";
  }
  if (robot.status.session_phase === "unknown") {
    return "Фаза уточняется";
  }
  return codeWithLabel(robot.status.session_phase);
}

function brokerStatusValue(): string {
  if (robot.status.broker_trading_status === "unknown") {
    return "Проверяется";
  }
  return codeWithLabel(robot.status.broker_trading_status);
}

function strategyValue(): string {
  if (market.dataShadowStatus.strategy_trading_disabled || market.dataShadowStatus.enabled) {
    return "Торговля отключена";
  }
  if (robot.status.strategy_state === "unknown" || robot.status.robot_control_state === "stopped") {
    return "Торговля не запущена";
  }
  return codeWithLabel(robot.status.strategy_state);
}

function strategyDetail(): string {
  return "Data-only режим: заявки и strategy shadow не запускаются.";
}

function microSessionValue(): string {
  return robot.status.micro_session_id ?? "Нет активного окна сбора";
}

function quoteSourceLabel(source: string | null): string {
  if (source === "live_order_book") {
    return "live стакан";
  }
  if (source === "last_candle") {
    return "последняя свеча";
  }
  return "нет цены";
}

function degradedFlagLabel(flag: string): string {
  const labels: Record<string, string> = {
    api_snapshot_unavailable: "Данные API не получены; dashboard продолжает отдельные запросы.",
    balance_unavailable: "Счёт не получен от брокера.",
    session_unavailable: "Сессия не получена из runtime.",
    no_active_instruments: "Нет активного universe в registry/config.",
    strategy_state_unavailable: "Нет strategy state event; торговля всё равно отключена.",
  };
  return labels[flag] ?? codeWithLabel(flag);
}

function connectionLabel(state: string): string {
  const labels: Record<string, string> = {
    live: "онлайн",
    loading: "подключение",
    idle: "ожидание",
    degraded: "нет связи",
    snapshot_closed: "резервный опрос",
  };
  return labels[state] ?? state;
}

function operatorError(value: string | null): string {
  if (!value) {
    return "";
  }
  return value
    .replaceAll("robot_status_unavailable", "статус робота не получен")
    .replaceAll("session_snapshot_unavailable", "сессия не получена")
    .replaceAll("signals_unavailable", "сигналы не получены")
    .replaceAll("balance_summary_unavailable", "счёт не получен")
    .replaceAll("api_snapshot_unavailable", "данные API недоступны")
    .replaceAll("request_timeout", "timeout запроса");
}
</script>

<template>
  <section class="page-stack" data-testid="live-dashboard">
    <div class="page-heading">
      <h1>Live Dashboard</h1>
      <div class="heading-status">
        <span class="connection-chip" :class="`connection-chip--${robot.liveConnection}`">
          <span class="connection-chip__dot" />
          Панель: {{ connectionLabel(robot.liveConnection) }}
        </span>
        <span class="connection-chip" :class="`connection-chip--${market.liveConnection}`">
          <span class="connection-chip__dot" />
          Котировки: {{ connectionLabel(market.liveConnection) }}
        </span>
        <span class="connection-chip" :class="`connection-chip--${portfolio.liveConnection}`">
          <span class="connection-chip__dot" />
          Портфель: {{ connectionLabel(portfolio.liveConnection) }}
        </span>
      </div>
    </div>

    <div v-if="robot.error || market.error || portfolio.error" class="alert-row">
      <EmptyState
        v-if="robot.error"
        title="Dashboard получил не все данные"
        :detail="operatorError(robot.error)"
        tone="warn"
      />
      <EmptyState
        v-if="market.error"
        title="Котировки частично недоступны"
        :detail="operatorError(market.error)"
        tone="warn"
      />
      <EmptyState
        v-if="portfolio.error"
        title="Портфель частично недоступен"
        :detail="operatorError(portfolio.error)"
        tone="warn"
      />
    </div>

    <div class="metric-grid">
      <MetricTile
        label="Брокерский счёт"
        :value="balanceValue()"
        :detail="balanceDetail()"
        :code="balanceCode()"
        :tone="robot.status.balance.balance_degraded ? 'warn' : 'good'"
      />
      <MetricTile
        label="Сессия MOEX"
        :value="sessionValue()"
        :detail="sessionDetail()"
      />
      <MetricTile
        label="Фаза рынка"
        :value="phaseValue()"
      />
      <MetricTile
        label="Связь с брокером"
        :value="brokerStatusValue()"
      />
      <MetricTile
        label="Окно сбора"
        :value="microSessionValue()"
        :detail="countdownFromMicroSession(robot.status.micro_session_id)"
      />
      <MetricTile
        label="Торговля"
        :value="strategyValue()"
        :detail="strategyDetail()"
      />
    </div>

    <div class="dashboard-command-row dashboard-command-row--passive">
      <span v-if="robot.balanceRefreshLoading" class="inline-spinner" aria-hidden="true" />
      <span>
        Баланс обновляется автоматически read-only через T-Invest. Это не включает торговлю.
      </span>
    </div>

    <div class="dashboard-layout">
      <div class="dashboard-layout__main">
        <DataPanel>
          <template #eyebrow>quotes</template>
          <template #title>Котировки core universe</template>
          <div class="quote-grid" v-if="market.quoteRows.length">
            <button
              v-for="instrument in market.quoteRows"
              :key="instrument.instrument_id"
              class="quote-card"
              :class="{ 'quote-card--active': market.selectedInstrumentId === instrument.instrument_id }"
              type="button"
              @click="market.selectedInstrumentId = instrument.instrument_id"
            >
              <span class="quote-card__ticker">{{ instrument.instrument_id }}</span>
              <strong>{{ formatDecimal(instrument.last_price, 2) }}</strong>
              <span>{{ quoteSourceLabel(instrument.last_price_source) }}</span>
              <small>{{ compactDateTime(instrument.last_price_at) }}</small>
            </button>
          </div>
          <EmptyState
            v-else
            title="Котировки пока не загружены"
            detail="Показываем live стакан или последнюю 1m свечу по core universe."
            tone="warn"
          />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>market</template>
          <template #title>Выбранный инструмент</template>
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
              label="Спред"
              :value="formatDecimal(market.currentInstrument?.spread, 4)"
              tone="info"
            />
            <MetricTile
              label="Последняя цена"
              :value="formatDecimal(market.currentInstrument?.last_price, 2)"
              :detail="quoteSourceLabel(market.currentInstrument?.last_price_source ?? null)"
            />
            <MetricTile
              label="Качество рынка"
              :value="formatDecimal(market.currentInstrument?.market_quality, 3)"
              :tone="Number(market.currentInstrument?.market_quality ?? 0) >= 0.7 ? 'good' : 'warn'"
            />
            <MetricTile
              label="Лучший bid / ask"
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
                detail="Live стакан появится после data-only сбора; последняя цена уже берётся из свечей."
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
              detail="Активные позиции появятся после успешного readonly refresh портфеля."
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
          <template #title>Что требует внимания</template>
          <div v-if="robot.status.degraded_flags.length" class="operator-list">
            <div v-for="flag in robot.status.degraded_flags" :key="flag">
              <strong>{{ degradedFlagLabel(flag) }}</strong>
            </div>
          </div>
          <EmptyState v-else title="Критичных проблем нет" detail="Dashboard не видит degraded flags." />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>collector</template>
          <template #title>Data-only сбор</template>
          <dl class="definition-grid">
            <dt>режим</dt>
            <dd>{{ market.dataShadowStatus.enabled ? "включён" : "выключен" }}</dd>
            <dt>торговля</dt>
            <dd>отключена; заявки не выставляются</dd>
            <dt>поток</dt>
            <dd>{{ market.dataShadowStatus.stream_alive ? "идут live samples" : "live samples нет" }}</dd>
            <dt>последний sample</dt>
            <dd>{{ market.dataShadowStatus.last_message_age_seconds ?? "нет samples" }}</dd>
            <dt>samples</dt>
            <dd>{{ market.dataShadowStatus.market_microstructure_snapshots }}</dd>
            <dt>стаканы</dt>
            <dd>{{ market.dataShadowStatus.order_book_snapshots }}</dd>
            <dt>средний спред</dt>
            <dd>{{ formatDecimal(market.dataShadowStatus.avg_spread_bps, 4) }}</dd>
            <dt>p95 спред</dt>
            <dd>{{ formatDecimal(market.dataShadowStatus.p95_spread_bps, 4) }}</dd>
            <dt>качество</dt>
            <dd>{{ formatDecimal(market.dataShadowStatus.avg_market_quality_score, 3) }}</dd>
            <dt>сессия</dt>
            <dd>{{ market.dataShadowStatus.current_session ?? "нет live session" }}</dd>
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
          <template #title>Соединения</template>
          <dl class="definition-grid">
            <dt>панель</dt>
            <dd>{{ robot.liveConnection === "live" ? "онлайн" : "опрос API" }}</dd>
            <dt>котировки</dt>
            <dd>{{ market.liveConnection === "live" ? "онлайн" : "polling последней цены" }}</dd>
            <dt>портфель</dt>
            <dd>{{ portfolio.liveConnection === "live" ? "онлайн" : "readonly polling" }}</dd>
            <dt>отчёты</dt>
            <dd>{{ reports.liveConnection === "live" ? "онлайн" : "опрос API" }}</dd>
            <dt>статус</dt>
            <dd>
              {{
                [robot.liveConnection, market.liveConnection, portfolio.liveConnection, reports.liveConnection].includes(
                  "degraded",
                )
                  ? "есть деградация соединения"
                  : "критичных проблем связи нет"
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
            <dt>панель</dt>
            <dd>{{ compactDateTime(robot.lastDashboardMessageAt) }}</dd>
            <dt>котировки обновлены</dt>
            <dd>{{ compactDateTime(market.overview.generated_at) }}</dd>
            <dt>сессия обновлена</dt>
            <dd>{{ compactDateTime(robot.session.observed_at) }}</dd>
          </dl>
        </DataPanel>
      </div>
    </div>
  </section>
</template>

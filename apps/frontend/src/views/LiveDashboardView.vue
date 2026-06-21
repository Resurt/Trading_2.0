<script setup lang="ts">
import { computed } from "vue";

import type { JsonPayload, MarketInstrumentOverview } from "../api/types";
import OrderBookWidget from "../components/dashboard/OrderBookWidget.vue";
import RiskEventsList from "../components/dashboard/RiskEventsList.vue";
import SignalReasonCard from "../components/dashboard/SignalReasonCard.vue";
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import { useMarketStore } from "../stores/market";
import { usePortfolioStore } from "../stores/portfolio";
import { useRobotStore } from "../stores/robot";
import {
  compactDateTime,
  formatDecimal,
  formatMoney,
  formatPercentRatio,
} from "../utils/format";

const robot = useRobotStore();
const market = useMarketStore();
const portfolio = usePortfolioStore();

const SESSION_LABELS: Record<string, string> = {
  weekday_morning: "Утренняя сессия",
  weekday_main: "Основная сессия",
  weekday_evening: "Вечерняя сессия",
  weekend: "Выходная сессия",
  closed: "Рынок закрыт",
  unknown: "Сессия уточняется",
};

const PHASE_LABELS: Record<string, string> = {
  continuous_trading: "Идут торги",
  auction: "Аукцион",
  break: "Перерыв",
  closed: "Торги закрыты",
  unknown: "Фаза уточняется",
};

const QUOTE_SOURCE_LABELS: Record<string, string> = {
  live_order_book_mid: "mid стакана",
  tbank_last_price: "последняя цена T-Invest",
  latest_market_candle_close: "последняя свеча",
  previous_close: "предыдущее закрытие",
  unavailable: "цена недоступна",
};

const QUOTE_STATUS_LABELS: Record<string, string> = {
  live: "live",
  stale: "stale",
  previous_close: "prev close",
  unavailable: "нет цены",
};

const COLLECTOR_LABELS: Record<string, string> = {
  stopped: "Остановлен",
  preflight_blocked: "Заблокирован preflight",
  starting: "Запускается",
  collecting: "Идёт сбор",
  stopping: "Останавливается",
  stopped_by_operator: "Остановлен оператором",
  emergency_stopped: "Аварийно остановлен",
  degraded: "Degraded",
};

const quoteRows = computed(() =>
  [...market.quoteRows].sort((left, right) => {
    const leftPriority = quotePriority(left);
    const rightPriority = quotePriority(right);
    if (leftPriority !== rightPriority) {
      return leftPriority - rightPriority;
    }
    return left.instrument_id.localeCompare(right.instrument_id);
  }),
);

const selectedInstrument = computed(() => market.currentInstrument);

function quotePriority(instrument: MarketInstrumentOverview): number {
  if (instrument.quote_status === "live") {
    return 0;
  }
  if (instrument.quote_status === "stale") {
    return 1;
  }
  if (instrument.quote_status === "previous_close") {
    return 2;
  }
  return 3;
}

function toneFromQuote(instrument: MarketInstrumentOverview): "good" | "warn" | "muted" {
  if (instrument.quote_status === "live") {
    return "good";
  }
  if (instrument.quote_status === "stale" || instrument.quote_status === "previous_close") {
    return "warn";
  }
  return "muted";
}

function sourceLabel(source: string | null): string {
  return QUOTE_SOURCE_LABELS[source ?? "unavailable"] ?? source ?? "цена недоступна";
}

function statusLabel(status: string): string {
  return QUOTE_STATUS_LABELS[status] ?? status;
}

function sessionLabel(value: string | null | undefined): string {
  return SESSION_LABELS[value ?? "unknown"] ?? value ?? "Сессия уточняется";
}

function phaseLabel(value: string | null | undefined): string {
  return PHASE_LABELS[value ?? "unknown"] ?? value ?? "Фаза уточняется";
}

function collectorLabel(value: string | null | undefined): string {
  return COLLECTOR_LABELS[value ?? "stopped"] ?? value ?? "Остановлен";
}

function commandStatusLabel(status: string | null): string {
  const labels: Record<string, string> = {
    checking_preflight: "Проверка preflight",
    start_requesting: "Запрос старта",
    requested: "Запрошено",
    accepted: "Принято",
    applied: "Выполнено",
    rejected: "Отклонено",
    blocked_by_preflight: "Старт заблокирован",
    preflight_failed: "Preflight не завершился",
    stop_requesting: "Запрос остановки",
    stop_failed: "Ошибка остановки",
    balance_refresh_completed: "Баланс обновлён",
    balance_refresh_degraded: "Баланс недоступен",
    balance_refresh_failed: "Ошибка баланса",
  };
  return labels[status ?? ""] ?? status ?? "Команд не было";
}

function reasonLabel(reason: string | null | undefined): string {
  const labels: Record<string, string> = {
    market_open: "рынок открыт",
    market_closed_expected: "рынок закрыт по расписанию",
    weekend_session_closed: "выходная сессия закрыта",
    no_trading_window: "нет торгового окна",
    broker_schedule_unavailable: "расписание брокера недоступно",
    broker_status_unavailable: "статус инструмента недоступен",
    session_preflight_unavailable: "preflight недоступен",
    collector_waiting_for_operator_start: "ожидает Start",
    collector_no_recent_samples: "нет свежих live samples",
    broker_balance_unavailable: "нет сохранённого broker balance",
    broker_accounts_empty: "T-Bank вернул пустой список счетов",
    broker_balance_timeout: "T-Bank не ответил вовремя",
    api_snapshot_unavailable: "snapshot API не получен",
  };
  return labels[reason ?? ""] ?? reason ?? "нет причины";
}

function operatorError(value: string | null): string {
  if (!value) {
    return "";
  }
  const cleaned = value.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  if (cleaned.includes("504 Gateway") || cleaned.includes("Gateway Time-out")) {
    return "API не ответил вовремя; экран держит последнее полученное состояние.";
  }
  if (cleaned.toLowerCase().includes("timeout")) {
    return "API отвечает медленно; экран не стирает предыдущие данные и повторяет запрос.";
  }
  return cleaned
    .replaceAll("dashboard_state_unavailable", "dashboard snapshot не получен")
    .replaceAll("robot_status_unavailable", "статус робота не получен")
    .replaceAll("session_snapshot_unavailable", "сессия не получена")
    .replaceAll("signals_unavailable", "сигналы не получены")
    .replaceAll("balance_summary_unavailable", "счёт не получен")
    .replaceAll("api_snapshot_unavailable", "snapshot API не получен")
    .replaceAll("request_timeout", "timeout запроса");
}

function serviceTone(errorValue: string | null, loadingValue: boolean, hasData = true): "good" | "warn" | "loading" {
  if (loadingValue) {
    return "loading";
  }
  if (errorValue || !hasData) {
    return "warn";
  }
  return "good";
}

function dashboardStatusText(): string {
  if (robot.loading) {
    return "Подключаю dashboard snapshot. Уже полученные данные остаются на экране.";
  }
  if (robot.error) {
    return operatorError(robot.error);
  }
  return "Статус робота, календарь, команды и счёт читаются без включения торговли.";
}

function marketStatusText(): string {
  if (market.loading) {
    return "Обновляю локальный market read-model. Медленный брокерский refresh не блокирует экран.";
  }
  if (market.error) {
    return operatorError(market.error);
  }
  if (!market.quoteRows.length) {
    return "Жду строки core universe. Если цены нет, строка всё равно должна показать причину.";
  }
  return "Котировки загружены. Stale и source показываются явно, старые цены не помечаются live.";
}

function portfolioStatusText(): string {
  if (robot.balanceRefreshLoading) {
    return "Обновляю broker balance read-only через T-Invest.";
  }
  if (portfolio.error) {
    return operatorError(portfolio.error);
  }
  if (robot.status.balance.balance_degraded) {
    return balanceUnavailableReason();
  }
  return "Broker balance получен. Это только отображение счёта, не торговое разрешение.";
}

function balanceUnavailableReason(): string {
  const reason = robot.status.balance.balance_degraded_reason_code;
  const labels: Record<string, string> = {
    api_snapshot_unavailable: "Snapshot API не получен; отдельно запрашиваю broker balance.",
    broker_balance_unavailable: "Нет сохранённых данных счёта. Запрашиваю T-Invest read-only.",
    broker_accounts_empty: "T-Bank вернул пустой список счетов. Проверьте token/account contour.",
    broker_balance_timeout: "T-Invest не ответил за отведённое время.",
    broker_gateway_unavailable: "Broker gateway недоступен в API container.",
    broker_balance_refresh_failed: "Readonly refresh счёта завершился ошибкой.",
    position_snapshot_missing: "В базе пока нет snapshot портфеля.",
  };
  return labels[reason ?? ""] ?? "Нет актуальных данных счёта от брокера.";
}

function balanceValue(): string {
  if (robot.status.balance.balance_degraded) {
    return robot.balanceRefreshLoading ? "Обновляю счёт..." : "Счёт не получен";
  }
  return formatMoney(
    robot.status.balance.total_portfolio_value_rub ?? robot.status.balance.available,
    "RUB",
  );
}

function availableCash(): string {
  return formatMoney(robot.status.balance.available_cash_rub ?? robot.status.balance.available, "RUB");
}

function blockedCash(): string {
  return formatMoney(robot.status.balance.blocked_cash_rub ?? robot.status.balance.blocked, "RUB");
}

function expectedYield(): string {
  return formatMoney(robot.status.balance.expected_yield_rub, "RUB");
}

function sessionDetail(): string {
  const date = robot.session.trading_date ?? robot.session.calendar_date ?? "нет даты";
  const marketOpen = market.dataShadowStatus.market_open;
  const openLabel = marketOpen === null ? "market_open уточняется" : marketOpen ? "market_open=true" : "market_open=false";
  const reason = market.dataShadowStatus.reason_code ?? robot.lastCommandReasonCode;
  return `${date} / ${openLabel}${reason ? ` / ${reasonLabel(reason)}` : ""}`;
}

function phaseDetail(): string {
  const expected = market.dataShadowStatus.market_closed_expected;
  if (expected === true) {
    return `Закрытие ожидаемо. Следующая сессия: ${compactDateTime(market.dataShadowStatus.next_session_at)}`;
  }
  if (expected === false) {
    return "Рынок должен быть доступен; collector можно запускать после preflight.";
  }
  return "Жду preflight или runtime status.";
}

function brokerStatusValue(): string {
  if (robot.status.broker_trading_status && robot.status.broker_trading_status !== "unknown") {
    return robot.status.broker_trading_status;
  }
  if (!robot.status.balance.balance_degraded || market.quoteRows.some((item) => item.last_price)) {
    return "Read-only доступен";
  }
  return "Статус уточняется";
}

function brokerStatusDetail(): string {
  if (market.quoteRows.some((item) => item.order_book_source === "tbank_order_book")) {
    return "Стакан получен через readonly T-Invest GetOrderBook.";
  }
  if (market.quoteRows.some((item) => item.last_price_source === "tbank_last_price")) {
    return "Последние цены получены через readonly T-Invest GetLastPrices.";
  }
  if (!robot.status.balance.balance_degraded) {
    return "Счёт получен через readonly T-Invest.";
  }
  return "Жду read-only ответ T-Invest по счёту, ценам или стаканам.";
}

function microSessionValue(): string {
  if (market.dataShadowStatus.collector_state) {
    return collectorLabel(market.dataShadowStatus.collector_state);
  }
  if (robot.status.micro_session_id) {
    return "Окно сбора активно";
  }
  return "Нет активного окна";
}

function microSessionDetail(): string {
  if (market.dataShadowStatus.stream_alive) {
    return `samples ${market.dataShadowStatus.market_microstructure_snapshots}, стаканы ${market.dataShadowStatus.order_book_snapshots}`;
  }
  if (market.dataShadowStatus.collector_state === "preflight_blocked") {
    return `Сбор не стартовал: ${reasonLabel(market.dataShadowStatus.reason_code)}`;
  }
  if (robot.status.micro_session_id) {
    return robot.status.micro_session_id;
  }
  return "Live stream не запущен. Цены показываются из read-only refresh или локальной истории.";
}

function tradingModeValue(): string {
  if (market.dataShadowStatus.collector_state === "collecting") {
    return "Data-only сбор";
  }
  return "Торговля отключена";
}

function tradingModeDetail(): string {
  return "Data-only режим: real orders, pseudo-orders, signal_candidate и order_intent не создаются.";
}

function formatBps(value: string | null | undefined): string {
  return value === null || value === undefined ? "Нет данных" : `${formatDecimal(value, 1)} bps`;
}

function formatLots(value: string | null | undefined): string {
  return value === null || value === undefined ? "Нет данных" : `${formatDecimal(value, 0)} lots`;
}

function formatChangeBps(value: string | null | undefined): string {
  if (value === null || value === undefined) {
    return "Нет данных";
  }
  const numeric = Number(value);
  const prefix = numeric > 0 ? "+" : "";
  return `${prefix}${formatDecimal(value, 1)} bps`;
}

function changeTone(value: string | null | undefined): string {
  const numeric = Number(value ?? 0);
  if (numeric > 0) {
    return "positive";
  }
  if (numeric < 0) {
    return "negative";
  }
  return "flat";
}

function quoteFreshness(instrument: MarketInstrumentOverview): string {
  if (!instrument.last_price_at) {
    return reasonLabel(String(instrument.quote_payload?.reason_code ?? "no_price_source_available"));
  }
  const age = instrument.price_staleness_seconds;
  if (age === null || age === undefined) {
    return compactDateTime(instrument.last_price_at);
  }
  return `${compactDateTime(instrument.last_price_at)} / ${age}s`;
}

function selectedInstrumentTitle(): string {
  const instrument = selectedInstrument.value;
  if (!instrument) {
    return "Инструмент не выбран";
  }
  return `${instrument.ticker ?? instrument.instrument_id} / ${statusLabel(instrument.quote_status)}`;
}

function orderBookReason(): string {
  const instrument = selectedInstrument.value;
  if (!instrument) {
    return "instrument_unavailable";
  }
  if (instrument.order_book_source && !instrument.order_book_stale) {
    return "Стакан свежий.";
  }
  if (instrument.order_book_stale && instrument.order_book_ts) {
    return `Стакан устарел: ${compactDateTime(instrument.order_book_ts)}.`;
  }
  if (market.dataShadowStatus.collector_state === "stopped") {
    return "Стакан не собран: data-only сбор выключен.";
  }
  if (market.dataShadowStatus.collector_state === "preflight_blocked") {
    return `Стакан не собран: ${reasonLabel(market.dataShadowStatus.reason_code)}.`;
  }
  if (market.dataShadowStatus.market_closed_expected) {
    return "Стакан не собран: рынок закрыт по расписанию.";
  }
  if (market.dataShadowStatus.warning) {
    return `Стакан не собран: ${market.dataShadowStatus.warning}.`;
  }
  return "Стакан не собран: нет свежих order book samples.";
}

function tradeTime(trade: JsonPayload): string {
  const raw = trade.ts ?? trade.ts_utc ?? trade.exchange_ts ?? trade.time;
  return typeof raw === "string" ? compactDateTime(raw) : "нет времени";
}

function tradePrice(trade: JsonPayload): string {
  const raw = trade.price ?? trade.price_rub ?? trade.last_price;
  return formatDecimal(typeof raw === "string" || typeof raw === "number" ? raw : null, 2);
}

function tradeQty(trade: JsonPayload): string {
  const raw = trade.qty_lots ?? trade.quantity_lots ?? trade.quantity;
  return formatLots(typeof raw === "string" || typeof raw === "number" ? String(raw) : null);
}

function tradeSideLabel(trade: JsonPayload): string {
  const raw = String(trade.side ?? trade.aggressor_side ?? trade.direction ?? "").toLowerCase();
  if (raw.includes("buy") || raw.includes("bid") || raw.includes("покуп")) {
    return "Покупка";
  }
  if (raw.includes("sell") || raw.includes("ask") || raw.includes("прод")) {
    return "Продажа";
  }
  return "нет стороны";
}

function tradeToneClass(trade: JsonPayload): string {
  const side = tradeSideLabel(trade);
  if (side === "Покупка") {
    return "market-tape-price market-tape-price--buy";
  }
  if (side === "Продажа") {
    return "market-tape-price market-tape-price--sell";
  }
  return "market-tape-price";
}

function degradedFlagLabel(flag: string): string {
  const labels: Record<string, string> = {
    api_snapshot_unavailable: "Snapshot API не получен; экран держит последнее состояние.",
    dashboard_unavailable: "Dashboard snapshot не получен; экран держит последнее состояние.",
    balance_unavailable: "Счёт не получен от брокера.",
    session_unavailable: "Сессия не получена из runtime.",
    no_active_instruments: "Нет active universe в registry/config.",
    strategy_state_unavailable: "Нет strategy state event; торговля всё равно отключена.",
  };
  return labels[flag] ?? flag;
}
</script>

<template>
  <section class="page-stack trader-dashboard" data-testid="live-dashboard">
    <div class="page-heading">
      <div>
        <p class="eyebrow">operator panel</p>
        <h1>Live Dashboard</h1>
      </div>
      <div class="heading-status">
        <span class="connection-chip" :class="`connection-chip--${robot.liveConnection}`">
          <span class="connection-chip__dot" />
          Панель: {{ robot.liveConnection === "live" ? "онлайн" : robot.liveConnection === "loading" ? "подключение" : robot.liveConnection === "degraded" ? "нет связи" : "ожидание" }}
        </span>
        <span class="connection-chip" :class="`connection-chip--${market.liveConnection}`">
          <span class="connection-chip__dot" />
          Котировки: {{ market.liveConnection === "live" ? "онлайн" : market.liveConnection === "loading" ? "подключение" : market.liveConnection === "degraded" ? "нет связи" : "ожидание" }}
        </span>
        <span class="connection-chip" :class="`connection-chip--${portfolio.liveConnection}`">
          <span class="connection-chip__dot" />
          Портфель: {{ portfolio.liveConnection === "live" ? "онлайн" : portfolio.liveConnection === "loading" ? "подключение" : portfolio.liveConnection === "degraded" ? "нет связи" : "ожидание" }}
        </span>
      </div>
    </div>

    <div class="operator-status-grid" aria-label="dashboard data status">
      <section class="operator-status-card" :class="`operator-status-card--${serviceTone(robot.error, robot.loading)}`">
        <div class="operator-status-card__head">
          <span class="operator-status-card__dot" />
          <strong>Панель</strong>
        </div>
        <p>{{ dashboardStatusText() }}</p>
      </section>
      <section
        class="operator-status-card"
        :class="`operator-status-card--${serviceTone(market.error, market.loading, market.quoteRows.length > 0)}`"
      >
        <div class="operator-status-card__head">
          <span class="operator-status-card__dot" />
          <strong>Котировки</strong>
        </div>
        <p>{{ marketStatusText() }}</p>
      </section>
      <section
        class="operator-status-card"
        :class="`operator-status-card--${serviceTone(portfolio.error, robot.balanceRefreshLoading, !robot.status.balance.balance_degraded)}`"
      >
        <div class="operator-status-card__head">
          <span class="operator-status-card__dot" />
          <strong>Портфель</strong>
        </div>
        <p>{{ portfolioStatusText() }}</p>
      </section>
    </div>

    <div class="command-status-panel" :class="{ 'command-status-panel--active': robot.commandLoading }">
      <span v-if="robot.commandLoading" class="inline-spinner" aria-hidden="true" />
      <div>
        <p class="eyebrow">last command</p>
        <strong>{{ commandStatusLabel(robot.lastCommandStatus) }}</strong>
      </div>
      <p>{{ robot.lastCommandMessage ?? "Команд ещё не было. Start запускает только data-only сбор, торговля отключена." }}</p>
      <code v-if="robot.lastCommandReasonCode">{{ reasonLabel(robot.lastCommandReasonCode) }}</code>
      <small v-if="robot.lastCommandNextSessionAt">next {{ compactDateTime(robot.lastCommandNextSessionAt) }}</small>
      <small v-if="robot.lastCommandAt">{{ compactDateTime(robot.lastCommandAt) }}</small>
    </div>

    <div class="metric-grid">
      <section class="balance-card" :class="{ 'balance-card--degraded': robot.status.balance.balance_degraded }">
        <div class="balance-card__top">
          <span>Брокерский счёт</span>
          <button type="button" class="inline-action" :disabled="robot.balanceRefreshLoading" @click="robot.refreshBalance()">
            {{ robot.balanceRefreshLoading ? "Обновляю..." : "Обновить" }}
          </button>
        </div>
        <strong>{{ balanceValue() }}</strong>
        <p>{{ robot.status.balance.balance_degraded ? balanceUnavailableReason() : "Readonly баланс T-Invest. Это не разрешение на торговлю." }}</p>
        <dl>
          <div>
            <dt>свободно</dt>
            <dd>{{ availableCash() }}</dd>
          </div>
          <div>
            <dt>блок</dt>
            <dd>{{ blockedCash() }}</dd>
          </div>
          <div>
            <dt>доход</dt>
            <dd>{{ expectedYield() }}</dd>
          </div>
          <div>
            <dt>счёт</dt>
            <dd>{{ robot.status.balance.account_id_masked ?? "masked" }}</dd>
          </div>
          <div>
            <dt>freshness</dt>
            <dd>{{ compactDateTime(robot.status.balance.last_balance_refresh_at) }}</dd>
          </div>
        </dl>
      </section>

      <MetricTile label="Сессия MOEX" :value="sessionLabel(robot.status.session_type)" :detail="sessionDetail()" />
      <MetricTile label="Фаза рынка" :value="phaseLabel(robot.status.session_phase)" :detail="phaseDetail()" />
      <MetricTile label="Связь с брокером" :value="brokerStatusValue()" :detail="brokerStatusDetail()" />
      <MetricTile label="Окно сбора" :value="microSessionValue()" :detail="microSessionDetail()" />
      <MetricTile label="Торговля" :value="tradingModeValue()" :detail="tradingModeDetail()" tone="info" />
    </div>

    <div class="dashboard-layout">
      <div class="dashboard-layout__main">
        <DataPanel>
          <template #eyebrow>quotes</template>
          <template #title>Котировки core universe</template>
          <template #action>
            <span class="status-badge status-badge--info">{{ quoteRows.length }} инструментов</span>
          </template>

          <div v-if="quoteRows.length" class="quote-table-wrap">
            <table class="quote-table">
              <thead>
                <tr>
                  <th>ticker</th>
                  <th>цена</th>
                  <th>status/source</th>
                  <th>freshness</th>
                  <th>сессия</th>
                  <th>spread</th>
                  <th>bid / ask</th>
                  <th>качество</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="instrument in quoteRows"
                  :key="instrument.instrument_id"
                  :class="{ 'quote-table__row--active': market.selectedInstrumentId === instrument.instrument_id }"
                  @click="market.selectedInstrumentId = instrument.instrument_id"
                >
                  <td>
                    <button class="quote-row-button" type="button">
                      <strong>{{ instrument.ticker ?? instrument.instrument_id }}</strong>
                      <small>{{ instrument.instrument_id }}</small>
                    </button>
                  </td>
                  <td>
                    <strong class="price-cell">{{ formatDecimal(instrument.last_price, 2) }}</strong>
                    <small :class="`quote-change quote-change--${changeTone(instrument.change_bps)}`">
                      {{ formatChangeBps(instrument.change_bps) }}
                    </small>
                  </td>
                  <td>
                    <span class="status-badge" :class="`status-badge--${toneFromQuote(instrument)}`">
                      {{ statusLabel(instrument.quote_status) }}
                    </span>
                    <small>{{ sourceLabel(instrument.last_price_source) }}</small>
                  </td>
                  <td>{{ quoteFreshness(instrument) }}</td>
                  <td>{{ sessionLabel(instrument.session_type) }}</td>
                  <td>{{ formatBps(instrument.spread_bps) }}</td>
                  <td>{{ formatDecimal(instrument.best_bid, 2) }} / {{ formatDecimal(instrument.best_ask, 2) }}</td>
                  <td>{{ formatPercentRatio(instrument.market_quality) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <EmptyState
            v-else
            title="Котировки core universe не получены"
            detail="GET /market/overview должен вернуть 8 строк даже без live стакана. Проверьте API route smoke и read-model."
            tone="warn"
          />
        </DataPanel>

        <DataPanel>
          <template #eyebrow>selected instrument</template>
          <template #title>{{ selectedInstrumentTitle() }}</template>
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

          <div class="selected-instrument-grid">
            <MetricTile
              label="Последняя цена"
              :value="formatDecimal(selectedInstrument?.last_price, 2)"
              :detail="selectedInstrument ? `${sourceLabel(selectedInstrument.last_price_source)} / ${quoteFreshness(selectedInstrument)}` : 'Нет инструмента'"
              :tone="selectedInstrument?.quote_status === 'live' ? 'good' : 'warn'"
            />
            <MetricTile label="Bid / Ask" :value="`${formatDecimal(selectedInstrument?.best_bid, 2)} / ${formatDecimal(selectedInstrument?.best_ask, 2)}`" />
            <MetricTile label="Mid" :value="formatDecimal(selectedInstrument?.mid_price, 2)" :detail="formatBps(selectedInstrument?.spread_bps)" />
            <MetricTile label="Спред" :value="formatDecimal(selectedInstrument?.spread_abs, 4)" :detail="formatBps(selectedInstrument?.spread_bps)" tone="info" />
            <MetricTile label="Глубина" :value="`${formatLots(selectedInstrument?.bid_depth_lots)} / ${formatLots(selectedInstrument?.ask_depth_lots)}`" />
            <MetricTile label="Imbalance" :value="formatDecimal(selectedInstrument?.book_imbalance, 3)" />
            <MetricTile label="Качество стакана" :value="formatPercentRatio(selectedInstrument?.market_quality)" :tone="Number(selectedInstrument?.market_quality ?? 0) >= 0.7 ? 'good' : 'warn'" />
            <MetricTile label="Статус стакана" :value="selectedInstrument?.order_book_stale ? 'stale' : selectedInstrument?.order_book_source ? 'fresh' : 'нет стакана'" :detail="orderBookReason()" />
          </div>

          <div class="market-depth-layout">
            <OrderBookWidget :instrument="selectedInstrument" />

            <section class="market-tape-card">
              <header class="market-tape-header">
                <div>
                  <h3>ЛЕНТА СДЕЛОК</h3>
                  <span>{{ selectedInstrument?.ticker ?? selectedInstrument?.instrument_id ?? "инструмент не выбран" }}</span>
                </div>
                <strong>рыночный поток</strong>
              </header>
              <div v-if="market.recentTrades.length" class="market-tape-table">
                <div class="market-tape-row market-tape-row--head">
                  <span>время</span>
                  <span>цена</span>
                  <span>объем</span>
                  <span>сторона</span>
                </div>
                <div v-for="(trade, index) in market.recentTrades.slice(0, 18)" :key="index" class="market-tape-row">
                  <span>{{ tradeTime(trade) }}</span>
                  <strong :class="tradeToneClass(trade)">{{ tradePrice(trade) }}</strong>
                  <span>{{ tradeQty(trade) }}</span>
                  <span>{{ tradeSideLabel(trade) }}</span>
                </div>
              </div>
              <EmptyState
                v-else
                title="Лента сделок недоступна"
                detail="Причина: no_market_trades_samples. Появится после market trades stream; отсутствие ленты не скрывается."
                tone="warn"
              />
            </section>
          </div>
        </DataPanel>
      </div>

      <div class="dashboard-layout__side">
        <DataPanel>
          <template #eyebrow>collector</template>
          <template #title>Data-only сбор</template>
          <dl class="definition-grid">
            <dt>state</dt>
            <dd>{{ collectorLabel(market.dataShadowStatus.collector_state) }}</dd>
            <dt>market_open</dt>
            <dd>{{ market.dataShadowStatus.market_open ?? "уточняется" }}</dd>
            <dt>reason</dt>
            <dd>{{ reasonLabel(market.dataShadowStatus.reason_code) }}</dd>
            <dt>next session</dt>
            <dd>{{ compactDateTime(market.dataShadowStatus.next_session_at) }}</dd>
            <dt>stream</dt>
            <dd>{{ market.dataShadowStatus.stream_alive ? "live samples идут" : "live samples нет" }}</dd>
            <dt>snapshots</dt>
            <dd>{{ market.dataShadowStatus.market_microstructure_snapshots }}</dd>
            <dt>стаканы</dt>
            <dd>{{ market.dataShadowStatus.order_book_snapshots }}</dd>
            <dt>last sample age</dt>
            <dd>{{ market.dataShadowStatus.last_message_age_seconds ?? "нет samples" }}</dd>
            <dt>last command</dt>
            <dd>{{ market.dataShadowStatus.last_command_status ?? "нет command" }}</dd>
          </dl>
          <div v-if="market.dataShadowStatus.warnings.length" class="operator-list operator-list--compact">
            <div v-for="warning in market.dataShadowStatus.warnings" :key="warning">
              <strong>{{ reasonLabel(warning) }}</strong>
            </div>
          </div>
        </DataPanel>

        <DataPanel>
          <template #eyebrow>session</template>
          <template #title>Текущая сессия</template>
          <dl class="definition-grid">
            <dt>date</dt>
            <dd>{{ robot.session.trading_date ?? robot.session.calendar_date ?? "нет даты" }}</dd>
            <dt>session</dt>
            <dd>{{ sessionLabel(robot.status.session_type) }}</dd>
            <dt>phase</dt>
            <dd>{{ phaseLabel(robot.status.session_phase) }}</dd>
            <dt>closed expected</dt>
            <dd>{{ market.dataShadowStatus.market_closed_expected ?? "уточняется" }}</dd>
            <dt>broker status</dt>
            <dd>{{ brokerStatusValue() }}</dd>
          </dl>
        </DataPanel>

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
          <template #eyebrow>freshness</template>
          <template #title>Обновление</template>
          <dl class="definition-grid">
            <dt>панель</dt>
            <dd>{{ compactDateTime(robot.lastDashboardMessageAt) }}</dd>
            <dt>котировки</dt>
            <dd>{{ compactDateTime(market.overview.generated_at) }}</dd>
            <dt>сессия</dt>
            <dd>{{ compactDateTime(robot.session.observed_at) }}</dd>
            <dt>баланс</dt>
            <dd>{{ compactDateTime(robot.status.balance.last_balance_refresh_at) }}</dd>
          </dl>
        </DataPanel>
      </div>
    </div>
  </section>
</template>

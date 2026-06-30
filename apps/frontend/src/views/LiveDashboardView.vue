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
  live_exchange_order_book: "MOEX стакан",
  live_exchange_last_price: "MOEX последняя цена",
  broker_quote_exchange_closed: "брокерская котировка",
  broker_otc_order_book: "внебиржевой стакан",
  broker_indicative_quote: "индикативная котировка",
  tbank_last_price: "последняя цена T-Invest",
  latest_market_candle_close: "последняя свеча",
  previous_close: "предыдущее закрытие",
  unavailable: "цена недоступна",
};

const QUOTE_STATUS_LABELS: Record<string, string> = {
  live: "свежая",
  broker_quote: "брокерская",
  indicative: "индикативная",
  stale: "устарела",
  previous_close: "пред. закрытие",
  unavailable: "нет цены",
};

const COLLECTOR_LABELS: Record<string, string> = {
  stopped: "Остановлен",
  preflight_blocked: "Заблокирован preflight",
  starting: "Запускается",
  collecting: "Идёт сбор",
  stopping: "Останавливается",
  stopped_by_operator: "Сбор остановлен",
  emergency_stopped: "Аварийно остановлен",
  degraded: "Degraded",
};

const quoteRows = computed(() => market.quoteRows);

const selectedInstrument = computed(() => market.currentInstrument);

function toneFromQuote(instrument: MarketInstrumentOverview): "good" | "warn" | "muted" {
  if (instrument.quote_status === "live") {
    return "good";
  }
  if (
    instrument.quote_status === "broker_quote" ||
    instrument.quote_status === "indicative" ||
    instrument.quote_status === "stale" ||
    instrument.quote_status === "previous_close"
  ) {
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
    preflight_unavailable: "preflight недоступен",
    collector_waiting_for_operator_start: "ожидает Start",
    collector_no_recent_samples: "нет свежих live samples",
    data_only_collection_stopped: "data-only сбор остановлен",
    data_only_collection_allowed: "data-only сбор разрешён",
    data_only_collection_blocked: "data-only сбор заблокирован",
    broker_balance_unavailable: "нет сохранённого broker balance",
    broker_accounts_empty: "T-Bank вернул пустой список счетов",
    broker_balance_timeout: "T-Bank не ответил вовремя",
    api_snapshot_unavailable: "snapshot API не получен",
    moex_dsvd_cancelled_platform_update: "биржа закрыта",
    broker_otc_only: "доступны только брокерские внебиржевые котировки",
    fresh: "свежая",
    stale: "устарела",
    trade_exchange_ts_too_old: "последние сделки устарели",
    missing_trade_exchange_ts: "нет времени сделок",
    no_market_trades_samples: "лента сделок не пришла",
    instrument_unavailable: "инструмент недоступен",
    not_for_calibration: "не для калибровки",
    broker_quote_not_for_calibration: "брокерская котировка только для отображения",
    stale_order_book: "стакан устарел",
    no_order_book_samples: "нет samples стакана",
    no_market_trades_feed_implemented: "market trades feed ещё не реализован",
    dashboard_market_feed_unavailable: "dashboard live feed временно недоступен",
    dashboard_last_prices_unavailable: "last prices временно недоступны",
    dashboard_gateway_unavailable: "readonly broker gateway недоступен",
    selected_order_book_unavailable: "стакан выбранного инструмента недоступен",
    selected_order_book_stale: "стакан выбранного инструмента устарел",
    empty_market_ws_snapshot: "пустой market WS snapshot проигнорирован",
    selected_instrument_details_unavailable: "details выбранного инструмента недоступны",
    data_shadow_status_unavailable: "статус data-only временно недоступен",
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
    return "Обновляю dashboard live feed. Уже полученные котировки и стакан остаются на экране.";
  }
  if (market.error) {
    return operatorError(market.error);
  }
  if (!market.quoteRows.length) {
    return "Жду строки core universe. Если цены нет, строка всё равно должна показать причину.";
  }
  if (market.dashboardFeedStatus.running && !market.feedErrors.length) {
    return "Рынок отображается через readonly dashboard feed. Start нужен только для записи data-only логов.";
  }
  return "Котировки загружены. Stale и source показываются явно, старые цены не помечаются live.";
}

function portfolioStatusText(): string {
  if (robot.balanceRefreshLoading) {
    return "Обновляю счёт через T-Invest.";
  }
  if (portfolio.error) {
    return operatorError(portfolio.error);
  }
  if (robot.status.balance.balance_degraded) {
    return balanceUnavailableReason();
  }
  return "Счёт получен. Баланс обновляется автоматически.";
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

function sessionDetail(): string {
  const marketOpen = market.dashboardFeedStatus.market_open;
  const reason =
    selectedInstrument.value?.reason_code ??
    market.dashboardFeedStatus.warnings[0] ??
    market.dataShadowStatus.reason_code ??
    robot.lastCommandReasonCode;
  const parts: string[] = [];
  if (marketOpen === true) {
    parts.push("рынок открыт");
  } else if (marketOpen === false) {
    parts.push("рынок закрыт или уточняется");
  }
  if (reason && !isDuplicateSessionReason(reason, marketOpen)) {
    const label = reasonLabel(reason);
    if (!parts.includes(label)) {
      parts.push(label);
    }
  }
  if (parts.length > 0) {
    return parts.join(" · ");
  }
  return robot.session.trading_date ?? robot.session.calendar_date ?? "нет даты";
}

function isDuplicateSessionReason(reason: string, marketOpen: boolean | null | undefined): boolean {
  if (marketOpen === true) {
    return reason === "market_open" || reason === "data_only_collection_allowed";
  }
  if (marketOpen === false) {
    return (
      reason === "market_closed_expected" ||
      reason === "official_exchange_closed" ||
      reason === "weekend_session_closed"
    );
  }
  return false;
}

function venueStatusValue(): string {
  const preflight = robot.lastSessionPreflight;
  if (market.dashboardFeedStatus.venue_type && market.dashboardFeedStatus.venue_type !== "unknown") {
    return venueLabel(market.dashboardFeedStatus.venue_type);
  }
  if (
    preflight?.venue_type === "official_exchange" ||
    preflight?.official_exchange_open ||
    market.quoteRows.some((instrument) => instrument.venue_type === "official_exchange")
  ) {
    return "Биржевая торговля";
  }
  if (
    preflight?.venue_type === "broker_otc" ||
    preflight?.broker_otc_or_indicative_available ||
    market.quoteRows.some((instrument) => instrument.venue_type === "broker_otc")
  ) {
    return "Внебиржевая торговля";
  }
  if (
    preflight?.venue_type === "broker_indicative" ||
    market.quoteRows.some((instrument) => instrument.venue_type === "broker_indicative")
  ) {
    return "Индикативные котировки";
  }
  if (
    preflight?.official_exchange_closed ||
    market.dataShadowStatus.market_closed_expected ||
    market.dashboardFeedStatus.market_open === false
  ) {
    return "Площадка закрыта";
  }
  return "Площадка уточняется";
}

function phaseDetail(): string {
  const preflight = robot.lastSessionPreflight;
  if (market.dashboardFeedStatus.last_refresh_at) {
    return `feed ${compactDateTime(market.dashboardFeedStatus.last_refresh_at)}`;
  }
  if (preflight?.official_exchange_closed) {
    return `Следующая: ${compactDateTime(preflight.next_session_at)}`;
  }
  const expected = market.dataShadowStatus.market_closed_expected;
  if (expected === true) {
    return `Следующая: ${compactDateTime(market.dataShadowStatus.next_session_at)}`;
  }
  if (expected === false) {
    return "Сбор разрешен.";
  }
  return "Проверяю.";
}

function brokerStatusValue(): string {
  const raw = robot.status.broker_trading_status;
  if (raw === "normal_trading") {
    return "Брокер доступен";
  }
  if (raw === "closed" || raw === "not_available_for_trading") {
    return "Торги закрыты";
  }
  if (raw && raw !== "unknown") {
    return raw.replaceAll("_", " ");
  }
  if (!robot.status.balance.balance_degraded || market.quoteRows.some((item) => item.last_price)) {
    return "Readonly доступен";
  }
  return "Проверяется";
}

function tradingModeValue(): string {
  if (market.dataShadowStatus.collector_state === "collecting") {
    return "Data-only сбор";
  }
  return "Торговля отключена";
}

function tradingModeDetail(): string {
  if (market.dashboardFeedStatus.running && market.dataShadowStatus.collector_state !== "collecting") {
    return "Рынок отображается. Запись data-only логов остановлена.";
  }
  return "Data-only Start управляет только записью логов; заявок, pseudo-orders и signal_candidate нет.";
}

function formatBps(value: string | null | undefined): string {
  return value === null || value === undefined ? "Нет данных" : `${formatDecimal(value, 1)} bps`;
}

function formatSpread(instrument: MarketInstrumentOverview | null): string {
  if (!instrument?.spread_bps && !instrument?.spread_abs_rub && !instrument?.spread_abs) {
    return "Нет данных";
  }
  return `${formatBps(instrument.spread_bps)} / ${formatPriceValue(
    instrument.spread_abs_rub ?? instrument.spread_abs,
  )} ₽`;
}

function formatPriceValue(value: string | null | undefined): string {
  return formatDecimal(value, 2);
}

function formatLots(value: string | null | undefined): string {
  return value === null || value === undefined ? "Нет данных" : `${formatDecimal(value, 0)} лотов`;
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
  const timestamp = compactDateTime(instrument.last_price_at);
  if (instrument.quote_status === "live") {
    return `${timestamp} · свежая`;
  }
  if (instrument.quote_status === "broker_quote") {
    return `${timestamp} · брокерская, не для калибровки`;
  }
  if (instrument.quote_status === "indicative") {
    return `${timestamp} · индикативная`;
  }
  if (instrument.quote_status === "previous_close") {
    return `${timestamp} · пред. закрытие`;
  }
  return `${timestamp} · устарела`;
}

function venueLabel(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    official_exchange: "биржевая",
    broker_otc: "внебиржевая",
    broker_indicative: "индикативная",
    stale_local: "локальная история",
    unknown: "источник уточняется",
  };
  return labels[value ?? "unknown"] ?? value ?? "источник уточняется";
}

function collectionAllowedLabel(): string {
  const preflight = robot.lastSessionPreflight;
  if (preflight?.data_only_collection_allowed === true) {
    return "разрешён";
  }
  if (preflight?.data_only_collection_allowed === false) {
    return "заблокирован";
  }
  if (market.dataShadowStatus.market_open === false) {
    return "заблокирован";
  }
  return "уточняется";
}

function collectionReason(): string {
  const reason =
    robot.lastSessionPreflight?.reason_code ??
    market.dataShadowStatus.reason_code ??
    robot.lastCommandReasonCode;
  return reasonLabel(reason);
}

function dashboardFeedValue(): string {
  if (market.feedErrors.length) {
    return "ошибка";
  }
  if (market.dashboardFeedStatus.running) {
    return market.dashboardFeedStatus.market_open ? "online" : "stale/closed";
  }
  return "запускается";
}

function dashboardFeedDetail(): string {
  if (market.feedErrors.length) {
    return reasonLabel(market.feedErrors[0]);
  }
  if (market.feedWarnings.length) {
    return reasonLabel(market.feedWarnings[0]);
  }
  return market.dashboardFeedStatus.last_refresh_at
    ? `refresh ${compactDateTime(market.dashboardFeedStatus.last_refresh_at)}`
    : "Start не требуется";
}

function tradeTapeReason(): string {
  return reasonLabel(
    selectedInstrument.value?.trade_tape_reason ??
      selectedInstrument.value?.trade_tape_status ??
      selectedInstrument.value?.market_trades_source ??
      "no_market_trades_samples",
  );
}

function tradeTapeStatusLabel(): string {
  const status = selectedInstrument.value?.trade_tape_status;
  if (market.recentTrades.length && status === "live") {
    return "рыночный поток";
  }
  if (market.recentTrades.length && status === "stale") {
    return "лента задержана";
  }
  return tradeTapeReason();
}

function nextSessionLabel(): string {
  const nextSession = robot.lastSessionPreflight?.next_session_at ?? market.dataShadowStatus.next_session_at;
  return nextSession ? `Следующая: ${compactDateTime(nextSession)}` : "Следующая сессия не указана";
}

function hasRealOrderBook(instrument: MarketInstrumentOverview | null): boolean {
  return Boolean(
    instrument?.order_book_source &&
      !instrument.order_book_stale &&
      instrument.best_bid &&
      instrument.best_ask,
  );
}

function displayQualityValue(instrument: MarketInstrumentOverview | null): string {
  if (!hasRealOrderBook(instrument)) {
    return "нет стакана";
  }
  return formatPercentRatio(instrument?.display_market_quality_score);
}

function displayQualityDetail(instrument: MarketInstrumentOverview | null): string {
  if (!instrument) {
    return "instrument_unavailable";
  }
  if (!hasRealOrderBook(instrument)) {
    return reasonLabel(instrument.reason_code ?? "no_order_book_samples");
  }
  return instrument.market_quality_label ?? "unknown";
}

function displayQualityTone(instrument: MarketInstrumentOverview | null): "good" | "warn" | "info" {
  if (!hasRealOrderBook(instrument)) {
    return "warn";
  }
  return Number(instrument?.display_market_quality_score ?? 0) >= 0.7 ? "good" : "warn";
}

function calibrationQualityLabel(instrument: MarketInstrumentOverview | null): string {
  if (!instrument) {
    return "Нет данных";
  }
  if (!hasRealOrderBook(instrument)) {
    return "display-only";
  }
  if (!instrument.quote_allowed_for_data_collection) {
    return "не для калибровки";
  }
  return formatPercentRatio(instrument.calibration_market_quality_score);
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
    if (instrument.official_exchange_closed) {
      return "Брокерская котировка; не для калибровки.";
    }
    return "Стакан свежий.";
  }
  if (instrument.order_book_stale && instrument.order_book_ts) {
    return `Стакан устарел: ${compactDateTime(instrument.order_book_ts)}.`;
  }
  if (market.dashboardFeedStatus.errors.length) {
    return `Dashboard feed не получил стакан: ${reasonLabel(market.dashboardFeedStatus.errors[0])}.`;
  }
  if (market.dashboardFeedStatus.warnings.length) {
    return `Dashboard feed: ${reasonLabel(market.dashboardFeedStatus.warnings[0])}.`;
  }
  if (!market.dashboardFeedStatus.running) {
    return "Dashboard feed ещё запускается. Data-only Start для отображения стакана не нужен.";
  }
  if (market.dashboardFeedStatus.market_open === false) {
    return "Стакан не обновляется: рынок закрыт или брокер отдаёт только последнюю цену.";
  }
  return "Стакан не получен из readonly GetOrderBook. Экран держит последнюю цену и повторяет запрос.";
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
    </div>

    <div
      v-if="robot.commandLoading || robot.lastCommandStatus"
      class="command-status-panel"
      :class="{ 'command-status-panel--active': robot.commandLoading }"
    >
      <span v-if="robot.commandLoading" class="inline-spinner" aria-hidden="true" />
      <div>
        <p class="eyebrow">команда</p>
        <strong>{{ commandStatusLabel(robot.lastCommandStatus) }}</strong>
      </div>
      <p>{{ robot.lastCommandMessage }}</p>
      <code v-if="robot.lastCommandReasonCode">{{ reasonLabel(robot.lastCommandReasonCode) }}</code>
      <small v-if="robot.lastCommandNextSessionAt">next {{ compactDateTime(robot.lastCommandNextSessionAt) }}</small>
      <small v-if="robot.lastCommandAt">{{ compactDateTime(robot.lastCommandAt) }}</small>
      <button
        v-if="!robot.commandLoading"
        class="command-status-dismiss"
        type="button"
        aria-label="Скрыть уведомление"
        @click="robot.dismissCommand"
      >
        x
      </button>
    </div>

    <section class="session-ribbon" data-testid="session-ribbon">
      <div>
        <span class="eyebrow">session</span>
        <strong>{{ sessionLabel(market.dashboardFeedStatus.session_type ?? robot.status.session_type) }}</strong>
        <small>{{ sessionDetail() }}</small>
      </div>
      <div>
        <span class="eyebrow">phase</span>
        <strong>{{ phaseLabel(market.dashboardFeedStatus.session_phase ?? robot.status.session_phase) }}</strong>
        <small>{{ brokerStatusValue() }}</small>
      </div>
      <div>
        <span class="eyebrow">venue</span>
        <strong>{{ venueStatusValue() }}</strong>
        <small>{{ nextSessionLabel() }}</small>
      </div>
      <div>
        <span class="eyebrow">dashboard feed</span>
        <strong>{{ dashboardFeedValue() }}</strong>
        <small>{{ dashboardFeedDetail() }}</small>
      </div>
      <div>
        <span class="eyebrow">data-only</span>
        <strong>{{ collectionAllowedLabel() }}</strong>
        <small>{{ collectionReason() }}</small>
      </div>
    </section>

    <div class="metric-grid">
      <section class="balance-card" :class="{ 'balance-card--degraded': robot.status.balance.balance_degraded }">
        <div class="balance-card__top">
          <span>Брокерский счёт</span>
          <span v-if="robot.balanceRefreshLoading" class="inline-spinner" aria-hidden="true" />
        </div>
        <strong>{{ balanceValue() }}</strong>
        <p v-if="robot.status.balance.balance_degraded">{{ balanceUnavailableReason() }}</p>
        <dl>
          <div>
            <dt>свободно</dt>
            <dd>{{ availableCash() }}</dd>
          </div>
          <div>
            <dt>блок</dt>
            <dd>{{ blockedCash() }}</dd>
          </div>
        </dl>
      </section>

      <MetricTile label="Площадка" :value="venueStatusValue()" />
      <MetricTile label="Фаза рынка" :value="phaseLabel(market.dashboardFeedStatus.session_phase ?? robot.status.session_phase)" :detail="phaseDetail()" />
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

          <div v-if="quoteRows.length" class="quote-grid quote-grid--dashboard">
            <button
              v-for="instrument in quoteRows"
              :key="instrument.instrument_id"
              class="quote-card quote-card--rich"
              :class="{
                'quote-card--active': market.selectedInstrumentId === instrument.instrument_id,
                [`quote-card--${toneFromQuote(instrument)}`]: true,
              }"
              type="button"
              @click="market.selectedInstrumentId = instrument.instrument_id"
            >
              <span class="quote-card__top">
                <span class="quote-card__ticker">{{ instrument.ticker ?? instrument.instrument_id }}</span>
                <span class="status-badge" :class="`status-badge--${toneFromQuote(instrument)}`">
                  {{ statusLabel(instrument.quote_status) }}
                </span>
              </span>
              <strong>{{ formatDecimal(instrument.last_price, 2) }}</strong>
              <span class="quote-card__source">{{ sourceLabel(instrument.last_price_source) }}</span>
              <span class="quote-card__freshness">{{ quoteFreshness(instrument) }}</span>
              <span class="quote-card__meta">
                <small>bid/ask {{ formatDecimal(instrument.best_bid, 2) }} / {{ formatDecimal(instrument.best_ask, 2) }}</small>
                <small>спред {{ formatSpread(instrument) }}</small>
                <small>стакан {{ displayQualityValue(instrument) }}</small>
              </span>
              <small :class="`quote-change quote-change--${changeTone(instrument.change_bps)}`">
                {{ formatChangeBps(instrument.change_bps) }}
              </small>
            </button>
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
            <MetricTile label="Спред" :value="selectedInstrument ? formatSpread(selectedInstrument) : 'Нет данных'" detail="bps / ₽" tone="info" />
            <MetricTile label="Глубина" :value="`${formatLots(selectedInstrument?.bid_depth_lots)} / ${formatLots(selectedInstrument?.ask_depth_lots)}`" />
            <MetricTile label="Имбаланс" :value="formatDecimal(selectedInstrument?.book_imbalance, 3)" />
            <MetricTile label="Качество стакана" :value="displayQualityValue(selectedInstrument)" :detail="displayQualityDetail(selectedInstrument)" :tone="displayQualityTone(selectedInstrument)" />
            <MetricTile label="Калибровка" :value="calibrationQualityLabel(selectedInstrument)" :detail="selectedInstrument?.quote_allowed_for_data_collection ? 'можно использовать' : 'display-only'" tone="warn" />
            <MetricTile label="Источник" :value="venueLabel(selectedInstrument?.venue_type)" :detail="selectedInstrument ? sourceLabel(selectedInstrument.quote_source) : 'Нет данных'" />
            <MetricTile label="Статус стакана" :value="selectedInstrument?.order_book_stale ? 'устарел' : selectedInstrument?.order_book_source ? 'свежий' : 'нет стакана'" :detail="orderBookReason()" />
          </div>

          <div class="market-depth-layout">
            <OrderBookWidget :instrument="selectedInstrument" />

            <section class="market-tape-card">
              <header class="market-tape-header">
                <div>
                  <h3>ЛЕНТА СДЕЛОК</h3>
                  <span>{{ selectedInstrument?.ticker ?? selectedInstrument?.instrument_id ?? "инструмент не выбран" }}</span>
                </div>
                <strong>{{ tradeTapeStatusLabel() }}</strong>
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
                :detail="`Причина: ${selectedInstrument?.market_trades_source ?? 'no_market_trades_samples'} (${tradeTapeReason()}). Появится после market trades feed; отсутствие ленты не скрывается.`"
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
          <p class="operator-note">
            {{ market.dashboardFeedStatus.running && market.dataShadowStatus.collector_state !== "collecting" ? "Рынок отображается. Запись логов остановлена." : "Start управляет только записью data-only логов." }}
          </p>
          <dl class="definition-grid">
            <dt>состояние</dt>
            <dd>{{ collectorLabel(market.dataShadowStatus.collector_state) }}</dd>
            <dt>рынок</dt>
            <dd>{{ market.dataShadowStatus.market_open === true ? "открыт" : market.dataShadowStatus.market_open === false ? "закрыт" : "уточняется" }}</dd>
            <dt>причина</dt>
            <dd>{{ reasonLabel(market.dataShadowStatus.reason_code) }}</dd>
            <dt>следующая сессия</dt>
            <dd>{{ compactDateTime(market.dataShadowStatus.next_session_at) }}</dd>
            <dt>поток</dt>
            <dd>{{ market.dataShadowStatus.stream_alive ? "samples идут" : "samples нет" }}</dd>
            <dt>snapshots</dt>
            <dd>{{ market.dataShadowStatus.market_microstructure_snapshots }}</dd>
            <dt>стаканы</dt>
            <dd>{{ market.dataShadowStatus.order_book_snapshots }}</dd>
            <dt>возраст sample</dt>
            <dd>{{ market.dataShadowStatus.last_message_age_seconds ?? "нет samples" }}</dd>
          </dl>
          <div v-if="market.dataShadowStatus.warnings.length" class="operator-list operator-list--compact">
            <div v-for="warning in market.dataShadowStatus.warnings" :key="warning">
              <strong>{{ reasonLabel(warning) }}</strong>
            </div>
          </div>
          <div v-if="market.warnings.length" class="operator-list operator-list--compact">
            <div v-for="warning in market.warnings" :key="warning">
              <strong>{{ reasonLabel(warning) }}</strong>
            </div>
          </div>
        </DataPanel>

        <DataPanel>
          <template #eyebrow>venue</template>
          <template #title>Площадка</template>
          <dl class="definition-grid">
            <dt>режим</dt>
            <dd>{{ venueStatusValue() }}</dd>
            <dt>фаза</dt>
            <dd>{{ phaseLabel(robot.status.session_phase) }}</dd>
            <dt>сбор</dt>
            <dd>{{ robot.lastSessionPreflight?.data_only_collection_allowed ? "разрешен" : "заблокирован" }}</dd>
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
      </div>
    </div>
  </section>
</template>

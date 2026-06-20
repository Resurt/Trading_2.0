const LABELS: Record<string, string> = {
  weekday_morning: "Утренняя",
  weekday_main: "Основная",
  weekday_evening: "Вечерняя",
  weekend: "Выходная",
  opening_auction: "Аукцион открытия",
  continuous_trading: "Торги",
  closing_auction: "Аукцион закрытия",
  break: "Перерыв",
  dealer_mode: "Dealer mode",
  closed: "Закрыто",
  normal_trading: "Обычные торги",
  idle: "Ожидание",
  warming_up: "Прогрев",
  wait: "Ожидание сигнала",
  candidate: "Кандидат",
  blocked: "Заблокировано",
  placing_order: "Отправка заявки",
  working_order: "Заявка в рынке",
  partially_filled: "Частичное исполнение",
  in_position: "В позиции",
  exiting: "Выход",
  degraded: "Деградация",
  stopped: "Остановлен",
  unknown: "Нет данных",
  loading: "Загрузка",
  live: "Онлайн",
  enabled: "Включено",
  disabled: "Выключено",
  checking_preflight: "Preflight",
  start_requesting: "Старт",
  stop_requesting: "Остановка",
  blocked_by_preflight: "Старт заблокирован",
  preflight_failed: "Preflight недоступен",
  balance_refresh_degraded: "Счёт не получен",
  balance_refresh_completed: "Счёт обновлён",
  balance_refresh_failed: "Ошибка счёта",
  start_requested: "Запуск запрошен",
  stop_requested: "Остановка запрошена",
  spread_too_wide: "Спред слишком широкий",
  market_quality_low: "Низкое качество рынка",
  stale_market_data: "Устаревшие данные",
  no_edge_after_costs: "Нет преимущества после издержек",
  risk_budget_exceeded: "Риск-бюджет превышен",
  session_forbidden: "Сессия запрещает действие",
  phase_forbidden: "Фаза запрещает действие",
  order_type_forbidden: "Тип заявки запрещен",
  max_drawdown_reached: "Достигнут max drawdown",
  open_order_conflict: "Конфликт открытой заявки",
  position_limit_reached: "Лимит позиции достигнут",
  balance_unavailable: "Баланс недоступен",
  session_unavailable: "Сессия недоступна",
  no_active_instruments: "Нет активных инструментов",
  strategy_state_unavailable: "Состояние стратегии недоступно",
  long_bias: "Long bias",
  short_bias: "Short bias",
  mixed_flat: "Mixed-flat",
};

export function humanizeCode(code: string | null | undefined): string {
  if (!code) {
    return "Нет данных";
  }
  return LABELS[code] ?? code.replaceAll("_", " ");
}

export function codeWithLabel(code: string | null | undefined): string {
  if (!code) {
    return "Нет данных";
  }
  const label = humanizeCode(code);
  return label === code ? code : `${label} · ${code}`;
}

export function severityForCode(code: string | null | undefined): "good" | "warn" | "bad" | "info" {
  if (!code) {
    return "info";
  }
  if (
    [
      "continuous_trading",
      "normal_trading",
      "wait",
      "idle",
      "start_requested",
      "long_bias",
    ].includes(code)
  ) {
    return "good";
  }
  if (
    [
      "blocked",
      "degraded",
      "closed",
      "stopped",
      "spread_too_wide",
      "stale_market_data",
      "risk_budget_exceeded",
    ].includes(code)
  ) {
    return "bad";
  }
  if (code.endsWith("_unavailable") || code.includes("forbidden") || code.includes("low")) {
    return "warn";
  }
  return "info";
}

# Спецификация frontend dashboard

Frontend - это Vue 3 dark-theme интерфейс оператора и аналитика. Это не landing page и не декоративная витрина.

## Технологии

- Vue 3
- Vite
- Vue Router
- Pinia
- REST client
- WebSocket client
- dark theme design tokens

## BFF источники данных

Frontend должен использовать FastAPI BFF из `Docs/api-contract.md`.

REST endpoints:

- `/robot/status`
- `/session/current`
- `/session/preflight`
- `/portfolio/summary`
- `/portfolio/refresh`
- `/positions`
- `/orders/open`
- `/signals/current`
- `/market/overview`
- `/reports/hourly`
- `/reports/daily`
- `/reports/counterfactual`
- `/config/strategy`

WebSocket channels:

- `/ws/dashboard`
- `/ws/orders`
- `/ws/market`
- `/ws/reports`

Команды управления и ручной запуск отчетов проходят через auth abstraction BFF.
В local-dev frontend может отправлять dev header `X-API-Role: operator`, но
в `production` этот provider запрещен: используется bearer-token provider,
а сами команды сохраняются в `robot_command` и `audit_event`.

## Общий layout

Обязательные зоны:

- верхняя status bar;
- левая навигация;
- основная рабочая область;
- компактная темная UI-система для длительного мониторинга.

Основные страницы:

- `Live Dashboard`
- `Reports`
- `Settings`
- `Logs/Diagnostics`

## Live Dashboard

Live dashboard должен отвечать на два вопроса:

1. Что сейчас делает рынок?
2. Что сейчас делает робот?

Обязательные панели:

- баланс;
- активные инструменты;
- активные таймфреймы;
- `session_type`;
- `session_phase`;
- `broker_trading_status`;
- текущий `micro_session_id`;
- countdown до rollover;
- `strategy_state`;
- текущий `signal_candidate`;
- текущий `blocker_event`;
- spread;
- mid price;
- market quality score;
- top of book;
- order book widget;
- recent market trades tape;
- positions;
- active orders;
- recent risk events;
- market stream health;
- last closed candle age;
- latest hourly report status.

## Reports

Страница отчетов нужна для анализа дня, часа, сессии, инструмента и blockers.

Фильтры:

- `calendar_date`;
- `trading_date`;
- `session_type`;
- `micro_session_id`;
- instrument;
- timeframe;
- strategy id;
- blocker code;
- order status;
- cancel reason;
- reject reason.

Панели:

- day trend / market regime;
- session-wise PnL;
- hourly micro-session comparison;
- candidate funnel;
- blocker ranking;
- execution quality;
- counterfactual outcomes 5/10/15 минут;
- infra health;
- risk events list;
- cancelled/rejected orders drill-down.

Каждый blocker должен открываться в drill-down с:

- `blocker_code`;
- `gate_name`;
- `gate_rank`;
- `reason_payload`;
- market context;
- session context;
- counterfactual result, если он уже посчитан.

## Settings

Начальные блоки:

- включенные instruments;
- включенные timeframes;
- strategy config по session template;
- risk limits;
- freeze window перед границей micro-session;
- режим запуска: `historical_replay`, `sandbox`, `shadow`, `production`;
- secret status без отображения значений секретов.

## Logs/Diagnostics

Эта страница показывает операционную диагностику. Она не заменяет аналитические отчеты из PostgreSQL.

Панели:

- service health;
- reconnects;
- stale data;
- broker/API errors;
- rate limit pressure;
- recent technical errors;
- correlation search по `run_id`, `micro_session_id`, `candidate_id`, `order_intent_id`, `request_order_id`, `exchange_order_id`.

## Dark theme tokens

UI должен определить tokens:

- background;
- surface;
- elevated surface;
- text primary;
- text secondary;
- text muted;
- border;
- success;
- warning;
- danger;
- info;
- long;
- short;
- flat;
- active;
- inactive;
- disabled.

Интерфейс должен быть плотным, читаемым и удобным для повторяющейся операторской работы.

## Реализация шага 11

Фактическая реализация находится в `apps/frontend/src`.

Карта страниц:

- `LiveDashboardView` - live состояние робота, сессии, рынка, стакана, позиций, заявок и risk events.
- `ReportsView` - фильтры, rebuild daily report, daily/hourly reports, blocker ranking, counterfactual missed opportunities и summary charts.
- `SettingsView` - strategy config по session template, risk limits, active instruments/timeframes и secret status без значений секретов.
- `DiagnosticsView` - WebSocket/API degraded states, correlation search и cancelled/rejected order reason codes.

Ключевые компоненты:

- `DataPanel` - базовая рабочая панель.
- `MetricTile` - компактная метрика.
- `StatusPill` - readable label + machine-readable code.
- `EmptyState` - пустое или degraded состояние.
- `MiniBars` - простые summary charts без тяжелой chart-библиотеки.
- `OrderBookWidget` - top-of-book и lightweight depth summary.
- `SignalReasonCard` - текущий candidate/blocker с reason code.
- `RiskEventsList` - последние candidate/blocker события.

Pinia stores:

- Live Dashboard bootstrap - one aggregated `GET /dashboard/state` snapshot.
- `robot` - dashboard snapshot, `/session/preflight`, `/portfolio/refresh`,
  `/ws/dashboard`, start/stop commands and last command result.
- `market` - dashboard snapshot, `/market/overview`, `/ws/market`, selected instrument
  и top-of-book read model.
- `portfolio` - dashboard snapshot, `/positions`, `/orders/open`, `/ws/orders`.
- `reports` - loaded on the Reports page only: `/reports/hourly`, `/reports/daily`,
  `/reports/counterfactual`, `/reports/daily/run`, `/ws/reports`.

Live widgets:

- balance;
- session type / phase / broker trading status;
- current micro-session и countdown до rollover;
- strategy state;
- current signal/candidate/blocker;
- spread / mid price / market quality;
- top-of-book и order book summary;
- recent market trades tape;
- positions;
- active orders;
- recent risk events;
- degraded flags;
- latest hourly report;
- freshness timestamps.

REST используется для initial snapshot/history. WebSocket используется для snapshot/live
обновлений. BFF WebSocket держит соединение открытым, отправляет snapshot при
подключении, затем push-обновления по interval и heartbeat.

## Current pages and blocks

Current pages:

- Live Dashboard
- Historical Data
- Reports
- Intraday Analytics
- Calibration / Calibration Center
- Settings
- Logs / Diagnostics

Current Live Dashboard blocks:

- Balance card
- session type, phase, broker status and micro-session
- one cockpit-style broker/API connection chip with a human label, not repeated
  `panel/quotes/portfolio` chips in several places
- compact quote cards for the core universe: SBER, GAZP, LKOH, YDEX, TATN, GMKN, OZON, VTBR
- Data-only Shadow Status
- selected instrument panel with price, bid/ask, mid, spread, depth, imbalance and book quality
- selected instrument market depth block: order-book ladder on the left and market trades tape on the right
- current signal and blocker reason
- recent risk events
- stream health

Balance card:

- shows portfolio value, available cash and blocked cash in the primary card;
- does not show full account id; the main dashboard card also avoids account id,
  expected yield and freshness clutter;
- auto-refreshes broker balance through readonly `POST /portfolio/refresh` while the
  dashboard is open;
- shows human-readable degraded reasons when broker balance is missing; raw technical
  codes are not the primary UI text;
- remains readonly account state in data-only shadow mode and never implies trading permission.

Start/Stop command feedback:

- Start first calls `GET /session/preflight` with the core data-only universe.
- If `market_open=false`, Start shows rejected/preflight-blocked state, `reason_code`
  and `next_session_at` when present. The frontend still calls `POST /robot/start`
  once so the API persists a rejected `robot_command`/audit event; trade-core does
  not start streams.
- If `market_open=true`, Start submits the data-only start command and shows the
  returned `RobotCommandResponse`.
- During Start, the button shows an animated progress state and a compact command strip
  appears only while a command is running or after a real command result.
- Stop always submits controlled stop and shows requested/stopped/error result in
  the command status panel.
- The latest command state is visible in human-readable form with reason and next
  session when applicable; the dashboard must not show an empty technical
  `last command` block before any command exists.

Quotes:

- `GET /market/overview` is fast and local. It must return all eight core universe
  rows without synchronous broker calls.
- Quote rows show `last_price`, `last_price_at`, `last_price_source`,
  `quote_status`, `is_price_stale`, `price_staleness_seconds`, spread bps, bid/ask,
  depth, imbalance and book quality where available.
- Source priority is fresh order-book mid, explicit readonly T-Bank quote refresh,
  latest known candle close, previous close, then unavailable.
- `POST /market/quotes/refresh` is the explicit readonly broker refresh path for
  `GetLastPrices`/`GetOrderBook`.
- A successful readonly `GetOrderBook` response is treated as fresh by broker
  response receipt time; the exchange timestamp remains visible only as a
  diagnostic field.
- Successful readonly quote refresh rows are cached briefly by the API, so a later
  `/market/overview` or dashboard snapshot cannot immediately replace live broker
  quotes with older candle fallback rows.
- Polling model while the dashboard is open: local `/market/overview` and
  `/runtime/data-shadow/status` every 5 seconds, readonly broker
  `/market/quotes/refresh` no more often than every 30 seconds, and
  `/portfolio/refresh` every 20 seconds.
- If a refresh request times out, the dashboard keeps the last good quote instead of
  clearing the quote grid.
- The quote grid is card-based and must not require horizontal scrolling for the
  eight core instruments.
- Stale candles/order books remain visible with timestamp and stale badge, never as
  current live prices.
- Balance, session state and current/last prices must be visible without starting
  strategy shadow or live trading.

Selected instrument depth:

- When the operator clicks a core universe row, the selected instrument block must
  render an order-book ladder with bid price, bid volume, ask price and ask volume.
- The selected instrument depth/tape layout must stay inside the selected
  instrument panel. It may stack ladder and tape vertically on narrower screens to
  avoid overlap with the side column.
- Ladder volume bars use only real `order_book_summary.bids`/`asks` levels from
  live data-only storage or explicit readonly `GetOrderBook` refresh. The UI must
  not invent synthetic depth levels.
- If there are no bid/ask levels, the block stays visible and shows the reason
  (`no_order_book_samples`, market closed, stream unavailable, stale book, etc.).
- The right side shows recent market trades when the market trades stream exists.
  If not available, it shows `no_market_trades_samples` instead of an empty panel.

Analytics pages:

- Historical Data: candle quality, instrument registry, dividend/special-day state and replay links.
- Intraday Analytics: session tabs for morning/main/evening/weekend, per-session summary, hour/micro-session rows and instrument x timeframe x side table.
- Calibration Center: Run Diagnostics, diagnosis, rolling performance cube, filters, market regime summary, top/dead contours, warnings and candidate config proposals.
## Market Source Display

The Live Dashboard must not show broker OTC/indicative quotes as MOEX live. Top-level
status separates official MOEX session, broker quote availability, data-only collection,
and trading-disabled state. Quote cards show source, venue, freshness, spread in
`bps / RUB`, and whether the row is eligible for calibration.

When MOEX is officially closed and broker quotes are available, the UI displays a
broker/indicative badge and a closed-exchange reason. Stale candle fallback remains
visible with timestamp and stale badge, but is never labelled live.

The selected instrument panel shows venue type, quote source, bid/ask/mid, spread,
depth, imbalance, display quality, calibration quality, order-book age, and trade tape
status. `/ws/market` is the primary live path; REST polling is fallback and must not
clear last good data on timeout.

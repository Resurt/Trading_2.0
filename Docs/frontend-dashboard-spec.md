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

Команды управления и ручной запуск отчетов должны отправлять placeholder role header `X-API-Role: operator` до внедрения полноценной auth-модели.

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

- `robot` - `/robot/status`, `/session/current`, `/signals/current`, `/ws/dashboard`, start/stop commands.
- `market` - `/market/overview`, `/ws/market`, selected instrument и top-of-book read model.
- `portfolio` - `/positions`, `/orders/open`, `/ws/orders`.
- `reports` - `/reports/hourly`, `/reports/daily`, `/reports/counterfactual`, `/reports/daily/run`, `/ws/reports`.

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

REST используется для initial snapshot/history. WebSocket используется для snapshot/live обновлений. Текущий BFF WebSocket пока отправляет snapshot и закрывает соединение; frontend отображает это как `snapshot_closed`, а не как ошибку.

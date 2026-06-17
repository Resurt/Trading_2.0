# Архитектура торгового робота

Этот файл является каноническим архитектурным документом проекта. Перед любой разработкой нужно сначала прочитать:

- `Docs/architecture.md`
- `Docs/implementation-plan.md`
- `Docs/logging-analytics-spec.md`
- все ADR из `Docs/adr/`

Основа документации взята из исследовательских документов в корне проекта:

- архитектурный документ для торгового робота на MOEX по SBER и GAZP;
- пошаговый план и промпты для Codex по сборке торгового робота для MOEX.

Проект строится для торговли акциями Московской биржи через T-Invest API. Первая рабочая вселенная: `SBER` и `GAZP`. `LKOH` разрешен как следующий инструмент после стабилизации первой итерации.

## Архитектурные инварианты

1. Бэкенд полностью на Python.
2. Фронтенд на Vue 3 в dark theme.
3. Критический путь живет в долгоживущем контейнере `trade-core`.
4. `trade-core` не перезапускается физически каждый час.
5. T-Bank gRPC - основной брокерский транспорт.
6. WebSocket используется для live feed и BFF, но не заменяет все торговые методы.
7. PostgreSQL - источник истины по состоянию, ордерам, событиям стратегии, отчетам и аудиту.
8. Redis - брокер фоновых задач и легкий coordination/cache слой.
9. Prometheus + Grafana - метрики и дашборды.
10. Loki + Fluent Bit - технические логи.
11. Сырые технические логи не являются основным источником аналитики в файлах или PostgreSQL.
12. Доменные события и агрегаты хранятся в PostgreSQL.
13. Технические JSON logs отправляются в Loki.
14. Часовые сессии - логические `micro-session` внутри биржевой торговой сессии.
15. Тяжелые отчеты и дневная аналитика выполняются через `report-worker`, а не в процессе `api`.
16. Логирование и доменные события должны поддерживать машинную аналитику и counterfactual-разбор заблокированных или отмененных сделок.
17. Секреты не коммитятся. Production использует Docker Compose secrets. Dev fallback через локальные env допустим только без реальных ключей.

## Компоненты системы

| Компонент | Технологии | Ответственность |
| --- | --- | --- |
| `trade-core` | Python 3.12, asyncio | Рыночные данные, сессии, bar engine, strategy engine, risk engine, execution engine, reconciliation, запись доменных событий. |
| `api` | Python, FastAPI | REST для управления и чтения данных, WebSocket BFF для live dashboard. Не строит тяжелые отчеты внутри процесса. |
| `report-worker` | Python, Celery | Celery worker очереди `reports`: часовые отчеты, дневные отчеты, rebuild jobs, counterfactual analytics, калибровочные агрегаты. |
| `report-worker-health` | Python HTTP health server | Легкий `/health` и `/metrics` endpoint для `report-worker`; тяжелые отчеты здесь не выполняются. |
| `frontend` | Vue 3, Vite, Pinia, Vue Router | Темный операторский интерфейс: live dashboard, отчеты, настройки, диагностика. |
| `postgres` | PostgreSQL | Источник истины для конфигурации, состояния, сессий, ордеров, событий, отчетов и аудита. |
| `redis` | Redis | Celery broker, coordination/cache слой. |
| `prometheus` | Prometheus | Сбор метрик `trade-core`, `api`, `report-worker-health` и инфраструктуры. |
| `grafana` | Grafana | Дашборды по Prometheus и Loki. |
| `loki` | Loki | Хранилище технических логов. |
| `fluent-bit` | Fluent Bit | Сбор stdout/stderr контейнеров и отправка JSON logs в Loki. |

## Топология

```text
T-Invest API
  gRPC unary methods + gRPC streams
  optional JSON WebSocket streams
        |
        v
trade-core
  market gateway
  session manager
  hourly micro-session manager
  bar engine
  strategy engine
  risk engine
  execution engine
  reconciliation service
  domain event writer
  metrics/logging
        |                 |                 |
        v                 v                 v
PostgreSQL           Redis/Celery       Prometheus
        |                 |                 |
        +-----------> FastAPI BFF <--------+
                         |
                   REST + WebSocket
                         |
                         v
                 Vue 3 dark dashboard

stdout/stderr JSON logs -> Fluent Bit -> Loki -> Grafana
```

## `trade-core`

`trade-core` - главный долгоживущий процесс. В нем находится все, что относится к критическому торговому пути:

- `market gateway`;
- `session manager`;
- `hourly micro-session manager`;
- `bar engine`;
- `strategy engine`;
- `risk engine`;
- `execution engine`;
- `reconciliation service`;
- запись доменных событий в PostgreSQL;
- техническое логирование и метрики.

Внутри `trade-core` допускается внутренняя event model. Внешний брокер сообщений не должен стоять на каждом шаге критического пути, чтобы не добавлять лишние сетевые задержки и точки отказа.

## Брокерский транспорт

Основной транспорт к T-Bank/T-Invest - gRPC.

`trade-core` работает с брокером через интерфейс `BrokerGateway`. Первая реализация - `TBankBrokerGateway`.

`BrokerGateway` должен разделять:

- unary methods;
- streaming methods;
- auth/secrets;
- retry/backoff;
- per-method deadlines;
- idempotency;
- reconciliation helpers.

Обязательные методы адаптера:

- `TradingSchedules`
- `GetTradingStatus`
- `GetCandles`
- `GetLastPrices`
- `GetOrderBook`
- `PostOrder`
- `CancelOrder`
- `GetOrderState`
- `GetOrders`
- `PostStopOrder`

Для ордеров система хранит и внутренний `request_order_id`, и брокерский `exchange_order_id`. Это нужно для идемпотентности, reconciliation и расследования инцидентов.

## WebSocket

WebSocket используется в двух местах:

1. как опциональный брокерский streaming-канал там, где это явно оправдано;
2. как BFF-канал FastAPI -> Vue 3 для live dashboard.

WebSocket не должен подменять все торговые методы брокера. Размещение ордеров, отмена, статус ордера, расписания и reconciliation остаются за `BrokerGateway` и gRPC/unary API.

## Сессионная модель

`session manager` не должен быть таблицей констант в коде. Источники правды:

- расписание через `TradingSchedules`;
- фактический статус инструмента через `GetTradingStatus` или `Info` stream;
- допуск конкретного инструмента к утренней, вечерней и выходной сессии.

Канонические значения `session_type`:

- `weekday_morning`
- `weekday_main`
- `weekday_evening`
- `weekend`

Канонические значения `session_phase`:

- `opening_auction`
- `continuous_trading`
- `closing_auction`
- `break`
- `dealer_mode`
- `closed`

Каждое доменное событие должно хранить:

- `calendar_date`;
- `trading_date`;
- `session_type`;
- `session_phase`;
- `micro_session_id`;
- `broker_trading_status`.

Это особенно важно для выходных сессий, где календарная дата и торговый день могут различаться.

## Hourly micro-sessions

`hourly micro-session` - логический часовой bucket внутри текущей биржевой сессии.

Правила:

- границы micro-session привязаны к биржевому времени, а не ко времени запуска процесса;
- если робот запущен в `07:30`, первая micro-session заканчивается в `07:59:59`, а не в `08:30`;
- если биржевая сессия заканчивается раньше полного часа, micro-session закрывается на границе биржевой сессии;
- micro-session создается только в разрешенной торговой фазе;
- `trade-core` остается живым при rollover;
- за 60-90 секунд до границы включается `freeze new entries`;
- на границе пишется snapshot состояния;
- закрывается `session_run`;
- создается событие `session_run_closed`;
- задача отчета отправляется в `report-worker`;
- следующая micro-session стартует без физического рестарта `trade-core`.

Цель: не смешивать утренние, основные, вечерние и выходные данные, а также не перегружать отчетные скрипты огромными дневными кусками.

## Контуры хранения данных

Система разделяет четыре контура:

| Контур | Куда пишем | Для чего |
| --- | --- | --- |
| `technical logs` | stdout/stderr -> Fluent Bit -> Loki | Диагностика, ошибки, reconnect, latency, tracking id, rate limits. |
| `domain events` | PostgreSQL | Машинная аналитика, отчеты, replay, калибровка, причины blocked/cancelled trades. |
| `metrics` | Prometheus | Временные ряды, latency, health, counters/gauges. |
| `reports` | PostgreSQL | Готовые hourly/daily агрегаты и counterfactual results. |

PostgreSQL хранит доменные сущности:

- `instrument_registry`
- `strategy_config`
- `session_run`
- `signal_candidate`
- `blocker_event`
- `order_intent`
- `broker_order`
- `fill_event`
- `risk_event`
- `position_snapshot`
- `market_candle`
- `market_status_snapshot`
- `order_book_summary`
- `strategy_state_event`
- `hourly_report`
- `daily_report`
- `robot_command`
- `report_job_outbox`
- `counterfactual_result`
- `audit_event`

В Docker Compose `trade-core`, `api` и `report-worker` обязаны использовать один PostgreSQL (`POSTGRES_HOST=postgres`, общие `POSTGRES_DB`/`POSTGRES_USER`, `POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password`). `trade-core` не имеет права молча переключаться на SQLite в compose/sandbox/shadow/production. SQLite fallback разрешён только для явного local режима `TRADING_RUNTIME_LOCAL_SQLITE=1`.

На startup `trade-core` пишет в structured log и `audit_event` поля `database_backend` и `database_url_redacted`. Launch readiness должен падать, если backend не `postgresql` или URL отличается от API/report-worker.

Таблицы с большим числом событий проектируются с учетом partitioning:

- `fill_event`
- `audit_event`
- `blocker_event`
- `strategy_state_event`
- `counterfactual_result`

## Отчеты и аналитика

Все тяжелые отчеты строит `report-worker` через Celery + Redis.

Основные задачи:

- `build_hourly_report`;
- `build_daily_report`;
- `rebuild_reports_for_date`;
- `run_counterfactual_analysis_for_date`.

Hourly report строится после закрытия micro-session.

Daily report строится по `trading_date` и должен уметь раскладывать день по:

- `weekday_morning`;
- `weekday_main`;
- `weekday_evening`;
- `weekend`;
- инструментам;
- таймфреймам;
- стратегиям;
- blocker codes.

Система должна объяснять не только сделки, которые состоялись, но и сделки, которые были заблокированы, отменены или отклонены.

## Observability

Обязательные метрики:

- `broker_post_order_latency_seconds`
- `order_state_convergence_seconds`
- `candle_close_delivery_lag_seconds`
- `session_rollover_duration_seconds`
- `report_generation_duration_seconds`
- `stream_reconnect_total`
- `rejected_orders_total`
- `risk_events_total`
- `counterfactual_jobs_total`
- `report_jobs_failed_total`
- `open_orders`
- `active_positions`
- `market_stream_alive`
- `last_stream_message_age_seconds`
- `celery_queue_backlog`

Prometheus labels не должны содержать значения с неограниченной кардинальностью: raw order id, candidate id, exception text и похожие поля нельзя использовать как labels.

## Режимы запуска

- `historical_replay` - детерминированный replay без брокера.
- `sandbox` - T-Invest sandbox для проверки инфраструктуры.
- `shadow` - live market data, сигналы и pseudo-orders без реальной отправки ордеров.
- `production` - реальная торговля с secrets из Docker Compose secrets и строгими risk limits.

Controlled launch policy:

- default mode is `historical_replay`;
- `production` requires `TRADING_PRODUCTION_CONFIRM=I_UNDERSTAND_LIVE_ORDERS`;
- `historical_replay` and `shadow` must not call real `PostOrder` or `CancelOrder`;
- `sandbox` may call readonly broker methods against sandbox target, but real
  sandbox `PostOrder`/`CancelOrder` requires explicit
  `TRADING_SANDBOX_ORDERS_CONFIRM=I_UNDERSTAND_SANDBOX_ORDERS`;
- `production` is enabled only after the final live checklist in `Docs/runbooks/production-checklist.md`.

## Секреты

Production секреты монтируются через Docker Compose secrets в `/run/secrets/*`.

Dev fallback через env допустим только локально и без реальных ключей.

Имена секретов:

- `tbank_full_access_token`
- `tbank_readonly_token`
- `postgres_password`
- `grafana_admin_password`

## Что не делаем на этом этапе

- Не реализуем торговую бизнес-логику.
- Не придумываем прибыльную стратегию.
- Не добавляем новые сервисы без архитектурной необходимости.
- Не используем физический hourly restart `trade-core`.
- Не храним сырые технические логи как основной источник аналитики в PostgreSQL или файлах.

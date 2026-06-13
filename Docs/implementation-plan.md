# План реализации

Этот roadmap задает порядок сборки проекта. Перед каждым шагом нужно прочитать:

- `Docs/architecture.md`
- `Docs/implementation-plan.md`
- `Docs/logging-analytics-spec.md`
- все ADR из `Docs/adr/`

Если в ходе шага меняется архитектурное решение, нужно обновить `Docs/` и ADR в том же изменении.

## Шаг 00 - документация и фиксация архитектуры

Цель: создать обязательную документационную базу до кодинга.

Артефакты:

- `Docs/architecture.md`
- `Docs/implementation-plan.md`
- `Docs/logging-analytics-spec.md`
- `Docs/frontend-dashboard-spec.md`
- `Docs/api-contract.md`
- ADR в `Docs/adr/`
- runbooks в `Docs/runbooks/`
- prompts в `Docs/prompts/`
- `README.md`

Критерии готовности:

- зафиксированы `trade-core`, `api`, `report-worker`;
- зафиксированы PostgreSQL, Redis, Prometheus, Grafana, Loki, Fluent Bit;
- зафиксирован T-Bank gRPC как primary transport;
- зафиксирован WebSocket для live feed и BFF;
- зафиксирована сессионная модель `weekend / weekday_morning / weekday_main / weekday_evening`;
- зафиксированы hourly micro-sessions без физического рестарта `trade-core`;
- зафиксированы контуры `technical logs / domain events / metrics / reports`.

## Шаг 01 - каркас репозитория

Цель: создать monorepo-структуру и базовые стандарты качества.

Артефакты:

- `apps/trade-core`
- `apps/api`
- `apps/report-worker`
- `apps/frontend`
- `packages/common`
- `tests`
- `scripts`
- `pyproject.toml`
- Python package layout через `src/`
- общие типы, enums, dataclasses или Pydantic models в `packages/common`
- Vue 3 + Vite каркас
- Vue Router
- Pinia
- dark theme design tokens
- `Makefile`
- pre-commit config

Команды, которые должны появиться:

- `make lint`
- `make test`
- `make up`
- `make down`
- `make logs`

Критерии готовности:

- Python-пакеты импортируются;
- frontend собирается;
- есть минимальные тесты и команды качества;
- бизнес-логика стратегии еще не реализуется.

## Шаг 02 - Docker Compose и инфраструктура

Цель: собрать production-like локальный стек.

Сервисы:

- `postgres`
- `redis`
- `loki`
- `fluent-bit`
- `prometheus`
- `grafana`
- `trade-core`
- `api`
- `report-worker`
- `frontend`

Артефакты:

- compose-файлы;
- healthcheck для каждого сервиса;
- Docker Compose secrets;
- Fluent Bit config для отправки логов в Loki;
- Prometheus scrape config;
- Grafana provisioning для Prometheus и Loki.

Критерии готовности:

- стек стартует одной командой;
- health endpoints зеленые;
- Grafana видит Prometheus и Loki;
- реальные секреты не закоммичены.

## Шаг 03 - схема PostgreSQL

Цель: реализовать модели, миграции и слой доступа.

Технологии:

- SQLAlchemy 2.x;
- Alembic;
- PostgreSQL.

Минимальные таблицы:

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
- `strategy_state_event`
- `hourly_report`
- `daily_report`
- `counterfactual_result`
- `audit_event`

Обязательные поля контекста:

- `calendar_date`
- `trading_date`
- `session_type`
- `session_phase`
- `micro_session_id`
- `broker_trading_status`

Критерии готовности:

- миграции применяются и откатываются;
- есть тесты ограничений и базовых repository methods;
- event-heavy таблицы спроектированы с учетом partitioning.

## Шаг 04 - T-Bank Broker Gateway

Цель: сделать безопасную границу между торговым ядром и T-Invest API.

Артефакты:

- интерфейс `BrokerGateway`;
- реализация `TBankBrokerGateway`;
- загрузка secrets из `/run/secrets/*`;
- dev fallback через локальные env;
- разделение unary и streaming methods;
- retry/backoff;
- per-method deadlines;
- генерация idempotent `request_order_id`;
- reconciliation helpers.

Обязательные методы:

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

Критерии готовности:

- адаптер тестируется на mock без реального токена;
- покрыты secret loading, idempotency, retry и deadlines.

## Шаг 05 - Session Manager и micro-sessions

Цель: реализовать сессионную модель.

Артефакты:

- `SessionManager`;
- `HourlyMicroSessionManager`;
- `session_type`;
- `session_phase`;
- freeze window за 60-90 секунд;
- snapshot на границе;
- `session_run_closed`;
- enqueue report task.

Реализация шага зафиксирована в `Docs/session-manager.md`.

Критерии готовности:

- покрыт старт внутри неполного часа;
- покрыта граница часа;
- покрыта граница биржевой сессии;
- покрыты выходные;
- покрыты auction/break phases;
- физический рестарт `trade-core` не используется.

## Шаг 06 - Market Data Pipeline и Bar Engine

Цель: построить основу рыночных данных и live dashboard.

Артефакты:

- подписки на candles;
- order book;
- last prices;
- trading status/info;
- market trades;
- user order state stream;
- internal event bus внутри `trade-core`;
- bar engine для 5m/10m/15m closed bars;
- market state calculators.

Реализация шага зафиксирована в `Docs/market-data-pipeline.md`.

Критерии готовности:

- replay tests воспроизводят бары детерминированно;
- stale data и feed freshness измеряются;
- данные готовы для API/frontend read models.

## Шаг 07 - Strategy, Risk, Execution, Reconciliation

Цель: сделать расширяемый торговый каркас без попытки придумать прибыльную модель.

Артефакты:

- `StrategyEngine`;
- `RiskEngine`;
- `ExecutionEngine`;
- `ReconciliationService`;
- конфигурационная стратегия-заглушка;
- explicit blocker codes;
- causal gate chain;
- order intent lifecycle;
- cancel/reject reason codes.

Реализация шага зафиксирована в `Docs/strategy-risk-execution.md`.

Критерии готовности:

- каждая заблокированная или отмененная попытка имеет reason code;
- решения воспроизводятся в replay;
- стратегия отделена от брокерского транспорта.

## Шаг 08 - логирование, метрики и корреляция

Цель: сделать production-like observability.

Артефакты:

- JSON structured logging на стандартном Python logging;
- context propagation через `contextvars`, `LoggerAdapter` или filters;
- technical logs -> stdout -> Fluent Bit -> Loki;
- domain events -> PostgreSQL;
- metrics -> Prometheus;
- canonical log schema в `Docs/logging-analytics-spec.md`.

Реализация шага зафиксирована в `Docs/logging-analytics-spec.md` и
`trading_common.observability`.

Критерии готовности:

- тесты проверяют JSON shape;
- тесты проверяют context propagation;
- Prometheus endpoint отдает обязательные метрики.

## Шаг 09 - Report Worker и analytics

Цель: реализовать hourly/daily reports и counterfactual-разбор.

Артефакты:

- Celery + Redis;
- `build_hourly_report`;
- `build_daily_report`;
- `rebuild_reports_for_date`;
- `run_counterfactual_analysis_for_date`;
- CLI scripts для ручного запуска отчетов.

Реализация шага зафиксирована в `Docs/logging-analytics-spec.md` и
`report_worker.analytics`.

Критерии готовности:

- отчеты строятся вне `api`;
- daily report содержит market regime, candidate funnel, blocker ranking, execution quality, counterfactual, session segmentation, infra health;
- counterfactual windows 5/10/15 минут сохраняются в PostgreSQL.

## Шаг 10 - FastAPI BFF

Цель: реализовать backend-for-frontend.

Артефакты:

- REST endpoints из `Docs/api-contract.md`;
- WebSocket channels `/ws/dashboard`, `/ws/orders`, `/ws/market`, `/ws/reports`;
- `/robot/status`;
- report task trigger endpoints.

Реализация шага зафиксирована в `Docs/api-contract.md` и `apps/api/src/trading_api`.

Критерии готовности:

- API tests покрывают ключевые маршруты;
- WebSocket tests покрывают основные сообщения;
- тяжелая аналитика только ставится в очередь `report-worker`.

## Шаг 11 - Vue 3 frontend

Цель: реализовать dark-theme UI для live-торговли и аналитики.

Артефакты:

- Live Dashboard;
- Reports;
- Settings;
- Logs/Diagnostics;
- REST/WebSocket clients;
- drill-down по blocker/cancel/reject events.

Реализация шага зафиксирована в `Docs/frontend-dashboard-spec.md` и `apps/frontend/src`.

Критерии готовности:

- интерфейс работает на mock data;
- текст не перекрывается;
- live dashboard и reports показывают ключевые статусы и аналитику.

## Шаг 12 - controlled launch

Цель: довести проект до replay/sandbox/shadow/production readiness.

Артефакты:

- `historical_replay`;
- `sandbox`;
- `shadow`;
- `production`;
- replay harness;
- sandbox smoke tests;
- shadow mode без реального `PostOrder`;
- CI pipeline;
- runbooks.

Критерии готовности:

- replay проверяет session rollovers, blockers и counterfactual pipeline;
- shadow mode пишет полную аналитику без реальной отправки ордеров;
- запуск и инциденты описаны в runbooks.

# Trading 2.0

Проект торгового робота для Московской биржи через T-Invest API.

Целевая архитектура:

- backend полностью на Python;
- frontend на Vue 3 в dark theme;
- `trade-core` как долгоживущий критический контейнер;
- T-Bank gRPC как primary broker transport;
- FastAPI BFF + WebSocket для live dashboard;
- PostgreSQL как source of truth по состоянию, ордерам, событиям, отчетам и аудиту;
- Redis для Celery и coordination/cache;
- Prometheus + Grafana для метрик;
- Loki + Fluent Bit для technical logs.

## Обязательное чтение перед разработкой

Перед любой задачей нужно прочитать:

- `Docs/architecture.md`
- `Docs/implementation-plan.md`
- `Docs/logging-analytics-spec.md`
- `Docs/logging_analytics_architecture.md`
- `Docs/logging_analytics_event_taxonomy.md`
- `Docs/logging_analytics_rollout_plan.md`
- `Docs/database-schema.md`
- `Docs/broker-gateway.md`
- `Docs/session-manager.md`
- `Docs/market-data-pipeline.md`
- `Docs/strategy-risk-execution.md`
- `Docs/observability_runbook.md`
- `Docs/live-analytics-bff.md`
- `Docs/logging_analytics_acceptance.md`
- все ADR из `Docs/adr/`

Если в ходе задачи меняется архитектурное решение, нужно обновить `Docs/` и соответствующий ADR в том же шаге.

## Текущее состояние

На этом этапе зафиксирована документация проекта, создан monorepo-каркас, добавлены
инфраструктурный compose-стек, схема PostgreSQL, BrokerGateway для T-Bank,
сессионная модель с hourly micro-sessions, market data pipeline с bar engine и
каркас strategy/risk/execution/reconciliation без прибыльной бизнес-логики.
Также добавлены structured JSON logging, Prometheus metrics registry и Grafana
dashboards provisioning для production-like observability. `report-worker`
содержит Celery task pipeline, hourly/daily reports, counterfactual analytics и
ручные CLI-скрипты для запуска отчетов вне FastAPI. `api` содержит FastAPI BFF
с REST endpoints для управления, live read models, отчетов, strategy config и
live WebSocket channels для dashboard/orders/market/reports. В production-like
режимах WebSocket в браузере авторизуется через короткоживущий ticket из
`POST /auth/ws-ticket`, а REST использует bearer auth. `frontend`
содержит Vue 3 dark-theme UI для live dashboard, reports, settings и diagnostics
с Pinia stores, REST snapshots и live WebSocket updates.

## Каркас репозитория

- `apps/trade-core` - долгоживущий Python runtime для session/market/strategy/risk/execution orchestration.
- `apps/api` - FastAPI BFF для управления, read models, отчетов и live WebSocket feeds.
- `apps/report-worker` - Celery/report worker для hourly/daily/counterfactual analytics.
- `apps/frontend` - Vue 3 + Vite dark-theme операторский UI.
- `packages/common` - общие enums и dataclasses.
- `tests` - backend unit/smoke/acceptance tests для runtime, API, SDK wrapper, analytics и launch gates.
- `scripts` - вспомогательные скрипты совместимости.
- `tools/reports` - CLI для hourly/daily/counterfactual отчетов вне FastAPI.

## Локальные проверки

```bash
python -m pytest
python -m ruff check .
python -m mypy
cd apps/frontend && npm run build
cd apps/frontend && npm run typecheck
cd apps/frontend && npm run test:unit
```

На Windows, если PowerShell блокирует `npm.ps1`, используйте `npm.cmd`.

Единая локальная проверка без зависимости от `make`:

```bash
python scripts/check.py
```

Полный local controlled-launch acceptance без реальных broker orders:

```bash
python scripts/run_controlled_launch_acceptance.py
```

Быстрый вариант, если `scripts/check.py` уже запускался отдельно:

```bash
python scripts/run_controlled_launch_acceptance.py --skip-full-check
```

Этот gate проверяет analytics-smoke, report rebuild, replay-day,
`docker compose config`, SQLite migration upgrade/downgrade/upgrade,
sandbox dry-run, production safety guards и secret scan.

Реальный T-Bank SDK wrapper подключается optional extra, чтобы обычный CI не зависел от
T-Bank package index:

```powershell
python -m pip install -e ".[tbank]" --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
$env:SSL_TBANK_VERIFY = "true"
$env:TBANK_UNARY_TIMEOUT_FLOOR_SECONDS = "5.0"
python scripts/run_sandbox_smoke.py --dry-run
```

Для реального readonly smoke без `--dry-run` токены должны лежать в ignored
`secrets/tbank_full_access_token` / `secrets/tbank_readonly_token`, а переменные
`TBANK_*_TOKEN_FILE` должны указывать на эти файлы. `SSL_TBANK_VERIFY=true`
включает bundled Russian Trusted Root CA в официальном T-Bank SDK; TLS verification
не отключается.

## Trade-core runtime

`python -m trade_core.service` запускает HTTP `/health` и `/metrics`, а также
фоновый `TradeCoreRuntime`. Безопасный режим по умолчанию - `historical_replay`:
он открывает logical micro-sessions, пишет domain events в БД, строит closed bars,
создаёт `signal_candidate`, прогоняет risk gates и создаёт pseudo-orders без
реальных broker calls.

Минимальный локальный запуск без T-Bank токенов:

```powershell
$env:TRADING_RUNTIME_MODE = "historical_replay"
python -m trade_core.service
```

Production не стартует без явного `TRADING_PRODUCTION_CONFIRM=I_UNDERSTAND_LIVE_ORDERS`.

### Launch blocker fixes

- В Docker Compose `trade-core`, `api` и `report-worker` используют один PostgreSQL через `POSTGRES_HOST=postgres`, `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD_FILE`.
- Runtime больше не уходит в SQLite молча. SQLite fallback разрешён только при явном `TRADING_RUNTIME_LOCAL_SQLITE=1` для локальных одно-процессных экспериментов.
- `trade-core` пишет в startup log/audit `database_backend` и `database_url_redacted`.
- Sandbox/shadow/production проверяют наличие T-Bank SDK extra; контейнерный build ставит `.[tbank]` через официальный T-Bank package index.
- Инструменты `SBER,GAZP` резолвятся через T-Bank instruments API в реальные `instrument_uid`/canonical `instrument_id`; placeholder UID запрещён для sandbox/shadow/production.
- API production использует `TRADING_AUTH_MODE=static_bearer`; браузерные WebSocket соединения получают короткоживущий ticket через `POST /auth/ws-ticket`.
- Для расширенной приёмки используйте `python scripts/run_launch_readiness.py --mode local|compose|sandbox|shadow|production-preflight`.

Приёмка logging/analytics слоя для калибровки:

```bash
make analytics-smoke
make report-rebuild
make replay-day
```

## Локальный Docker Compose

Создайте локальные Docker secrets в папке `secrets/` и не коммитьте их:

```bash
mkdir -p secrets
printf "local_postgres_password" > secrets/postgres_password
printf "local_grafana_password" > secrets/grafana_admin_password
printf "paste_full_access_token_here" > secrets/tbank_full_access_token
printf "paste_readonly_token_here" > secrets/tbank_readonly_token
```

Запуск:

```bash
docker compose up -d --build
docker compose ps
python -m alembic upgrade head
docker compose logs -f --tail=200
```

Локальные адреса:

- frontend: `http://localhost:5173`
- api health: `http://localhost:8000/health`
- trade-core health: `http://localhost:8001/health`
- report-worker health: `http://localhost:8002/health`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`
- Loki: `http://localhost:3100/ready`

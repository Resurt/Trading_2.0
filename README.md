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
- все ADR из `Docs/adr/`

Если в ходе задачи меняется архитектурное решение, нужно обновить `Docs/` и соответствующий ADR в том же шаге.

## Текущее состояние

На этом этапе зафиксирована документация проекта и создан monorepo-каркас. Бизнес-логика торгового робота еще не реализуется.

## Каркас репозитория

- `apps/trade-core` - долгоживущий Python service skeleton.
- `apps/api` - FastAPI BFF skeleton без реальных routes.
- `apps/report-worker` - Celery/report worker skeleton без задач.
- `apps/frontend` - Vue 3 + Vite dark-theme shell.
- `packages/common` - общие enums и dataclasses.
- `tests` - smoke tests для импортов Python-пакетов.
- `scripts` - место для CLI и вспомогательных скриптов следующих шагов.

## Локальные проверки

```bash
python -m pytest
python -m ruff check .
python -m mypy
cd apps/frontend && npm run build
```

На Windows, если PowerShell блокирует `npm.ps1`, используйте `npm.cmd`.

Единая локальная проверка без зависимости от `make`:

```bash
python scripts/check.py
```

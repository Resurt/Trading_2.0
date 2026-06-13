# Step 01 Prompt: Repository Bootstrap

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: создать монорепо-каркас проекта и базовые стандарты качества.

Сделай:

- `apps/trade-core`
- `apps/api`
- `apps/report-worker`
- `apps/frontend`
- `packages/common`
- `tests`
- `scripts`
- Python `pyproject.toml`
- единые настройки lint/type/test
- базовую структуру пакетов `src/`
- общие типы, enums, dataclasses или Pydantic models в `packages/common`
- Vue 3 + Vite frontend
- Vue Router
- Pinia
- dark theme design tokens
- `Makefile` с `make lint`, `make test`, `make up`, `make down`, `make logs`
- pre-commit config

Не реализуй прибыльную стратегию. Сначала каркас, типы, тесты и документация.

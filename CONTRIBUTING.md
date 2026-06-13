# Contributing

## Docs-first workflow

Перед любой разработкой сначала прочитайте:

- `Docs/architecture.md`
- `Docs/implementation-plan.md`
- `Docs/logging-analytics-spec.md`
- все ADR из `Docs/adr/`

Правило проекта: сначала `Docs`, потом код.

Если задача меняет архитектуру, контракты, reason codes, модель данных, observability или режимы запуска, сначала обновите соответствующий документ в `Docs/`, затем вносите код и тесты в том же изменении.

## Секреты

Реальные T-Bank токены, пароли, приватные ключи и `.env` с секретами не коммитятся.

Для production используются Docker Compose secrets. Для local dev допустимы только placeholder-значения из `.env.example`.

## Коммиты

Сообщения коммитов пишем на русском языке.

## Проверки

Минимальные проверки перед отправкой изменений:

```bash
python -m pytest
python -m ruff check .
python -m mypy
cd apps/frontend && npm run build
```

На Windows, если PowerShell блокирует `npm.ps1`, используйте `npm.cmd`.

Можно запустить весь набор одной командой:

```bash
python scripts/check.py
```

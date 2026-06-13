# ADR-006: тяжелые отчеты через `report-worker`

Status: Accepted

## Контекст

Hourly reports, daily reports, rebuild jobs и counterfactual analytics могут быть тяжелыми по CPU и базе данных. FastAPI handlers и FastAPI `BackgroundTasks` не должны выполнять такие задачи внутри процесса API.

## Решение

Тяжелые отчеты выполняет `report-worker` через Celery + Redis. `api` может поставить задачу и вернуть статус, но не считает отчет inline.

## Последствия

- API остается отзывчивым.
- Отчеты можно ретраить и мониторить отдельно.
- Результаты отчетов и task metadata хранятся в PostgreSQL.
- Дальние Celery ETA/countdown не используются как основная scheduling-модель.

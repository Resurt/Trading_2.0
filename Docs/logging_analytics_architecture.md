# Logging & Analytics Architecture

Документ фиксирует архитектуру логирования и аналитики для текущего состояния
репозитория `Resurt/Trading_2.0`. Это docs-only слой: он не меняет торговую
логику, а выравнивает уже реализованные компоненты и целевую каноническую
модель.

Связанные документы:

- `Docs/architecture.md`
- `Docs/logging-analytics-spec.md`
- `Docs/database-schema.md`
- `Docs/session-manager.md`
- `Docs/market-data-pipeline.md`
- `Docs/strategy-risk-execution.md`
- `Docs/controlled-launch.md`
- ADR из `Docs/adr/`

## Фактическая карта репозитория

| Компонент | Путь | Назначение |
| --- | --- | --- |
| `trade-core` | `apps/trade-core` | Долгоживущий Python service: broker adapter, session manager, hourly micro-sessions, market data pipeline, strategy/risk/execution, replay/shadow hooks. |
| `api` | `apps/api` | FastAPI BFF: REST endpoints, live WebSocket channels with ticket auth, read models, постановка report tasks в Celery. |
| `report-worker` | `apps/report-worker` | Celery worker и health service для hourly/daily reports, rebuild jobs, counterfactual analytics. |
| `frontend` | `apps/frontend` | Vue 3 + Vite + Pinia dark-theme UI для live dashboard, reports, settings, diagnostics. |
| `common` | `packages/common` | Общие enums, SQLAlchemy models/repositories, DB settings, observability primitives, launch modes. |
| `compose stack` | `docker-compose.yml`, `deploy/` | Postgres, Redis, Loki, Fluent Bit, Prometheus, Grafana и четыре app-сервиса. |
| `CI` | `.github/workflows/ci.yml` | lint, typecheck, backend/frontend tests, migration checks, smoke build. |

`APScheduler` в текущем репозитории не используется. Планирование тяжелых
отчетов сейчас строится вокруг событий micro-session rollover, ручных CLI и
Celery tasks через Redis, а не вокруг long ETA/countdown scheduling.

## Реальный путь запуска

Локальный production-like стек поднимается одной командой:

```bash
docker compose up -d --build
```

Эквивалент через Makefile:

```bash
make up
make down
make logs
make test
make lint
```

Основные local endpoints:

| Сервис | URL |
| --- | --- |
| `frontend` | `http://localhost:5173` |
| `api` health | `http://localhost:8000/health` |
| `trade-core` health | `http://localhost:8001/health` |
| `report-worker` health | `http://localhost:8002/health` |
| Prometheus | `http://localhost:9090` |
| Grafana | `http://localhost:3000` |
| Loki readiness | `http://localhost:3100/ready` |

Секреты читаются через Docker Compose secrets из `./secrets/*`.
Никакие реальные T-Bank tokens не должны попадать в Git.

## Трехслойная модель

Система логирования и аналитики делится на три слоя. Они не заменяют друг
друга: каждый отвечает за свой тип правды.

| Слой | Что хранит | Основное хранилище | Потребители | Не использовать для |
| --- | --- | --- | --- | --- |
| `runtime logs` | Технические JSON logs процесса: ошибки, retries, reconnects, latency, service health, broker headers. | stdout/stderr -> Fluent Bit -> Loki | оператор, Grafana/Loki dashboards, incident runbooks | восстановление доменной истории сделки как единственный источник |
| `decision journal` | Нормализованные доменные факты: candidates, stage results, blockers, intents, broker states, fills, session snapshots. | Postgres | report-worker, API read models, calibration scripts, counterfactual analyzer | отладочные stack traces и шумные transport logs |
| `analytics mart` | Агрегаты и витрины: hourly_report, daily_report, counterfactual_result, funnels, blocker rankings, trend classification. | Postgres | frontend Reports, API, calibration notebooks/scripts | raw trading decisions без ссылок на первичные факты |

Минимальный принцип: техлог помогает понять, почему сервис вел себя так, а
decision journal помогает воспроизвести, почему робот принял или отклонил
конкретное торговое решение.

## Два контура хранения

### Loki: technical logs

Путь:

```text
Python logging JSON -> stdout/stderr -> Docker fluentd driver -> Fluent Bit -> Loki
```

Контур Loki хранит:

- service lifecycle logs;
- broker retry/backoff/deadline diagnostics;
- stream reconnects and gap recovery messages;
- captured broker metadata headers, включая `x-tracking-id`;
- API request diagnostics;
- report-worker task lifecycle diagnostics.

Loki labels должны оставаться низкокардинальными:

- `service`;
- `container`;
- `environment`;
- `runtime_mode`;
- `level`;
- `event_type`, если cardinality контролируемая.

Высококардинальные поля вроде `candidate_id`, `order_intent_id`,
`request_order_id`, `exchange_order_id`, `tracking_id` остаются JSON fields, а
не Loki labels.

### Postgres: domain facts and reports

Контур Postgres хранит:

- состояние сессий и micro-sessions;
- доменные события strategy/risk/execution;
- order lifecycle и fills;
- агрегированные hourly/daily reports;
- counterfactual results;
- audit events.

Технические raw logs не являются primary analytics source. Если поле важно для
аналитики, оно должно попасть в нормализованную таблицу или в управляемый JSONB
payload доменного события, а не оставаться только в free-text log message.

## Exchange sessions

Канонические `session_type`:

| Значение | Смысл |
| --- | --- |
| `weekend` | Биржевая торговля недоступна или используется weekend broker mode. |
| `weekday_morning` | Утренняя торговая сессия. Должна закрываться на границе перехода к main session. |
| `weekday_main` | Основная дневная сессия. Не смешивается с morning/evening в отчетах. |
| `weekday_evening` | Вечерняя сессия. Имеет отдельные отчеты и blockers. |

`calendar_date` и `trading_date` хранятся отдельно. Это нужно для вечерних
режимов, переносов и аналитики, где календарная дата события может отличаться
от торгового дня.

## Exchange phases

Целевая аналитическая каноника:

| Phase | Смысл |
| --- | --- |
| `opening_auction` | Открывающий аукцион, entry permissions ограничены политикой сессии. |
| `continuous` | Непрерывная торговля; только здесь стартуют новые hourly micro-sessions. |
| `closing_auction` | Закрывающий аукцион, новые entries обычно запрещены. |
| `break` | Перерыв или пауза между режимами торгов. |
| `discrete_auction` | Дискретный аукцион или аналогичный broker phase, требующий отдельной политики. |
| `session_closed` | Сессия закрыта, разрешены только безопасные reconciliation/read operations. |

Текущее состояние кода использует `SessionPhase` из
`packages/common/src/trading_common/enums.py`:

| Целевая каноника | Текущий enum | Статус |
| --- | --- | --- |
| `opening_auction` | `opening_auction` | совпадает |
| `continuous` | `continuous_trading` | требуется словарь совместимости или миграция enum |
| `closing_auction` | `closing_auction` | совпадает |
| `break` | `break` | совпадает |
| `discrete_auction` | `dealer_mode` | требует уточнения mapping по T-Bank trading status |
| `session_closed` | `closed` | требуется словарь совместимости или миграция enum |

До миграции кода отчеты и UI должны уметь читать текущие значения, но новые
документы используют целевую канонику из этого раздела.

## Hourly micro-session rollover

`trade-core` не должен физически перезапускаться по часу. Hourly
micro-session является логической единицей внутри биржевой сессии.

Ожидаемый flow:

1. `SessionManager` определяет `session_type`, `session_phase`,
   `calendar_date`, `trading_date` из заранее полученного `TradingSchedules` и
   текущего broker trading status.
2. `HourlyMicroSessionManager` открывает новую micro-session только в фазе
   `continuous`.
3. За configurable 60-90 секунд до часовой границы включается freeze new
   entries. Управление уже открытыми заявками остается за execution policy.
4. На границе часа пишется snapshot состояния, закрывается старая
   micro-session и публикуется `report_requested`.
5. `report-worker` строит `hourly_report` через Celery + Redis вне процесса
   `api`.
6. В той же долгоживущей памяти `trade-core` открывает следующую micro-session,
   если exchange session и phase это разрешают.
7. На жестких границах `weekday_morning -> weekday_main` и
   `weekday_main -> weekday_evening` текущая micro-session закрывается даже если
   старт была не ровно в начале часа.

Цель такого разбиения: не смешивать morning/main/evening логи и доменные факты,
а также не строить тяжелый дневной отчет из одного огромного runtime-сегмента.

## Event and analytics flow

```text
T-Bank gRPC / streams
        |
        v
trade-core
  - session manager
  - market data pipeline
  - strategy/risk/execution
        |
        +--> runtime JSON logs --> Fluent Bit --> Loki
        +--> Prometheus metrics --> Prometheus --> Grafana
        +--> decision journal/domain facts --> Postgres
                                             |
                                             v
                                  report-worker via Celery/Redis
                                             |
                                             v
                                  hourly/daily/counterfactual mart
                                             |
                                             v
                                   FastAPI read models + Vue UI
```

FastAPI не считает тяжелые отчеты inline. Он читает read models и ставит
задачи в Celery, например `report_worker.rebuild_reports_for_date`.

## Observability boundaries

Runtime logs должны быть полезны для incident response, но не должны становиться
скрытой БД торговых решений. Decision journal должен быть достаточно полным,
чтобы ответить:

- почему candidate был создан;
- какие gates он прошел;
- какой blocker стал финальным;
- почему order был создан, отменен или отклонен;
- какой broker state был получен;
- какой tracking id вернул T-Bank;
- что случилось бы с blocked/cancelled candidate через 5/10/15 минут.

## Open questions / TODO

- Решить, мигрируем ли `continuous_trading -> continuous` и `closed ->
  session_closed` в enum/schema или оставляем compatibility layer.
- Уточнить mapping `dealer_mode` к целевому `discrete_auction` на фактических
  ответах T-Bank `GetTradingStatus`/Info stream.
- Решить, нужна ли отдельная таблица `micro_session` или достаточно текущего
  `session_run.micro_session_id` как canonical storage.
- Добавить явные `candidate_stage_result`, `order_state_event` и
  `market_context_snapshot` как таблицы или materialized/read views.
- Зафиксировать retention policy для Loki и event-heavy Postgres partitions.
- Если появится APScheduler, нужен отдельный ADR: сейчас scheduling heavy
  reports должен оставаться event/Celery based, без distant future ETA как
  основной модели.

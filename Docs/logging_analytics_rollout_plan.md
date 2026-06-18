# Logging & Analytics Rollout Plan

План описывает, как довести logging/analytics spine до production-like
состояния без изменения торговой логики и без физического hourly restart
`trade-core`.

## Принципы rollout

- Сначала документация и словарь, затем схема и код.
- Любое новое поле для аналитики должно быть machine-readable.
- Technical logs остаются в Loki, доменные факты и отчеты остаются в Postgres.
- `api` не считает тяжелые отчеты inline.
- `report-worker` выполняет тяжелую аналитику через Celery + Redis.
- Все изменения должны быть совместимы с текущими launch modes:
  `historical_replay`, `sandbox`, `shadow`, `production`.
- Production mode не включается по умолчанию.

## Текущее состояние

Уже есть:

- `docker-compose.yml` со стеком Postgres, Redis, Loki, Fluent Bit,
  Prometheus, Grafana, `trade-core`, `api`, `report-worker`, `frontend`;
- Docker Compose secrets для T-Bank, Postgres и Grafana;
- SQLAlchemy/Alembic schema в `packages/common`;
- event-heavy таблицы и report tables, включая `blocker_event`,
  `fill_event`, `strategy_state_event`, `counterfactual_result`,
  `hourly_report`, `daily_report`;
- structured JSON logging и Prometheus metrics registry;
- Grafana provisioning для health, market data, execution, risk/blockers,
  session rollovers;
- Celery tasks:
  - `report_worker.build_hourly_report`;
  - `report_worker.build_daily_report`;
  - `report_worker.rebuild_reports_for_date`;
  - `report_worker.run_counterfactual_analysis_for_date`;
- CLI:
  - `scripts/run_hourly_report.py`;
  - `scripts/run_daily_report.py`;
  - `scripts/run_counterfactual.py`;
- FastAPI BFF endpoints и live WebSocket channels с production-like ticket auth;
- Vue 3 dark-theme dashboard and reports UI;
- replay harness, sandbox smoke, shadow mode runbooks, CI.

`APScheduler` не найден в текущей структуре. Это нормально для текущей
архитектуры: hourly reports должны стартовать от закрытия micro-session и
Celery task enqueue, а не от дальних scheduled jobs внутри API.

## Phase 1. Vocabulary alignment

Цель: выровнять язык runtime logs, decision journal, reports и UI.

Работы:

- Принять целевые phases:
  `opening_auction`, `continuous`, `closing_auction`, `break`,
  `discrete_auction`, `session_closed`.
- Описать compatibility mapping к текущим enum values:
  `continuous_trading`, `dealer_mode`, `closed`.
- Зафиксировать mandatory entities и correlation IDs в документации.
- Проверить, что все новые docs discoverable из `README.md`.

Статус: выполняется этим docs-only шагом.

## Phase 2. Non-breaking schema/read-model additions

Цель: добавить недостающие decision journal сущности без изменения стратегии.

Кандидаты на миграции или read/materialized views:

| Entity | Рекомендуемый путь |
| --- | --- |
| `micro_session` | Сначала view над `session_run`, затем отдельная таблица только если появятся независимые атрибуты. |
| `candidate_stage_result` | Новая append-only таблица для passed/failed stage results. |
| `order_state_event` | Новая append-only таблица для broker/order lifecycle transitions. |
| `market_context_snapshot` | Нормализованная таблица или view, объединяющая spread, mid, book summary, freshness, quality. |

Acceptance criteria:

- миграции idempotent и покрыты tests;
- текущие отчеты продолжают работать;
- новые таблицы не требуют изменения signal generation logic;
- payload JSONB используется только для extended context.

## Phase 3. Writers in trade-core

Цель: писать полный decision journal там, где уже принимаются решения.

Работы:

- При создании candidate писать `signal_candidate_created`.
- После каждого stage писать `candidate_stage_result_recorded`.
- При финальном отказе писать `blocker_triggered` с `final=true`.
- Перед broker/pseudo broker call писать `order_intent_created`.
- При broker state updates писать `order_state_event`.
- При market degradation писать `market_context_snapshot_written`.
- На micro-session rollover писать `session_snapshot_written`,
  `micro_session_closed`, `report_requested`, `micro_session_opened`.

Не менять:

- условия входа/выхода стратегии;
- risk thresholds;
- broker adapter behavior;
- физический lifecycle контейнера `trade-core`.

## Phase 4. Analytics mart expansion

Цель: сделать hourly/daily/counterfactual reports полностью объяснимыми.

Работы в `report-worker`:

- расширить hourly report расчет stage funnel:
  candidates -> blockers -> approved -> posted -> filled -> profitable;
- добавить breakdown по `session_type`, `instrument_id`, `timeframe`;
- включить latency distributions из доменных facts/metrics snapshots;
- строить blocker ranking по `candidate_stage_result` и `blocker_event`;
- строить missed opportunity summary из `counterfactual_result`;
- сохранять algorithm version и assumptions в report payload.

Reports должны оставаться buildable через:

```bash
python scripts/run_hourly_report.py --micro-session-id <id> --strategy-id baseline
python scripts/run_daily_report.py --trading-date <YYYY-MM-DD> --strategy-id baseline
python scripts/run_counterfactual.py --trading-date <YYYY-MM-DD> --strategy-id baseline
```

## Phase 5. API and frontend exposure

Цель: сделать причины решений видимыми оператору.

FastAPI read models:

- current session and micro-session;
- current candidate and final blocker;
- market context snapshot;
- open orders and order state events;
- hourly/daily reports;
- counterfactual results;
- blocker ranking and missed opportunity tables.

Vue UI:

- Live Dashboard показывает active session, phase, micro-session countdown,
  candidate, blocker, market quality, order state.
- Reports показывает daily/hourly filters по date, instrument, timeframe,
  session_type, blocker_code.
- Diagnostics показывает Loki/Grafana navigation hints and degraded flags,
  но не превращает technical logs в analytics source.

## Phase 6. Operational validation

Цель: controlled launch без сюрпризов.

Проверки:

```bash
docker compose up -d --build
docker compose ps
python -m alembic upgrade head
python scripts/check.py
make replay-smoke
make sandbox-smoke
```

Health checks:

- `http://localhost:8001/health` for `trade-core`;
- `http://localhost:8000/health` for `api`;
- `http://localhost:8002/health` for `report-worker`;
- `http://localhost:9090/-/healthy` for Prometheus;
- `http://localhost:3000/api/health` for Grafana;
- `http://localhost:3100/ready` for Loki.

Dashboards:

- broker/API health;
- market data health;
- order execution quality;
- risk/blockers;
- session rollovers;
- report-worker backlog.

## Как будут считаться дневной тренд, блокеры и контрфакты

### Daily trend

Algorithm v1:

- input: closed candles for `trading_date`;
- per instrument: `(last_close - first_open) / first_open * 10000`;
- aggregate: equal-weight average return in bps;
- `long_bias` if average `>= +25 bps`;
- `short_bias` if average `<= -25 bps`;
- otherwise `mixed_flat`;
- output stored in `daily_report.report_payload.market_regime` with
  `average_return_bps`, `instrument_returns_bps`, `algorithm_version`.

### Blockers

Algorithm v1:

- input: `candidate_stage_result` target table plus existing `blocker_event`;
- group by `trading_date`, `session_type`, `instrument_id`, `timeframe`,
  `strategy_id`, `blocker_code`;
- count failed stages and final blockers separately;
- expose final blocker ranking in reports;
- expose non-final failed gates for calibration, because repeated near-failures
  can explain degraded strategy quality even when trade was eventually allowed.

### Counterfactuals

Algorithm v1:

- sources: blocked candidates and cancelled order intents;
- windows: 5, 10, 15 minutes after source event;
- price path: closed candles or replay market events converted to deterministic
  `PricePathPoint`;
- metrics: MFE bps, MAE bps, close return bps, theoretical PnL bps/RUB;
- TP/SL: compare MFE/MAE with assumptions;
- costs: subtract fees and slippage assumptions;
- output: `counterfactual_result.result_payload` plus indexed summary fields for
  filters and reports.

## Open questions / TODO

- Decide if phase enum migration should be done in-place or via read-model
  aliases first.
- Confirm T-Bank status mapping for `discrete_auction`.
- Decide if `tracking_id` should be promoted from log context/payload into
  explicit DB columns for all broker events.
- Define retention and partition maintenance for high-volume journal tables.
- Decide whether `micro_session` remains a logical entity or becomes a physical
  table.
- Add alert thresholds for report backlog, missing hourly report and stale
  market context.
- If APScheduler is introduced later, document why it is needed and ensure it
  does not replace micro-session event driven reporting.

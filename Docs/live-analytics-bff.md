# Live analytics BFF and frontend

Этот документ фиксирует слой FastAPI + Vue 3 для live view и аналитики логирования.
Он дополняет `Docs/api-contract.md`, `Docs/frontend-dashboard-spec.md` и
`Docs/logging-analytics-spec.md`.

## Scope

Слой не меняет торговую стратегию и не выполняет тяжелые расчеты в процессе `api`.
FastAPI читает read models и доменные факты из PostgreSQL, а пересчет отчетов ставит
в `report-worker` через Celery + Redis.

## REST endpoints

Отчеты:

- `GET /reports/hourly`
- `GET /reports/daily`
- `GET /reports/counterfactual`
- `POST /reports/rebuild/run`
- `GET /reports/jobs/{job_id}`

Аналитика логирования:

- `GET /analytics/blockers`
- `GET /analytics/candidate-funnel`
- `GET /analytics/canceled-orders`

Общие фильтры:

- `trading_date`
- `instrument_id`
- `timeframe`
- `session_type`
- `blocker_code`
- `strategy_id`
- `strategy_version`

`POST /reports/rebuild/run` принимает `scope=daily` или `scope=hourly`.
Для `scope=hourly` обязателен `micro_session_id`.

## WebSocket snapshots

`/ws/dashboard` отдает live snapshot:

- `robot_status`
- `market`
- `positions`
- `open_orders`
- `signals`
- `blockers`
- `candidate_funnel`

`/ws/reports` отдает report/analytics snapshot:

- `hourly`
- `daily`
- `blockers`
- `candidate_funnel`
- `counterfactual`
- `canceled_orders`

WebSocket endpoints держат соединение открытым. При подключении BFF отправляет
первый snapshot, затем повторные snapshot/update сообщения по configurable interval
и heartbeat. Если клиент не успевает принимать сообщения, BFF закрывает соединение
как backpressure protection; frontend должен перейти в degraded/reconnect состояние.

Control plane команды идут через `robot_command`: API пишет durable command и audit,
а `trade-core` применяет ее в runtime loop без физического рестарта процесса.

## Analytics read models

`/analytics/blockers` строит lightweight ranking по `blocker_event` и
`counterfactual_result`:

- `blocker_code`
- `blocker_family`
- `count`
- `terminal_count`
- `candidate_count`
- `measured_value_avg`
- `threshold_value_avg`
- `missed_pnl_gross`
- `missed_pnl_net`
- `avoided_loss`
- `false_positive_rate`
- `explanation_payload`

`/analytics/candidate-funnel` показывает путь:

```text
created -> passed_gates -> blocked -> order_intent -> posted -> filled -> exited
```

`/analytics/canceled-orders` группирует отмены по `cancel_reason_code` и добавляет
counterfactual итоги по горизонтам `+5m`, `+10m`, `+15m`.

## Frontend panels

`LiveDashboardView` показывает:

- `session_type`, `session_phase`, `broker_trading_status`;
- `strategy_state`;
- balance, positions, active orders;
- spread, mid price, market quality, top of book;
- order book summary and market tape when read model is available;
- last signal candidate and final blocker explanation;
- stream health / reconnect status.

`ReportsView` показывает:

- filters by date, instrument, timeframe, session type, blocker code, strategy id/version;
- daily regime card;
- hourly micro-session timeline;
- candidate funnel;
- blocker ranking;
- canceled order diagnostics;
- counterfactual horizons for `+5m`, `+10m`, `+15m`;
- table `Что нас тормозит` with machine-readable blocker codes and explainability payload.

## Operational constraints

- Technical logs remain stdout -> Fluent Bit -> Loki.
- Domain facts and reports remain in PostgreSQL.
- Heavy analytics remains in `report-worker`; API only enqueues Celery jobs and reads results.
- UI labels can be human-readable, but reason fields must remain machine-readable:
  `blocker_code`, `cancel_reason_code`, `request_order_id`, `exchange_order_id`,
  `candidate_id`, `order_intent_id`, `tracking_id`.

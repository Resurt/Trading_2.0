# API Contract

`api` - это FastAPI BFF для frontend. Он предоставляет REST для команд, снимков состояния, конфигурации и отчетов, а WebSocket - для live feed.

Тяжелые отчеты не строятся внутри API handlers. API только ставит задачи в `report-worker` и возвращает статус.

Реализация шага 10 находится в `apps/api/src/trading_api`. Контракт оформлен через Pydantic schemas, поэтому `/openapi.json` и `/docs` являются машинно-читаемым источником для frontend.

## Auth and control plane

API использует auth abstraction. В local-dev, `historical_replay`, `sandbox` и `shadow`
допустим dev provider через заголовки `X-API-Role` и `X-API-Actor`. В `production`
dev provider запрещен на startup: нужно задать `TRADING_AUTH_MODE=static_bearer`
и токены `TRADING_API_OBSERVER_TOKEN`, `TRADING_API_OPERATOR_TOKEN` или
`TRADING_API_ADMIN_TOKEN` через env/secret file.

Для browser WebSocket в production-like режимах используется короткоживущий ticket:
клиент вызывает `POST /auth/ws-ticket` с bearer auth, затем подключается к
`/ws/...?...ticket=...`. Обычный `new WebSocket()` не передает custom
`Authorization` header, поэтому X-API-Role-only WebSocket доступен только вне
production.

Разрешенные роли:

- `observer` - чтение состояния, отчетов и конфигурации;
- `operator` - чтение плюс команды управления и запуск отчетов;
- `admin` - те же права, что `operator`, с будущим расширением под администрирование.

Если dev-заголовок не передан, роль считается `observer`. Команды `POST /robot/start`,
`POST /robot/stop`, `POST /robot/pause`, `POST /robot/resume`,
`POST /robot/emergency-stop`, `POST /reports/daily/run` и `PUT /config/strategy`
требуют `operator` или `admin`.

Команды управления не меняют только in-memory state API. BFF пишет строку
`robot_command` со статусом `requested` и audit row в `audit_event`. `trade-core`
читает команды, переводит их в `accepted/applied/rejected/failed` и применяет
safe runtime policy без физического рестарта процесса.

`POST /robot/start` is guarded by session preflight. API runs `/session/preflight`
for the configured data-only universe before persisting a requested start. If
`market_open=false`, API writes a rejected `robot_command`/audit event with the
preflight `reason_code` and returns HTTP 200 with `accepted=false`,
`status=rejected`, and `preflight_result`. The rejected command does not start
runtime streams and does not enable live trading.

`RobotCommandResponse` includes:

- `command_id`;
- `command` and `command_type`;
- `status`;
- `reason_code`;
- `message`;
- `accepted`;
- optional `preflight_result`.

Для локального Vue frontend BFF разрешает CORS origins из `CORS_ALLOW_ORIGINS`.
Значение по умолчанию: `http://localhost:5173,http://127.0.0.1:5173`.

## Read model policy

API читает данные через `BffReadService`, а не напрямую из произвольных таблиц в route handlers.

Основные источники:

- `session_run` - текущая биржевая сессия и `micro_session_id`;
- `position_snapshot` - последние позиции;
- `broker_order` + `order_intent` - открытые заявки и reason codes;
- `signal_candidate` + `blocker_event` - текущие сигналы и финальные blockers;
- `order_book_summary` - market overview без хранения полного стакана на каждый тик;
- `hourly_report`, `daily_report`, `counterfactual_result` - готовые отчеты и аналитика;
- `strategy_config` - версионированная конфигурация стратегии.

## REST endpoints

| Method | Path | Назначение |
| --- | --- | --- |
| `POST` | `/robot/start` | Запросить запуск робота в настроенном режиме. |
| `POST` | `/robot/stop` | Запросить controlled stop. |
| `POST` | `/robot/pause` | Запретить новые entries без остановки процесса. |
| `POST` | `/robot/resume` | Возобновить прием новых entries после pause/stop. |
| `POST` | `/robot/emergency-stop` | Немедленно перевести runtime в emergency stopped mode. |
| `GET` | `/robot/status` | Получить текущее состояние робота. |
| `GET` | `/session/current` | Получить текущую биржевую сессию и micro-session. |
| `GET` | `/session/preflight` | Readonly session/calendar preflight for the target universe before live data-only start. |
| `GET` | `/positions` | Получить текущие позиции. |
| `GET` | `/portfolio/summary` | Latest portfolio/balance read model with masked account id or degraded reason. |
| `POST` | `/portfolio/refresh` | Operator/admin readonly broker balance refresh via get_accounts/get_portfolio/get_positions. |
| `GET` | `/orders/open` | Получить открытые ордера. |
| `GET` | `/signals/current` | Получить текущие candidates и blockers. |
| `GET` | `/market/overview` | Получить market overview по включенным инструментам. |
| `GET` | `/reports/hourly` | Получить hourly reports по фильтрам. |
| `GET` | `/reports/daily` | Получить daily reports по фильтрам. |
| `POST` | `/reports/daily/run` | Поставить rebuild daily report в `report-worker`. |
| `GET` | `/reports/counterfactual` | Получить counterfactual analytics. |
| `GET` | `/config/strategy` | Прочитать strategy config. |
| `PUT` | `/config/strategy` | Обновить strategy config через audited change. |

## `/robot/status`

Реализованные поля:

- balance;
- active instruments;
- active timeframes;
- strategy state;
- `session_type`;
- `session_phase`;
- `broker_trading_status`;
- `micro_session_id`;
- open orders count;
- active positions count;
- degraded flags;
- robot control state.

Пример:

```json
{
  "balance": {
    "currency": "RUB",
    "available": "0",
    "blocked": "0"
  },
  "active_instruments": ["MOEX:SBER", "MOEX:GAZP"],
  "active_timeframes": ["5m", "10m", "15m"],
  "strategy_state": "wait",
  "session_type": "weekday_main",
  "session_phase": "continuous_trading",
  "broker_trading_status": "normal_trading",
  "micro_session_id": "2026-06-13:weekday_main:1000",
  "open_orders_count": 1,
  "active_positions_count": 1,
  "degraded_flags": ["balance_unavailable"],
  "robot_control_state": "start_requested"
}
```

`balance_unavailable` сейчас ожидаемый degraded flag: баланс еще не подключен к broker/account read model.

## `/market/overview`

`/market/overview` отдает список инструментов с полями:

- spread;
- mid price;
- market quality;
- best bid/ask;
- recent market trades;
- lightweight order book summary.

Полноразмерный стакан не пишется в PostgreSQL на каждый тик. Для BFF используется подготовленный агрегат `order_book_summary`.

## `/reports/daily/run`

Endpoint не считает daily report внутри FastAPI. Он ставит Celery task `report_worker.rebuild_reports_for_date` через Redis и возвращает job status:

```json
{
  "job_id": "celery-task-id",
  "task_name": "report_worker.rebuild_reports_for_date",
  "status": "queued",
  "payload": {
    "trading_date": "2026-06-13",
    "strategy_id": "baseline",
    "include_counterfactual": true
  }
}
```

## WebSocket channels

| Path | Назначение |
| --- | --- |
| `/ws/dashboard` | Общий live feed для dashboard. |
| `/ws/orders` | Order lifecycle updates. |
| `/ws/market` | Market overview, top of book, candles, market quality. |
| `/ws/reports` | Статусы report tasks и новые reports. |

При подключении каждый канал отправляет первый `*.snapshot`, затем продолжает
слать snapshot/update сообщения с sequence в payload и heartbeat каждые 10
итераций. Соединение не закрывается после первого сообщения; при backpressure
BFF закрывает канал кодом `1011`, а при невалидной авторизации - `1008`.

## WebSocket message envelope

```json
{
  "message_id": "uuid",
  "ts_utc": "2026-06-13T12:00:00Z",
  "type": "dashboard.snapshot",
  "run_id": "uuid",
  "micro_session_id": "2026-06-13T07",
  "payload": {}
}
```

`message_id` и timestamps обязательны для deduplication и traceability.

Пример сообщения `/ws/dashboard`:

```json
{
  "message_id": "7e16a7c7-8e87-4c9d-97f7-71b49db9cc69",
  "ts_utc": "2026-06-13T07:10:00Z",
  "type": "dashboard.snapshot",
  "run_id": null,
  "micro_session_id": "2026-06-13:weekday_main:1000",
  "payload": {
    "data": {
      "robot_status": {
        "session_type": "weekday_main",
        "session_phase": "continuous_trading",
        "strategy_state": "wait",
        "open_orders_count": 1,
        "active_positions_count": 1,
        "degraded_flags": ["balance_unavailable"]
      },
      "market": {
        "instruments": []
      },
      "open_orders": [],
      "signals": []
    }
  }
}
```

`payload.data` содержит снимок read model на момент отправки, а `payload.sequence`
позволяет frontend обнаруживать пропуски/переподключения.

## Current endpoint groups

OpenAPI (`GET /openapi.json`) is the machine-readable source of truth. After route changes, rebuild API/frontend containers and run:

```bash
python scripts/run_api_route_smoke.py --json-output
```

Historical data:

- `GET /historical/quality`
- `POST /historical/backfill/run`
- `POST /historical/replay/run`

Corporate actions and dividend sync:

- `GET /corporate-actions`
- `POST /corporate-actions/import`
- `GET /dividends/sync/status`
- `POST /dividends/sync/run`

Instrument registry:

- `GET /instruments/registry`
- `POST /instruments/resolve`

Data-only shadow microstructure:

- `GET /market/microstructure/latest`
- `GET /market/microstructure/summary`
- `GET /runtime/data-shadow/status`

Intraday analytics:

- `GET /analytics/intraday/today`
- `GET /analytics/intraday`
- `GET /analytics/intraday/session`
- `GET /analytics/intraday/micro-session/{micro_session_id}`

Calibration observatory:

- `GET /calibration/observatory/status`
- `POST /calibration/observatory/run`
- `GET /calibration/diagnostics`
- `GET /calibration/diagnostics/{diagnostic_run_id}`
- `GET /calibration/rolling-performance`
- `GET /calibration/regime`
- `GET /calibration/config-candidates`
- `GET /calibration/config-candidates/{candidate_config_id}`
- `POST /calibration/config-candidates/{candidate_config_id}/approve-for-shadow`
- `POST /calibration/config-candidates/{candidate_config_id}/reject`

Portfolio and balance:

- `GET /portfolio/summary`
- `POST /portfolio/refresh`
- `GET /robot/status`
- `GET /session/preflight`

`/robot/status.balance` includes total portfolio value, available cash, blocked cash, expected yield, free collateral when available, masked account id, freshness and degraded reason. Full account ids and secrets must not be exposed in balance payloads.

`POST /portfolio/refresh` is operator/admin only and readonly. It must not call
`PostOrder` or `CancelOrder`; it stores a `broker_balance` payload in the existing
position snapshot read model. `GET /portfolio/summary` returns the latest stored
snapshot. If broker balance is unavailable, responses keep the balance object visible
with `balance_degraded=true` and `balance_degraded_reason_code`.

`GET /session/preflight` accepts optional `instruments=SBER,GAZP` and
`mode=data_shadow`. The response includes `market_open`, `market_closed_expected`,
`now_msk`, `trading_date`, `calendar_date`, `session_type`, `session_phase`,
`broker_trading_status`, `api_trade_available`, `next_session_at`,
`next_session_type`, `reason_code`, `source`, `instruments_checked` and
`per_instrument_status`.

Anchor: data-only shadow endpoints are listed above and remain observer/read-only APIs.

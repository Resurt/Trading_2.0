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

`POST /robot/start` is guarded by a fast calendar/session preflight. For operator
UI responsiveness it uses the same fallback calendar path as
`GET /session/preflight?broker_checks=false`; full broker schedule and per-instrument
status checks remain mandatory for data-only smoke/readiness scripts. If
`market_open=false`, API writes a rejected `robot_command`/audit event with the
preflight `reason_code` and returns HTTP 200 with `accepted=false`,
`status=rejected`, and `preflight_result`. The rejected command does not start
runtime streams and does not enable live trading.

The API keeps a short server-side preflight cache controlled by
`TRADING_SESSION_PREFLIGHT_CACHE_TTL_SECONDS`, default 30 seconds. This lets
`POST /robot/start` reuse the fresh `GET /session/preflight` result that the
dashboard just received instead of issuing a second slow broker status pass.
Incident triage can bypass this cache with
`GET /session/preflight?...&cache=false`; the response includes `cache_hit` and
`cache_key` so CLI/API comparisons are explicit.
Fresh broker preflight is bounded by `TRADING_SESSION_PREFLIGHT_TIMEOUT_SECONDS`,
default 30 seconds in code and 45 seconds in Docker Compose, because
full-universe readonly broker status checks can take longer than a lightweight
dashboard calendar pass, especially while the dashboard live feed is polling
readonly quotes.

`GET /session/preflight` uses the same `TradingSessionPreflightService` rules as
the data-only smoke CLI. The response includes schedule/status diagnostics:

- `source`, `schedule_source`, `status_source`;
- `schedule_error_code`, `schedule_error_message`;
- `market_window_open`, `data_only_collection_allowed`, `trading_allowed`,
  `blocking_layer`, `broker_schedule_windows_count`, and `fallback_reason`;
- `status_success_count`, `status_error_count`;
- `market_data_probe_success_count`, `market_data_probe_error_count`, and
  `market_data_probe`;
- `fallback_used`, `cache_hit`, `cache_key`;
- `requested_instruments`, `working_instruments`, `blocked_instruments`;
- per-instrument `broker_status`, `api_trade_available`, `status_source`,
  `status_error_code`, `status_error_message`, `collection_allowed`, and
  `blocked_reason`.

When T-Bank `TradingSchedules` returns `INVALID_ARGUMENT 30003`, the API records
`schedule_source=tbank_error`, `schedule_error_code=30003`, and uses fallback
schedule rules plus per-instrument broker trading status. A fallback schedule
alone must not mark the market open when every broker status is unavailable.
The known 30003 trigger is a `TradingSchedules.from_` value earlier than the
current broker date/time after timezone conversion; normal preflight requests
schedules from the current preflight timestamp forward.

T-Bank `TradingSchedules` can also return a valid payload that omits the active
local MOEX window while `GetTradingStatus` reports exchange `normal_trading` or
readonly market data probe calls succeed for the requested instruments. In that
case preflight treats the schedule as incomplete, uses the local MOEX fallback
window, and returns `source=broker_status_fallback_time_rules`,
`schedule_source=broker_trading_schedules_status_fallback`,
`fallback_used=true`, and warnings including
`broker_schedule_missing_active_window` and
`broker_status_open_schedule_closed` or `market_data_probe_used_without_status`.
If every status call and every readonly market data probe fails, Start remains
blocked with `reason_code=broker_status_and_market_data_unavailable`, not with a
false closed-window reason.

In data-only mode `trading_allowed=false` even when
`data_only_collection_allowed=true`. Start may launch only persistent market-data
logging streams plus a bounded readonly `GetOrderBook` polling fallback when
streams are silent. Order APIs remain disabled.

Data-only Start does not perform runtime position snapshots. Account-level
`GetPositions`/`GetPortfolio` reads are reserved for explicit readonly balance
diagnostics (`POST /portfolio/refresh` or the broker balance script), not for
collector startup or micro-session rollover.

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

### Market overview and readonly broker refresh

`GET /market/overview` is a cheap read-model endpoint backed by the Dashboard Live
Feed cache first and local DB fallbacks second. It must not perform heavy
all-instrument order-book refreshes. It returns all core-universe instruments with
explicit `quote_source`, `venue_type`, freshness, spread units, and calibration
eligibility.

Dashboard Live Feed is the readonly display path for the operator terminal. It can
call T-Bank `GetLastPrices` for the quote board and `GetOrderBook`/last trades for
only the selected instrument. It is not data-only collection and must not write
calibration rows.

`POST /market/quotes/refresh` is an explicit readonly broker refresh. It may call
T-Bank `GetLastPrices`, `GetOrderBook`, and `GetLastTrades`, but it must never
call `PostOrder` or `CancelOrder`. Refresh calls are guarded by a server-side
single-flight lock and short broker deadlines; overlapping refreshes return the
latest cached/local overview instead of piling up broker requests.

Broker OTC/indicative quotes and trades may be displayed on the dashboard, but
they are tagged with `venue_type=broker_otc` or `broker_indicative` and
`quote_allowed_for_data_collection=false`. They are excluded from calibration by
default. `display_market_quality_score` describes the visible book only;
`calibration_market_quality_score` is zero/not applicable outside an official
exchange session.

| Method | Path | Назначение |
| --- | --- | --- |
| `POST` | `/robot/start` | Запросить запуск робота в настроенном режиме. |
| `POST` | `/robot/stop` | Запросить controlled stop. |
| `POST` | `/robot/pause` | Запретить новые entries без остановки процесса. |
| `POST` | `/robot/resume` | Возобновить прием новых entries после pause/stop. |
| `POST` | `/robot/emergency-stop` | Немедленно перевести runtime в emergency stopped mode. |
| `GET` | `/robot/status` | Получить текущее состояние робота. |
| `GET` | `/dashboard/state` | Fast Live Dashboard bootstrap: robot status, lightweight calendar preflight, market overview, positions, open orders and signals. Heavy report/candidate analytics are intentionally excluded. |
| `GET` | `/dashboard/market-feed/status` | Readonly Dashboard Live Feed state: cache freshness, session/venue, selected instrument, quote/order-book/trade-tape availability, warnings and errors. |
| `GET` | `/dashboard/market-feed/snapshot` | Readonly live-display snapshot for the dashboard. Uses `GetLastPrices` for the quote board and `GetOrderBook`/last trades only for the selected instrument. Does not start data-only collection and does not write calibration logs. |
| `POST` | `/dashboard/market-feed/refresh` | Readonly manual refresh of the dashboard feed cache. Observer/operator/admin only; no order placement/cancel calls and no trading entities. |
| `GET` | `/session/current` | Получить текущую биржевую сессию и micro-session. |
| `GET` | `/session/preflight` | Readonly session/calendar preflight for the target universe before live data-only start. `broker_checks=false` returns the fast dashboard calendar path; default `true` runs broker schedule/status checks. |
| `GET` | `/positions` | Получить текущие позиции. |
| `GET` | `/portfolio/summary` | Latest portfolio/balance read model with masked account id or degraded reason. |
| `POST` | `/portfolio/refresh` | Operator/admin readonly broker balance refresh via get_accounts/get_portfolio/get_positions. |
| `GET` | `/orders/open` | Получить открытые ордера. |
| `GET` | `/signals/current` | Получить текущие candidates и blockers. |
| `GET` | `/market/overview` | Cheap market overview read-model backed by the Dashboard Live Feed cache. Returns one row per core universe instrument and avoids heavy all-instrument order-book calls. Use `include_details=false` for quote board payloads. |
| `GET` | `/market/instruments/{instrument_id}/details` | Selected-instrument details from the Dashboard Live Feed: quote, top of book, order-book levels, explicit trade-tape status and source/freshness diagnostics for one instrument. |
| `POST` | `/market/quotes/refresh` | Explicit readonly quote/order-book refresh via T-Invest `GetLastPrices`/`GetOrderBook`; does not place or cancel orders. If the readonly gateway is unavailable, it returns the local overview fallback instead of blocking the dashboard. |
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
- session reconciliation fields: `session_source`, `session_stale`,
  `session_stale_reason`.

`GET /session/current` and `GET /robot/status` reconcile the latest runtime
`session_run` snapshot with a fresh preflight-derived session state for
operator-facing reads. Fresh preflight is authoritative when the runtime snapshot
is missing or stale. Responses expose `source`/`session_source` as
`runtime_session_snapshot`, `fresh_preflight`, or `stale_runtime_snapshot`, plus
`stale=true` and `stale_reason=runtime_snapshot_mismatch` when a previous
runtime session is not current. A closed preflight must not be rendered as
`continuous_trading` without an explicit stale warning.

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

## `/dashboard/market-feed/*`

Dashboard Live Feed is a readonly display feed. It is independent from the
data-only Start button and may run while `collector_state=stopped`. It must not
create `market_microstructure_snapshot`, `signal_candidate`, `order_intent`,
`broker_order`, or `order_state_event` rows and must never call `PostOrder` or
`CancelOrder`.

The API owns a small in-process `DashboardMarketFeedService` cache. The first
dashboard request starts a lightweight refresh path:

- quote board: `GetLastPrices` for the core universe, default every 2 seconds;
- selected instrument: `GetOrderBook`, default every 1 second while the dashboard
  is polling;
- selected trade tape: readonly last trades when available; otherwise
  `market_trades_source=no_market_trades_samples` or
  `no_market_trades_feed_implemented`;
- selected broker trading status: `GetTradingStatus`, default every 5 seconds, used
  only to keep `session_type`/`session_phase`/`venue_type` consistent for display;
- session/venue fields are display metadata only. Official exchange closed days stay
  closed for calibration even if broker quotes are available. Data-only collection
  preflight remains the gate for persistent logging.

`GET /dashboard/market-feed/snapshot` accepts:

- `instruments=SBER,GAZP,...` optional filter;
- `selected_instrument=MOEX:SBER` default;
- `include_order_book=true|false`;
- `include_trades=true|false`.

Response fields:

- `session`: `market_open`, `session_type`, `session_phase`, `venue_type`,
  `data_only_collection_allowed`, `reason_code`, `next_session_at`;
- `quote_rows`: exactly the display rows used by the quote board;
- `selected_details`: one `MarketInstrumentOverview` row with bid/ask, mid,
  spread, depth, imbalance, quality components, order-book freshness and trade tape;
- `status`: enabled/running/freshness counters, selected instrument, warnings/errors;
- `data_only_collection_required=false`.

## `/market/overview`

`/market/overview?include_details=false` returns one row per core universe instrument
even when no live order book exists. It is now a cheap BFF read-model backed by
Dashboard Live Feed cache first, then stored `order_book_summary`, `market_candle`
and previous-close fallback. It accepts `instruments=SBER,GAZP` for filtered quote
boards. The default `include_details=false` keeps payloads small: it must not fetch
heavy order books for all eight instruments.

Important quote fields:

- `last_price`, `last_price_at`, `last_price_ts`;
- `last_price_source`: `live_order_book_mid`, `tbank_last_price`,
  `latest_market_candle_close`, `previous_close`, or `unavailable`;
- `quote_status`: `live`, `stale`, `previous_close`, or `unavailable`;
- `is_price_stale` and `price_staleness_seconds`;
- `previous_close`, `change_abs`, `change_bps`;
- `best_bid`, `best_ask`, `mid_price`, `spread_abs`, `spread_bps`;
- `bid_depth_lots`, `ask_depth_lots`, `book_imbalance`, `market_quality`;
- `order_book_source`, `order_book_ts`, `order_book_stale`;
- compact `order_book_summary` metadata when available;
- `quote_payload`.

`GET /market/instruments/{instrument_id}/details` returns the same
`MarketInstrumentOverview` shape for one selected instrument with detailed
`order_book_summary.bids[]`, `order_book_summary.asks[]`, explicit
`market_trades_source` and `recent_market_trades` when the readonly feed has samples.
It must not refresh all eight order books or start any runtime stream.

Price source priority for the dashboard is:

1. fresh live/read-model order-book mid;
2. Dashboard Live Feed last price / selected order-book result;
3. latest stored `market_candle` close;
4. previous close;
5. unavailable with reason code.

Stale data must stay visible with `quote_status=stale` and timestamp. A candle from
an older trading date must not be labeled as current/live.

`POST /market/quotes/refresh` remains an explicit readonly broker path. It accepts
`quotes_only=true` and `include_order_book=false` defaults. It may call T-Invest
`GetLastPrices` and, only when details/order-book refresh is requested, `GetOrderBook`
with bounded timeouts. It must not call `PostOrder` or `CancelOrder`. If the readonly
gateway cannot be constructed, the endpoint returns the local `/market/overview`
payload quickly so the frontend does not get stuck on 500/504.
When `GetOrderBook` succeeds, the quote is fresh by broker response receipt time;
the original exchange timestamp is exposed only as diagnostic payload
(`exchange_ts` / `exchange_age_seconds`).
The API keeps successful readonly quote refresh rows in a short in-process cache
(`MARKET_QUOTE_REFRESH_CACHE_TTL_SECONDS`, default 45 seconds). During that TTL,
`GET /market/overview`, `/dashboard/state`, and `/ws/market` overlay the cached
readonly broker rows on top of the local read model. This prevents the frontend from
being immediately overwritten by an older candle fallback after a successful refresh.
The cache overlay is session-gated: if the fresh/base read model says
`official_exchange_open=false`, cached rows that still claim live exchange source,
`quote_allowed_for_data_collection=true`, or `include_in_calibration=true` are
ignored for that response.

Closed-session broker quotes and selected order books are display-only. They may
set `quote_allowed_for_display=true`, but they must use
`broker_quote_exchange_closed`, `broker_indicative_quote`, `broker_otc`, or
`stale_local` style labels, must keep `quote_allowed_for_data_collection=false`,
and must set `calibration_market_quality_score=0` or not applicable. Only an
open session accepted by fresh data-only preflight may produce calibration-eligible
market quality.

## `/runtime/data-shadow/status`

The data-shadow status payload includes observable supervisor fields:

- `supervisor_enabled`;
- `supervisor_state`;
- `stream_restart_count`;
- `last_restart_at`;
- `last_restart_reason`;
- `stream_stale_count`;
- `last_stream_error`;
- `per_stream_status` for `order_book`, `last_price`, `candles`,
  `trading_status`, and `market_trades` when available.

When the collector is intentionally stopped or preflight-blocked,
`supervisor_state=stopped`. If the implementation is not configured,
`supervisor_state=not_configured` is explicit rather than omitted.

If stream order books are silent while Start is accepted, trade-core may write
microstructure through bounded readonly `GetOrderBook` polling. These rows keep
`source=data_only_shadow` and include `data_only_polling_fallback=true`,
`include_in_calibration`, `calibration_allowed`, and `venue_type` in
`snapshot_payload`. Stop disables both streams and polling.

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

`available_cash_rub` and `blocked_cash_rub` are RUB-only buckets from broker
positions. Non-RUB cash balances are not summed into RUB fields unless an explicit
FX conversion model is added later. `balance_currency` must remain `RUB` for these
rub-denominated dashboard fields.

Dashboard balance reads are intentionally fast-path reads: `/portfolio/summary` and
`/robot/status.balance` read only the latest `position_snapshot` timestamp, not the
full historical position table. `POST /portfolio/refresh` has a bounded broker timeout
and returns a degraded reason such as `broker_accounts_empty` or
`broker_balance_timeout` instead of blocking the UI indefinitely.

`GET /session/preflight` accepts optional `instruments=SBER,GAZP` and
`mode=data_shadow`. The response includes `market_open`, `market_closed_expected`,
`now_msk`, `trading_date`, `calendar_date`, `session_type`, `session_phase`,
`broker_trading_status`, `api_trade_available`, `next_session_at`,
`next_session_type`, `reason_code`, `source`, `instruments_checked` and
`per_instrument_status`.

Anchor: data-only shadow endpoints are listed above and remain observer/read-only APIs.
## Market Source Contract

`GET /session/preflight` exposes both official exchange status and raw broker
availability. `official_exchange_open=true` is required before data-only calibration
collection may start. The operator dashboard must not call `/robot/start` when
preflight returns closed/blocked. If an API client still calls `/robot/start` with a
closed preflight, the API returns `accepted=false`, `status=rejected`, and a concrete
`reason_code` such as `moex_dsvd_cancelled_platform_update`.

`GET /market/overview?include_details=false` returns one row per core instrument.
`POST /market/quotes/refresh?instruments=...` may return the requested subset, and
`GET /market/instruments/{instrument_id}/details` returns one selected row with heavy
details. Rows include `venue_type`, `trading_mode`, `official_exchange_open`,
`official_exchange_closed`, `quote_source`, `quote_allowed_for_data_collection`,
`spread_abs_rub`, `spread_bps`, `display_market_quality_score`,
`calibration_market_quality_score`, `market_quality_components`, and explicit
reason/warning fields. Detailed rows additionally include `recent_market_trades` and
order-book levels.

Spread units are fixed: `spread_abs`/`spread_abs_rub` are RUB, and
`spread_bps = (best_ask - best_bid) / mid_price * 10000`.

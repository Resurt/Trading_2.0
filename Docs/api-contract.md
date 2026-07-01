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

`POST /robot/start` is the authoritative operator command endpoint and must return
quickly. It no longer waits for a full synchronous broker preflight before creating
`robot_command`. The response includes `command_id`, `accepted=true`, `queued=true`,
`status=preflight_pending`, `reason_code=preflight_pending`,
`next_poll_after_seconds`, and `effective_logging_state=start_pending`.
`trade-core` performs the fresh broker/session preflight in the background with
bounded retry/backoff, then either starts data-only collection or marks the command
blocked with the preflight `reason_code`. The browser may call a fast advisory
`GET /session/preflight`, but a timeout there must not prevent `POST /robot/start`
from queuing the command.

The API keeps a short server-side preflight cache controlled by
`TRADING_SESSION_PREFLIGHT_CACHE_TTL_SECONDS`, default 30 seconds. This lets
`POST /robot/start` reuse the fresh `GET /session/preflight` result that the
dashboard just received instead of issuing a second slow broker status pass.
Incident triage can bypass this cache with
`GET /session/preflight?...&cache=false`; the response includes `cache_hit` and
`cache_key` so CLI/API comparisons are explicit.
Fresh broker preflight is bounded by `TRADING_SESSION_PREFLIGHT_TIMEOUT_SECONDS`,
default 30 seconds in code and 45 seconds in Docker Compose. Start-command
preflight retry is controlled by `TRADING_SESSION_PREFLIGHT_RETRY_COUNT` and
`TRADING_SESSION_PREFLIGHT_RETRY_BACKOFF_SECONDS`. Dashboard readonly broker calls
must yield to Start pressure: the API uses a separate bounded readonly executor
(`BROKER_READONLY_MAX_CONCURRENCY`, default 4) and briefly pauses dashboard broker
refreshes during Start (`DASHBOARD_FEED_PAUSE_DURING_START_SECONDS`, default 120).
`GET /health` is service liveness and must remain responsive even when broker
connectivity is degraded.

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

One accepted data-only Start creates a daily collection intent. Runtime status
must represent the day lifecycle separately from the current stream state:
`daily_collection_active=true` while the intent is alive,
`collector_state=collecting` during a window, `collector_state=paused_until_next_window`
between same-day windows, and `collector_state=stopped_day_complete` after the
last window. Manual Stop sets `collector_state=stopped_by_operator`, clears
`daily_collection_active`, and cancels auto-resume.

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
- `queued`;
- optional `preflight_result`/`preflight_summary`;
- `next_poll_after_seconds`;
- `effective_logging_state`.

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

`/ws/market-feed` is the primary Dashboard Live Feed WebSocket. `/ws/market` is
kept as a compatible alias and sends the same `market.snapshot` envelope with
DashboardMarketFeed payload. The first snapshot is sent immediately after connect
and does not require pressing Start.

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
- selected trade tape: readonly all-source last trades when available; otherwise
  `trade_tape_status=no_market_trades_samples` /
  `market_trades_source=no_market_trades_samples` or
  `no_market_trades_feed_implemented`;

The selected trade-tape path first requests a short `GetLastTrades` lookback and,
when the broker returns an empty trades list, retries once with
`DASHBOARD_TRADES_FALLBACK_LOOKBACK_MINUTES` (default 30). The display budget for
delayed rows is `DASHBOARD_TRADES_DELAYED_DISPLAY_SECONDS=300`; delayed rows keep
`trade_tape_status=stale` and `trade_tape_reason=trade_exchange_ts_too_old`.

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

The operator dashboard must not mix market-session copy with data-only collector
lifecycle reasons. Market session text is derived from the dashboard feed/session
fields. Data-only reasons such as `data_only_collection_started` are displayed only
in the logging panel and `/runtime/data-shadow/status`.

Every quote/order-book/trade-tape row carries dual freshness metadata where the
broker response receipt time is distinct from exchange data time:
`received_ts`, `exchange_ts`, `received_age_ms`, `exchange_age_ms`,
`stale_by_received_time`, `stale_by_exchange_time`, `freshness_status`, and
`freshness_reason`. For live order-book snapshots, `received_ts` is authoritative
for operator-display freshness because `exchange_ts` may represent the last
exchange-side book change. Last-price-only, candle, previous-close, OTC/indicative
and trade-tape fallbacks remain exchange-time gated; stale exchange data remains
display-only and is not calibration eligible.
The selected order-book feed refreshes below the freshness budget by default
(`DASHBOARD_SELECTED_BOOK_REFRESH_SECONDS=3`,
`DASHBOARD_ORDER_BOOK_MAX_EXCHANGE_AGE_SECONDS=30`) and forces a refresh when the
cached selected ladder is about to become stale.
For `selected_details`, the BFF loads the selected instrument with full
read-model details before applying readonly broker overlays. A partial
`GetOrderBook` response with fewer levels must not collapse a fresh stream-backed
selected ladder. `status.order_book_available=true` requires actual
`order_book_summary.bids` and `.asks` arrays with at least five levels per side;
`depth_levels` alone is not enough and a one-row top-of-book refresh must be
reported as loading/unavailable instead of a fresh selected book.
If the dashboard session is closed, `session.session_type` and
`session.session_phase` must both be `closed`; broker OTC/indicative availability
is reported through `venue_type`, `quote_source`, and `trading_mode`, not by
rendering an old weekday session as active. Cached live exchange ladders from a
previous open window must not be copied into closed-session selected details.
If another refresh is already in progress, the service may return the last cache
snapshot instead of starting a duplicate broker fan-out. That single-flight state
is not an operator-facing error and must not overwrite an otherwise usable
dashboard with a persistent `dashboard_refresh_in_progress` warning.

## `/market/overview`

`/market/overview?include_details=false` returns one row per core universe instrument
even when no live order book exists. It is now a cheap BFF read-model backed by
Dashboard Live Feed cache first, then stored `order_book_summary`, `market_candle`
and previous-close fallback. It accepts `instruments=SBER,GAZP` for filtered quote
boards. The default `include_details=false` keeps payloads small: it must not fetch
heavy order books for all eight instruments.

Stored `order_book_summary` rows can use the broker storage id (`instrument_uid`),
`figi`, ticker, or canonical `MOEX:*` id depending on the writer path. The BFF must
resolve these aliases through `instrument_registry` before marking a quote row as
missing book data. If a fresh stored order-book mid is available, a later stale
`GetLastPrices` response must not downgrade the quote card to stale last-price data.

Important quote fields:

- `last_price`, `last_price_at`, `last_price_ts`;
- `last_price_source`: `live_order_book_mid`, `tbank_last_price`,
  `latest_market_candle_close`, `previous_close`, or `unavailable`;
- `quote_status`: `live`, `stale`, `previous_close`, or `unavailable`;
- `is_price_stale` and `price_staleness_seconds`;
- `received_ts`, `exchange_ts`, `received_age_ms`, `exchange_age_ms`,
  `freshness_status`, and `freshness_reason`;
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

Trade tape freshness is independent from order-book freshness. Fresh rows use
`trade_tape_status=live`. Short delayed readonly `GetLastTrades` rows may
populate `recent_market_trades` only while their newest exchange timestamp is
within `DASHBOARD_TRADES_DELAYED_DISPLAY_SECONDS`; they must keep
`trade_tape_status=stale` and `trade_tape_reason=trade_exchange_ts_too_old`, so
clients render them as delayed, not as a live market stream. Rows older than that
display budget return an empty list with the same stale status/reason. Use
`market_trades_source=tbank_get_last_trades` for that diagnostic source; raw
internal names such as stale diagnostic source variants must not appear in the
operator UI.
Dashboard trade tape is display-only. It may use T-Bank all-source market trades
to avoid an empty exchange-only tape, but those rows are not primary calibration
rows and must not create `market_microstructure_snapshot`,
`signal_candidate`, `order_intent`, `broker_order`, or `order_state_event`.
Trade-core market-data stream payloads must canonicalize broker `instrument_uid`
or `figi` to the selected dashboard id (`MOEX:*`) before writing read-model
payloads. The original broker id is retained as `broker_instrument_id`, but the
dashboard joins selected trades/order books by canonical `instrument_id`.

`POST /market/quotes/refresh` remains an explicit readonly broker path. It accepts
`quotes_only=true` and `include_order_book=false` defaults. It may call T-Invest
`GetLastPrices` and, only when details/order-book refresh is requested, `GetOrderBook`
with bounded timeouts. It must not call `PostOrder` or `CancelOrder`. If the readonly
gateway cannot be constructed, the endpoint returns the local `/market/overview`
payload quickly so the frontend does not get stuck on 500/504.
When `GetOrderBook` succeeds, BFF receipt time and exchange data time remain
separate. For order books, a newly received broker snapshot is display-fresh even
when `exchange_ts` is older because the book may simply not have changed; the
exchange age is still returned as diagnostics.
The selected-instrument order book must refresh faster than its freshness
threshold; the default is a 3-second selected book refresh with a 30-second
display freshness budget.
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

Known-invalid primary market-data rows are not retained as rejected calibration
samples. If a bug writes `market_microstructure_snapshot` or dependent primary
rows after the session cutoff, during official closure, in OTC/indicative mode,
from stale local history, or with wrong session context, maintenance must create
a purge manifest, write `audit_event.action=data_only_invalid_rows_purged`, and
remove those rows from primary calibration/logging tables. Audit metadata may
remain in `audit_event` and `.local` reports; invalid primary rows must not stay
in the tables consumed by calibration.

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
- `day_collection_state`, `daily_collection_active`, `current_window_state`;
- `next_collection_window_at`, `remaining_windows_today`, `next_resume_at`;
- `paused_at`, `completed_for_day_at`, `last_window_completed_at`;
- `last_stop_reason`, `last_pause_reason`, `last_resume_at`;
- `collector_left_running`.

When the collector is intentionally stopped or preflight-blocked,
`supervisor_state=stopped`. If the implementation is not configured,
`supervisor_state=not_configured` is explicit rather than omitted.
If a same-day session window ends and another window remains,
`collector_state=paused_until_next_window`, `stream_alive=false`,
`daily_collection_active=true`, and the payload exposes
`next_collection_window_at`/`next_resume_at`. If the last window ends,
`collector_state=stopped_day_complete`, `daily_collection_active=false`, and
`completed_for_day_at` is set. Recent snapshots alone must not make a paused or
stopped collector look alive.

If Start is requested before a same-day collection window and the next window is
within `DATA_SHADOW_START_ARMING_MAX_WAIT_HOURS`, trade-core may return
`command_status=armed_until_next_window`, `daily_collection_active=true`,
`next_collection_window_at`, `start_armed_at`, and
`effective_logging_state=armed`/`waiting_for_open`. No streams or calibration
rows are created before the window opens. Manual Stop cancels both armed and
active daily collection.

`GET /robot/status` distinguishes the API control state from logging state with
`robot_control_state`, `data_shadow_collector_state`, `daily_collection_active`,
and `effective_logging_state`. It must not report a stopped or paused data-shadow
collector as simply running.

Trade-core emits these data-only lifecycle audit events:
`data_only_shadow_collection_started`,
`data_only_shadow_collection_armed_until_next_window`,
`data_only_shadow_collection_window_closed`,
`data_only_shadow_collection_paused_until_next_window`,
`data_only_shadow_collection_resumed`,
`data_only_shadow_collection_day_complete`,
`data_only_shadow_collection_stopped`,
`data_only_shadow_collection_auto_stopped`, and
`data_only_shadow_collection_resume_failed`. Payloads include trading date,
window boundaries, next resume time, requested/working instruments,
`readonly_calls_only=true`, `real_orders_disabled=true`, and
`strategy_trading_disabled=true`.

If stream order books are silent while Start is accepted, trade-core may write
microstructure through bounded readonly `GetOrderBook` polling. These rows keep
`source=data_only_shadow` and include `data_only_polling_fallback=true`,
`include_in_calibration`, `calibration_allowed`, and `venue_type` in
`snapshot_payload`. Stop disables both streams and polling.

Primary microstructure/order-book writes reject invalid calibration samples before
persistence. Rejection reasons include `crossed_book`, `invalid_spread`,
`invalid_depth`, `invalid_imbalance`, `missing_bid_ask`,
`outside_session_window`, and `non_calibration_source`. Rejected data-only
microstructure samples emit rate-limited
`data_only_microstructure_row_rejected` audit evidence and do not create trading
entities.

After a trade-core restart, `started`/`resumed` lifecycle audit events restore only
the daily collection intent. They are not proof that this new process has live stream
tasks. Runtime must perform a fresh preflight and resume streams/polling immediately
when the current window is open. A future `next_resume_at` is honored only for an
actual `paused_until_next_window` lifecycle event.

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
| `/ws/market-feed` | Primary readonly Dashboard Live Feed: quote board, selected instrument details, order-book summary, explicit trade tape status and session/freshness metadata. |
| `/ws/market` | Compatibility alias for `/ws/market-feed`; sends the same `market.snapshot` DashboardMarketFeed payload. |
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
collection may start. The operator dashboard may use preflight for advisory copy,
but `/robot/start` is the authoritative async command endpoint. If a queued Start is
later blocked by closed-market or broker/session preflight, trade-core marks the
command `blocked_by_preflight`/`preflight_blocked` with a concrete `reason_code`
such as `moex_dsvd_cancelled_platform_update`; streams are not started and no
calibration rows are written.

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

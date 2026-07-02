# Data-only shadow collector

Data-only shadow is a readonly market-data collection mode. It is not trading shadow and not
strategy shadow.

## Purpose

Use this mode after candle-only historical research fails to produce a shadow-ready contour. The
collector gathers live microstructure needed for later calibration:

- top of book, spread and mid price;
- depth and book imbalance;
- market quality score and freshness;
- stream health and reconnect pressure;
- candle delivery lag and trading status context.

## Safety invariants

- `TRADING_DATA_ONLY_SHADOW=true` must be set.
- No `signal_candidate`, `order_intent`, `broker_order`, or pseudo-order should be created by this
  mode.
- `PostOrder` and `CancelOrder` are forbidden.
- Strategy evaluation is disabled; the runtime does not subscribe the closed-bar strategy handler.
- Production mode is not used.

## Storage

The collector writes `market_microstructure_snapshot` with:

- `best_bid`, `best_ask`, `mid_price`, `spread_abs`, `spread_bps`;
- `bid_depth_lots`, `ask_depth_lots`, `book_imbalance`;
- `market_quality_score`, `feed_freshness_age_ms`, `is_stale`;
- `exchange_ts` when the broker/source payload provides exchange event time;
- `received_ts` for the local receive/write time;
- `exchange_age_ms`, `received_age_ms`, `stale_by_exchange_time`,
  `stale_by_received_time`, `freshness_basis`, `exchange_ts_missing_reason`, and
  `strict_dual_freshness_eligible`;
- session context: `trading_date`, `session_type`, `session_phase`, `micro_session_id`;
- `source=data_only_shadow`.

The collector also persists real market tape samples in `market_trade_sample`.
It first uses broker market-trade stream events when they arrive; when the stream
does not yield samples during an allowed collection window, the bounded readonly
`GetLastTrades` fallback can persist real broker rows through the same market
data pipeline. It must never create fake tape rows. Missing trades are reported
as status/reason diagnostics, not as samples. Fallback rows default to
`include_in_calibration=false` unless a future policy explicitly promotes them.

Trade polling is controlled by `DATA_SHADOW_COLLECT_TRADES=true`,
`DATA_SHADOW_TRADES_POLL_SECONDS=5`,
`DATA_SHADOW_TRADES_LOOKBACK_SECONDS=60`, and
`DATA_SHADOW_TRADES_MAX_PER_INSTRUMENT=100`.
`/runtime/data-shadow/status` exposes `trade_collection_enabled`,
`trade_sample_count`, `trade_samples_seen`, `last_trade_sample_at`,
`last_data_only_trade_poll_at`, and `trade_collection_reason`.

`exchange_ts` must never be fabricated from `received_ts`. If historical rows do
not contain an exchange timestamp and no matching broker/order-book payload
contains it, remediation marks them as `freshness_basis=received_ts_only`,
`strict_dual_freshness_eligible=false`, and records
`exchange_ts_missing_reason`. Such rows can support partial diagnostics but not
strict dual-freshness calibration.

## Local smoke

Dry-run, no broker calls:

```bash
python scripts/run_data_only_shadow_smoke.py --instruments SBER,GAZP --minutes 1 --dry-run --json-output
```

Readonly live smoke, only when token and market data access are configured:

```bash
set TRADING_DATA_ONLY_SHADOW=true
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP --strict --json-output
python scripts/run_tbank_dividend_sync.py --instruments SBER,GAZP --lookback-days 730 --lookahead-days 365 --json-output
python scripts/run_data_only_shadow_smoke.py --instruments SBER,GAZP --minutes 10 --require-dividend-sync --json-output
```

If the market is closed, zero order book samples is a warning, not a trading failure.

## Summary report

```bash
python scripts/run_data_shadow_summary_report.py --lookback-hours 6 --json-output
```

The report is written to `.local/collection_reports/data_shadow/data_shadow_summary_latest.json`.
The report exposes `exchange_ts_present_count`, `exchange_ts_missing_count`,
`received_ts_only_count`, `strict_dual_freshness_eligible_count`,
`freshness_basis_distribution`, `trade_tape_sample_count`, and
`tape_confirmed_candidate_count`.

## Readiness gate

```bash
set TRADING_DATA_ONLY_SHADOW=true
python scripts/run_launch_readiness.py --mode data-shadow --instruments SBER,GAZP --shadow-minutes 10
```

The gate checks SDK import, instrument registry readiness, dividend sync readiness unless explicitly
skipped for local dry-run, absence of production confirmation, smoke counters, and no order calls.

## API and dashboard

API endpoints:

- `GET /market/microstructure/latest`
- `GET /market/microstructure/summary`
- `GET /runtime/data-shadow/status`

The live dashboard shows `Data-only Shadow Status` and explicitly states:

```text
Strategy trading disabled: data-only shadow mode
```

## Next calibration step

Collect 10-20 trading days of data-only shadow samples, then calibrate spread, depth, imbalance,
freshness, slippage assumptions, latency, and stream stability before considering any strategy shadow.

Run diagnostic analytics after data-only shadow has collected enough market hours:

```bash
python scripts/run_intraday_analytics.py --date YYYY-MM-DD --mode data_shadow --json-output
python scripts/run_calibration_observatory.py --universe SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --lookback-days 20 --mode data_shadow --json-output
```

Outputs:

- `.local/collection_reports/intraday/`
- `.local/collection_reports/calibration_observatory/`

Interpretation boundary:

- Intraday Analytics is diagnostic only and does not enable trading.
- Calibration Center can report `market_dead`, `robot_too_strict`, `data_quality_problem`,
  `regime_changed`, `not_enough_data`, `normal_no_action_needed` or
  `calibration_recommended`.
- 10-20 trading days are early evidence, not final truth.
- Candidate configs created by the observatory are draft proposals only and are not applied to live
  trading automatically.

## Exchange timestamp and tape verification

Closed-market broker payloads are inconclusive for `exchange_ts` and trade tape
availability. After a schema/pipeline change, verify during an open collection
window with:

```bash
python scripts/run_live_exchange_ts_trade_tape_diagnostic.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --minutes 10 --json-output
```

The script is readonly. It calls `GetOrderBook`, `GetLastPrices`,
`GetLastTrades`, and `GetTradingStatus`, checks DB deltas for
`market_microstructure_snapshot`, `order_book_summary`, and
`market_trade_sample`, and exits with `market_closed_live_diagnostic_not_run`
when the market is closed.

## Session preflight before live samples

Every live data-only smoke must run session/calendar preflight before starting runtime streams.

Required order:

```bash
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --strict --json-output
python scripts/run_tbank_dividend_sync.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --lookback-days 730 --lookahead-days 365 --json-output
python scripts/run_data_only_shadow_smoke.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --minutes 0 --preflight-only --require-dividend-sync --json-output
```

Preflight fields include `market_open`, `market_closed_expected`, `reason_code`,
`next_session_at`, `session_type`, `session_phase`, `broker_trading_status`,
`api_trade_available`, `per_instrument_status`, `source`, `schedule_source`,
`status_source`, `schedule_error_code`, `schedule_error_message`,
`status_success_count`, `status_error_count`, `fallback_used`,
`requested_instruments`, `working_instruments`, and `blocked_instruments`.

CLI smoke and API `/session/preflight` use the same `TradingSessionPreflightService`
rules. For fresh API comparison during incident triage, call:

```text
GET /session/preflight?instruments=SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR&mode=data_shadow&cache=false
```

Operator-facing session state is reconciled from fresh preflight. `/session/current`
and `/robot/status` must not present an old `session_run` row as current when
fresh preflight says the market is closed or a different session is active. Check
`source`/`session_source`, `stale`, and `stale_reason` when investigating a
session mismatch.

If T-Bank `TradingSchedules` fails with `INVALID_ARGUMENT 30003`, preflight must
not silently contradict another caller. The response records
`schedule_source=tbank_error`, `schedule_error_code=30003`,
`fallback_used=true`, then falls back to local MOEX time windows and broker
`GetTradingStatus`:

- if all instrument statuses are unavailable, collection is blocked with
  `reason_code=broker_status_and_market_data_unavailable` unless the readonly
  market data probe succeeds;
- if at least one instrument has an open broker status during an open fallback
  window, `working_instruments` contains only those allowed instruments and
  `blocked_instruments` explains the rest;
- if broker schedule has no active window for the current local MOEX fallback
  window but broker statuses report exchange trading or the readonly market data
  probe succeeds,
  preflight may open data-only collection with
  `source=broker_status_fallback_time_rules`,
  `schedule_source=broker_trading_schedules_status_fallback`,
  `fallback_used=true`, and warnings
  `broker_schedule_missing_active_window` plus either
  `broker_status_open_schedule_closed` or `market_data_probe_used_without_status`;
- if broker schedule is empty/incomplete while the local fallback window is open,
  that is not enough by itself to call the market closed. Preflight records
  `market_window_open=true` and then gates data-only collection on
  `GetTradingStatus` or readonly `GetLastPrices`/`GetOrderBook` probe evidence.
  If both status and probe fail, collection stays blocked with
  `reason_code=broker_status_and_market_data_unavailable`.

The smoke script starts streams only for `working_instruments`. If that list is
empty, it returns `no_tradeable_instruments` and does not start runtime streams.
CI and unit smoke tests must pass an explicit isolated `database_url` such as a
temporary SQLite file. They must not rely on ambient local Docker secrets or
fallback to SQLite implicitly. Runtime/compose jobs still require PostgreSQL
unless a local experiment explicitly opts in with `TRADING_RUNTIME_LOCAL_SQLITE=1`.

Known 30003 context: T-Bank rejects `TradingSchedules` when request `from` is
earlier than the current broker date/time after timezone conversion. Preflight
requests schedules from the current preflight timestamp forward; the 30003
fallback remains only as a defensive path.

If `market_open=false` and `market_closed_expected=true`, the smoke must not start market streams,
must not subscribe to order book, and must not call the data-only runtime. The JSON result should
pass safety checks and include `warning=market_closed_expected_no_live_samples`,
`post_order_calls=0`, `cancel_order_calls=0`, `signal_candidates_delta=0`,
`order_intents_delta=0`, `broker_orders_delta=0`, and
`microstructure_snapshots_delta=0`.

Weekend handling:

- broker `TradingSchedules` is authoritative when available;
- a broker trading day on Saturday/Sunday is classified as `session_type=weekend`;
- fallback weekend window is 10:00-19:00 MSK. If broker schedule is empty or
  omits the active weekend window, an open broker status or successful readonly
  market data probe can promote it to
  `source=broker_status_fallback_time_rules`;
- outside the weekend window, closed market is expected and `next_session_at` must be present when
  known.

Readiness gate:

```bash
python scripts/run_launch_readiness.py --mode data-shadow --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --shadow-minutes 10 --gate-timeout-seconds 900
```

The readiness gate first runs preflight-only. If the market is expected closed, it passes with
`status=market_closed_expected`, `no_live_samples_expected=true`, and `smoke_was_run=false`.
If the market is open, it runs the bounded data-only smoke.

For large universes, use stream batching flags on smoke. `RESOURCE_EXHAUSTED` is a broker resource
warning; do not retry aggressively, reduce the universe or stream batch size.

## Operator dashboard Start/Stop

The dashboard Start button is not a blind start command. It first calls:

```text
GET /session/preflight?instruments=SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR&mode=data_shadow
```

Current policy: the dashboard preflight is advisory only. The Start button must not
die on a long broker preflight or `request_timeout`. The browser creates an operator
command immediately through `POST /robot/start`; the API returns quickly with
`status=preflight_pending`, `command_id`, `queued=true`, `next_poll_after_seconds`
and `effective_logging_state=start_pending`. `trade-core` then performs the
authoritative fresh preflight with bounded retries/background status updates. If
collection is allowed it starts the data-only collector; if not, the command moves
to `blocked_by_preflight`/`preflight_blocked` with reason and next session when
available.

The Start button progress states are:

- checking session / `preflight_pending`;
- broker retry / `preflight_retrying`;
- collector starting;
- collecting;
- blocked or failed with reason.

The command strip must show short operator text. Success/already-running messages
auto-dismiss after 10-15 seconds. Blocked/failed messages remain dismissible.

Stop remains a controlled operator command. In data-only mode it stops/cancels market
stream tasks, moves collector state to `stopped_by_operator`, and shows the result in
the command status strip. It does not wait for intraday, summary, calibration, or
other report generation. The dashboard must immediately switch the data-only logging
panel from active collection to `Останавливается`/`Сбор логов остановлен` after the
operator command is accepted, then reconcile with `/runtime/data-shadow/status`.
Trade-core runs a fast robot-command poller
(`TRADING_ROBOT_COMMAND_POLL_INTERVAL_SECONDS`, default `0.25`) so Stop does not
wait for the next full runtime tick.
If the runtime is inside the readonly order-book polling fallback, it checks Stop
between instruments and must not wait for a full universe polling batch to finish.

One accepted Start creates a daily collection intent. For a weekday trading date,
the runtime collects the morning window, pauses as `paused_until_next_window` at
the morning cutoff, automatically resumes for `weekday_main`, pauses before
`weekday_evening`, and completes the day as `stopped_day_complete` after the final
window. The pause is not an operator stop: `daily_collection_active=true`,
`next_collection_window_at`/`next_resume_at` are populated, streams are stopped, and
no primary calibration rows are written in the gap. Manual Stop sets
`stopped_by_operator`, clears the daily intent, records `cancelled_by_operator=true`,
and must not auto-resume at the next window.

Process restart recovery uses the latest data-only lifecycle audit event. If the
last durable state is `paused_until_next_window` and the operator did not stop the
run, fresh preflight decides whether to resume immediately, stay paused, or mark
the day complete. If the last durable state is `data_only_shadow_collection_started`
or `data_only_shadow_collection_resumed`, the restarted process must not assume the
old stream tasks are still alive and must not wait for a future evening
`next_resume_at`. It should run a fresh preflight on the next cycle and immediately
restart streams/polling when the current collection window is open.

## Controlled Docker rebuild while collecting

Docker rebuilds/restarts are never done blindly while data-only collection is
running. If a code or configuration change requires `docker compose up -d --build`
and the current status shows `collector_state=collecting`, `stream_alive=true`, or
fresh snapshots are growing, use this controlled Stop -> rebuild -> Start flow:

1. Capture the current state:

   ```text
   GET /health
   GET http://localhost:8001/health
   GET /runtime/data-shadow/status
   GET /robot/status
   GET /session/preflight?instruments=SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR&mode=data_shadow&cache=false
   ```

2. Stop collection through the operator API before touching Docker:

   ```text
   POST /robot/stop
   X-API-Role: operator
   X-API-Actor: controlled_docker_rebuild
   ```

   Verify that `collector_state` is `stopped_by_operator`/`stopped`/`idle`,
   `stream_alive=false`, snapshot counts stop growing after the grace period, and
   `signal_candidate`, `order_intent`, `broker_order`, `order_state_event`,
   `PostOrder`, and `CancelOrder` deltas remain zero. If controlled Stop fails,
   do not rebuild.

3. Rebuild/restart Docker only after the controlled Stop is confirmed:

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

   Wait for `api`, `trade-core`, Postgres, Redis, frontend, report worker, Loki,
   Fluent Bit, Prometheus, and Grafana health checks to be healthy or ready as
   applicable. Re-check `GET /health`, `GET http://localhost:8001/health`, and a
   fresh preflight with `cache=false`.

4. Start data-only collection again if the fresh preflight allows collection:

   ```text
   POST /robot/start
   X-API-Role: operator
   X-API-Actor: restart_after_controlled_docker_rebuild

   {
     "mode": "data_shadow",
     "reason": "restart_after_controlled_docker_rebuild",
     "instruments": "SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR",
     "real_orders_disabled": true,
     "strategy_trading_disabled": true
   }
   ```

   If the current session is open, acceptance is `collector_state=collecting` and
   either `stream_alive=true` or both `market_microstructure_snapshot` and
   `order_book_summary` counts grow. Check this for at least one minute after
   Start; prefer confirming 8/8 instruments have fresh snapshots. Trading entity
   deltas and `PostOrder`/`CancelOrder` must stay zero.

5. If the current session is closed but the next same-day collection window is
   within the arming threshold, Start may arm the command as
   `command_status=armed_until_next_window`; no primary rows should be written
   before the window opens. If preflight is blocked and the next session is not
   today, do not Start; record `reason_code` and `next_session_at`.

The intent is to preserve collection continuity and operator control: stop
normally, rebuild normally, prove the stack is healthy, start normally, and prove
the collector actually resumed. Do not run `docker compose up -d --build` against
a live collector without this sequence.

## Broker balance visibility

Refresh broker account state before live data-only checks:

```bash
python scripts/run_broker_balance_refresh.py --json-output
```

The command is readonly: it uses `get_accounts`, `get_portfolio` and `get_positions`
only. It writes masked `broker_balance` payloads for `/portfolio/summary` and
`/robot/status.balance`. If broker balance is unavailable, the dashboard still shows
the card with `balance_degraded=true` and `balance_degraded_reason_code`.

The dashboard auto-refreshes balance through readonly `POST /portfolio/refresh` while
open. The API container needs the readonly T-Bank token mounted for this path. If
`GetAccounts` is empty/unavailable but `TRADING_ACCOUNT_ID` is configured, the refresh
may still call readonly `get_portfolio`/`get_positions` for that account and only expose
the masked account id. The manual CLI remains useful for morning preflight and troubleshooting.

## Dashboard quotes

The Live Dashboard must show the core universe prices even when live collection is not
running. This is not data-only collection. Dashboard display uses
`DashboardMarketFeedService` through primary WebSocket `/ws/market-feed`
(`/ws/market` remains a compatibility alias), REST fallback
`/dashboard/market-feed/snapshot`, and `/dashboard/market-feed/status`; Start is
only for persistent logging.

Dashboard Live Feed may call readonly T-Invest methods (`GetLastPrices`,
compact quote-board `GetOrderBook`, selected-instrument `GetOrderBook`,
`GetTradingStatus`, last trades/status display) with bounded timeouts.
It must not write
`market_microstructure_snapshot` calibration logs, create trading entities, or call
`PostOrder`/`CancelOrder`. `GET /market/overview` is the cheap BFF read-model backed
by that feed cache first, then stored `order_book_summary`, `market_candle` and
previous-close fallbacks. It must return one row per core universe instrument and
expose `last_price_source`, `quote_status`, `is_price_stale` and timestamp.

Quote cards must stay live when the data-only collector is stopped. The dashboard
feed therefore refreshes compact readonly top-of-book/quality data for the core
universe and stores it only in API cache. When data-only collection is running,
quote cards may also prefer fresh `order_book_summary` top-of-book data written by
the collector. These rows may be stored under broker `instrument_uid`/`figi` while
the operator UI requests `MOEX:*`; the BFF resolves aliases through
`instrument_registry`. Do not start a second collector just because a card shows
stale last-price data; first check whether the dashboard readonly feed and
read-model alias path are finding the current broker/book data.

The dashboard feed distinguishes broker response receipt time from exchange data
time. `received_ts`/`received_age_ms` only say when the BFF received a readonly
response. For live order-book snapshots, receipt freshness is the display signal:
an unchanged exchange-side book can keep an older `exchange_ts` while the broker
is still returning a current snapshot. Last-price-only, candle, previous-close,
OTC/indicative and trade-tape fallbacks remain exchange-time gated. Old candle or
old exchange-time-only data must remain visible as stale/display-only and must not
be labeled live.

Selected-instrument switching is latest-wins. The frontend sends
`{"type":"market.select","selected_instrument":"MOEX:GAZP"}` over the market
WebSocket and ignores late selected-details responses for another instrument.
Late SBER responses may update only the SBER row; they must not change the user's
current selected instrument or show the SBER book under a GAZP heading.

Trade tape is explicit: selected details include either `recent_market_trades` or
`trade_tape_status`/`trade_tape_reason` such as `no_market_trades_samples`,
`get_last_trades_timeout`, `market_closed`, `stale`, or `unavailable`. Missing
trade tape must not block quotes or order-book display.
The operator dashboard requests all-source readonly market trades for display so
the tape does not disappear when exchange-only samples are unavailable. These
trade rows are display diagnostics, not primary calibration rows.
If broker `GetLastTrades` returns rows whose `exchange_ts` is older than
`DASHBOARD_TRADES_MAX_EXCHANGE_AGE_SECONDS`, those rows are diagnostic only and
must not be labeled live. Rows newer than
`DASHBOARD_TRADES_DELAYED_DISPLAY_SECONDS` may remain visible as a delayed tape
with `trade_tape_status=stale`, `trade_tape_reason=trade_exchange_ts_too_old`,
and `market_trades_source=tbank_get_last_trades`. Older diagnostic rows are
hidden behind the same explicit stale status/reason. A fresh order book with a
stale trade tape is a display limitation, not a data-only logging failure by
itself.
If `GetLastTrades` is transiently empty or stale while `market_trade_sample`
continues to grow, selected details may use the persisted data-only tape as a
read-only dashboard fallback. Expected fields are
`trade_tape_source=persisted_data_only_trade_tape`,
`persisted_trade_tape_available=true`, `latest_persisted_trade_ts`, and
`dashboard_trade_tape_fallback=persisted`. Do not treat this as live broker tape,
do not backfill/fabricate trades, and do not write any rows from the dashboard
feed path.
Data-only collector stream names must include `market_trades`; otherwise the
dashboard can only report `no_market_trades_samples`/stale diagnostics and cannot
show a true live tape.
If `market_trades` is alive but the selected tape remains empty, verify that
trade-core maps broker `instrument_uid`/`figi` to canonical `MOEX:*` in stream
payloads. The dashboard joins selected rows by canonical `instrument_id`; broker
ids are retained only as `broker_instrument_id`.
For analytics warnings, inspect structured classifications before escalating.
`stream_gaps_detected` means warning-severity gaps remain, while
`session_boundary_gap` and `sparse_identifier_gap` are informational context.
`some_rows_not_calibration_eligible` should be read with
`calibration_eligibility_breakdown`: strict timestamp eligibility only means real
`exchange_ts`/`received_ts` are present; strict calibration eligibility can still
reject stale or invalid rows.
Selected order-book refresh must remain below the freshness threshold. The current
defaults are `DASHBOARD_SELECTED_BOOK_REFRESH_SECONDS=3` and
`DASHBOARD_ORDER_BOOK_MAX_EXCHANGE_AGE_SECONDS=30`; if operators see an open-market
selected ladder flip to stale while broker health is OK, check whether the
dashboard is receiving fresh snapshots or serving an old cache. For order books,
`received_ts` drives operator-display freshness; `exchange_ts` is diagnostic and
can stay older when the book has not changed.
The frontend keeps the last full selected ladder through short intermittent
partial refreshes while that ladder is still inside the freshness budget. If it
expires, the UI must show the explicit stale/unavailable reason rather than an old
ladder as live data.
A full selected ladder means actual `order_book_summary.bids[]` and `.asks[]`
arrays with at least five levels per side. `depth_levels` without arrays, or a
single top-of-book row, is a partial refresh/loading state and must not be called
fresh/full in the operator UI.

Readonly broker quote refresh remains explicit for diagnostics:
`POST /market/quotes/refresh` may call T-Invest `GetLastPrices`/`GetOrderBook` with
bounded timeouts. Temporary request failures must not clear already displayed
frontend quotes. Successful readonly quote/feed rows are cached briefly by the API
and overlaid into subsequent `GET /market/overview` responses so a page reload does
not fall back to stale candle rows immediately after a live broker refresh.

Operator dashboard polling while open:

- `/dashboard/market-feed/snapshot` quote board: every 2 seconds;
- compact quote-board `GetOrderBook` refresh: default every 5 seconds, bounded
  concurrency, API cache only;
- `/dashboard/market-feed/snapshot` selected instrument details: every 1 second while
  dashboard feed sees `market_open=true`, otherwise every 5 seconds;
- selected broker trading status inside the dashboard feed: every 5 seconds;
- `/runtime/data-shadow/status`: every 2-5 seconds;
- `/market/quotes/refresh`: explicit readonly diagnostic/operator action, not an
  all-instruments dashboard mount poll;
- `/portfolio/refresh`: every 60 seconds.

These polling paths are readonly. They must not call `PostOrder`, `CancelOrder`, create
`signal_candidate`, create `order_intent`, or create `broker_order`.
## Data-Only Collection Gate

Data-only persistent logging is allowed only when fresh preflight reports
`market_open=true`, `market_window_open=true`, and
`data_only_collection_allowed=true`. If T-Bank schedules omit the active local
MOEX window, broker `GetTradingStatus` or readonly `GetLastPrices`/`GetOrderBook`
probe success can allow collection with fallback warnings. `trading_allowed=false`
always remains enforced in data-only mode.

Trade-core starts only the minimal data-only stream set (`order_book`,
`last_prices`, `trading_status`). If streams are silent but preflight allowed
collection and readonly `GetOrderBook` works, a bounded polling fallback writes
`market_microstructure_snapshot` through the same pipeline. This fallback is
readonly, checks operator Stop between instrument requests, and must not call
`PostOrder`/`CancelOrder` or create trading entities.

While data-only collection is running, trade-core also skips runtime position
snapshots. Operator balance diagnostics remain available through explicit
readonly `/portfolio/refresh` or `run_broker_balance_refresh.py`, but Start does
not require account-level `GetPositions`/`GetPortfolio` calls.

Calibration eligibility is still explicit. Broker OTC or indicative quotes can be
displayed on the dashboard, but they are not calibration samples. Snapshot payloads
must carry `include_in_calibration`/`calibration_allowed`; closed-session or
display-only data sets those fields to false.

Closed-session dashboard quotes and selected order books are display-only. They
must keep `quote_allowed_for_data_collection=false`, must not use live exchange
labels, and must set `include_in_calibration=false` /
`calibration_market_quality_score=0`.
The dashboard ribbon should show `Рынок закрыт` / `Торги закрыты` when the
feed session is closed. OTC or indicative broker quotes are shown only as
`venue_type`/`quote_source`; they must not make the UI jump back to
`weekday_evening`, `continuous_trading`, or `data_only_collection_allowed=true`.
Do not preserve an old live selected ladder across the official session close.

`/runtime/data-shadow/status` exposes supervisor observability fields:
`supervisor_enabled`, `supervisor_state`, `stream_restart_count`,
`last_restart_at`, `last_restart_reason`, `stream_stale_count`,
`last_stream_error`, and `per_stream_status`. After an intentional Stop or a
preflight-blocked Start, the supervisor state should be `stopped` and must not
auto-restart the collector.

For 2026-06-20 and 2026-06-21 the local MOEX override returns
`reason_code=moex_dsvd_cancelled_platform_update`, `market_open=false`, and
`data_only_collection_allowed=false`. Start must be rejected and no calibration streams
should run.

## Session close behavior

Weekday data-only windows are half-open in MSK:
`[07:00,10:00)` for `weekday_morning`, `[10:00,19:00)` for
`weekday_main`, and `[19:00,23:50)` for `weekday_evening`. Exact boundaries
belong to the new interval: `10:00:00` is main/hour 10 and `19:00:00` is
evening/hour 19. `micro_session_id` is deterministic from MSK timestamp,
session type, and floored hour bucket.

If an operator clicks Start before the next same-day window and the wait is
within `DATA_SHADOW_START_ARMING_MAX_WAIT_HOURS`, the command is armed as
`armed_until_next_window`. Streams remain stopped until fresh preflight at the
window open. Manual Stop cancels armed and active daily collection.

The collector must not keep writing calibration rows after the fresh preflight
window closes. On each runtime cycle, trade-core rechecks the current data-only
preflight context. If the current time is outside `current_window_start_at` /
`current_window_end_at`, it emits `data_only_shadow_collection_window_closed`. If
another same-day window exists it stops streams/polling, emits
`data_only_shadow_collection_paused_until_next_window`, and keeps
`stream_alive=false` while `daily_collection_active=true`. If no same-day window
remains, it emits `data_only_shadow_collection_auto_stopped` and
`data_only_shadow_collection_day_complete`. No `market_microstructure_snapshot` or
`order_book_summary` primary rows may be written while paused, between windows, or
after final close.

Known-invalid primary market-data rows are purged, not merely hidden by
`not_for_calibration` flags. If a run produced `market_microstructure_snapshot`
or `order_book_summary` rows after the session close, during an official exchange
closed override, in OTC/dealer/indicative mode, from stale/local history, or with
bugged session context, those primary rows must be removed from calibration and
logging tables after a purge manifest is written. Incident evidence stays in
`.local` reports and `audit_event`; the invalid primary rows do not remain as
calibration inputs.

Deterministic metadata errors, such as a wrong hour bucket or
`micro_session_id` on otherwise valid market rows, may be repaired with a
manifest. Invalid market values, including crossed books and negative spreads,
are purged from primary calibration/logging rows. Use
`scripts/run_repair_data_shadow_quality_issues.py --date YYYY-MM-DD --dry-run`
first, then `--apply` only after reviewing the manifest. The script writes
`data_only_quality_rows_repaired` and `data_only_invalid_rows_purged` audit
events and refuses mass changes by default.

Use the protected purge CLI for known late data-shadow rows:

```bash
python scripts/run_purge_invalid_data_shadow_rows.py --date TODAY --reason late_after_session_close_bug --dry-run --json-output
python scripts/run_purge_invalid_data_shadow_rows.py --date TODAY --reason late_after_session_close_bug --apply --json-output
```

The purge must record `data_only_invalid_rows_purged` in `audit_event` and must
not delete valid rows before the cutoff, `audit_event`, `robot_command`, or
historical candle backfill data.

Forward writes are guarded as well. Crossed books, negative spreads, invalid
mid/spread/depth/imbalance, missing bid/ask, outside-session rows, and
non-calibration display sources are rejected before
`market_microstructure_snapshot`/`order_book_summary` persistence. Rejected
microstructure emits rate-limited `data_only_microstructure_row_rejected`
audit_event records with the rejection reason.
## Calibration/Risk Hardening Rules

Before any future strategy shadow:

- Resolve the full core universe through broker/SDK or a previously resolved
  `instrument_registry` cache: `SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR`.
- Treat `lot_size` and `min_price_increment` as broker/registry facts, not env
  defaults. Env instruments identify tickers only.
- Unknown `lot_size` blocks entry risk with `instrument_lot_size_unknown`.
- Unknown `min_price_increment` blocks limit entry risk and rejects execution
  before broker boundary with `price_tick_invalid`.
- Notional/exposure uses `price_per_share * lot_qty * lot_size`.
- Limit order prices are normalized to tick before broker boundary: BUY floors,
  SELL ceils.
- EXIT candidates reduce existing positions and must not be blocked by position
  limit when projected lots decrease. Exit without position or oversized exit is
  blocked explicitly.
- Short entry permission is fail-closed when account or instrument permission is
  unknown. Short exits that reduce risk remain allowed by the short-permission
  gate.
- Core market-state freshness uses both `received_ts` and `exchange_ts`; stale or
  missing exchange time blocks entries.
- Local weekday calendar defaults are advisory. Broker `TradingSchedules` /
  trading status and official exchange overrides are authoritative for
  real/shadow trading gates. Data-only may use market-data probe fallback for
  logging visibility, but that fallback must keep `trading_allowed=false`.
- TODO: add a full MOEX holiday-calendar source/sync job before relying on local
  calendar defaults beyond advisory diagnostics.

Daily trend / forward-return research is retrospective only. It must expose
requested vs actual horizon and exclude `horizon_mismatch` windows from top/worst
candidate lists.

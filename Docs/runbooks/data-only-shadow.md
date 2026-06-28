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
- session context: `trading_date`, `session_type`, `session_phase`, `micro_session_id`;
- `source=data_only_shadow`.

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

If preflight is unavailable, the UI shows `preflight_unavailable` and does not call
`POST /robot/start`. If `market_open=false` or
`data_only_collection_allowed=false`, the UI shows `blocked_by_preflight`, the
`reason_code` and `next_session_at` when available. The frontend does not call
`POST /robot/start` in this state; trade-core does not start streams and no data-only
runtime command is created from the closed-market click.

The Start button must show an animated progress state while preflight/start is in
flight. A disabled button without command feedback is a UI bug. The command strip
must show the current phase, operator message, reason code and next session time when
available.

Stop remains a controlled operator command. In data-only mode it stops/cancels market
stream tasks, moves collector state to `stopped_by_operator`, and shows the result in
the command status strip.

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
`DashboardMarketFeedService` through `/dashboard/market-feed/snapshot` and
`/dashboard/market-feed/status`; Start is only for persistent logging.

Dashboard Live Feed may call readonly T-Invest methods (`GetLastPrices`,
`GetOrderBook`, `GetTradingStatus`, last trades/status display) with bounded timeouts.
It must not write
`market_microstructure_snapshot` calibration logs, create trading entities, or call
`PostOrder`/`CancelOrder`. `GET /market/overview` is the cheap BFF read-model backed
by that feed cache first, then stored `order_book_summary`, `market_candle` and
previous-close fallbacks. It must return one row per core universe instrument and
expose `last_price_source`, `quote_status`, `is_price_stale` and timestamp.

Readonly broker quote refresh remains explicit for diagnostics:
`POST /market/quotes/refresh` may call T-Invest `GetLastPrices`/`GetOrderBook` with
bounded timeouts. Temporary request failures must not clear already displayed
frontend quotes. Successful readonly quote/feed rows are cached briefly by the API
and overlaid into subsequent `GET /market/overview` responses so a page reload does
not fall back to stale candle rows immediately after a live broker refresh.

Operator dashboard polling while open:

- `/dashboard/market-feed/snapshot` quote board: every 2 seconds;
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
readonly, stops on operator Stop, and must not call `PostOrder`/`CancelOrder` or
create trading entities.

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

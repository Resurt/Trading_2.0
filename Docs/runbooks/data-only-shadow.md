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
`api_trade_available`, `per_instrument_status` and `source`.

If `market_open=false` and `market_closed_expected=true`, the smoke must not start market streams,
must not subscribe to order book, and must not call the data-only runtime. The JSON result should
pass safety checks and include `warning=market_closed_expected_no_live_samples`,
`post_order_calls=0`, `cancel_order_calls=0`, `signal_candidates_delta=0`,
`order_intents_delta=0`, `broker_orders_delta=0`, and
`microstructure_snapshots_delta=0`.

Weekend handling:

- broker `TradingSchedules` is authoritative when available;
- a broker trading day on Saturday/Sunday is classified as `session_type=weekend`;
- fallback weekend window is 10:00-19:00 MSK and is marked `source=fallback_weekend_time_rules`;
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

If `market_open=false`, the UI shows `blocked_by_preflight`, the `reason_code` and
`next_session_at` when available. It does not submit Start automatically. Direct
API calls to `POST /robot/start` are also guarded and return a rejected
`RobotCommandResponse` instead of starting streams.

The Start button must show an animated progress state while preflight/start is in
flight. A disabled button without command feedback is a UI bug. The command strip
must show the current phase, operator message, reason code and next session time when
available.

Stop remains a controlled operator command and shows its result in the command status strip.

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
open. The manual CLI remains useful for morning preflight and troubleshooting.

## Dashboard quotes

The Live Dashboard must show the core universe prices even when live collection is not
running. `/market/overview` uses live order-book mid price when available and falls
back to the latest stored `1m` candle close when the market is closed or order book
samples have not been collected yet.

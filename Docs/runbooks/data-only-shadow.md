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

# Production Checklist

## Strict Dividend Sync Gate

Production preflight requires the latest `dividend_sync_run` to be clean:

- `status=completed`;
- `clean=true`;
- `failed_instruments=0`;
- `error_count=0`;
- age is within `--max-dividend-sync-age-hours`.

`completed_with_errors`, `failed`, stale sync, and unavailable dividend calendar are
launch blockers. `TRADING_DIVIDEND_SYNC_FAIL_OPEN=true` is rejected by production
preflight unless an explicit override flag is used.

Production mode is not default. Start only after every item is green.

## Required Env

```powershell
$env:TRADING_RUNTIME_MODE = "production"
$env:TRADING_PRODUCTION_CONFIRM = "I_UNDERSTAND_LIVE_ORDERS"
$env:TBANK_ENVIRONMENT = "live"
$env:SSL_TBANK_VERIFY = "true"
$env:TBANK_UNARY_TIMEOUT_FLOOR_SECONDS = "5.0"
$env:TRADING_AUTH_MODE = "static_bearer"
```

## Final Live Checklist

- Docker Compose secrets exist for `tbank_full_access_token`, `tbank_readonly_token`, `postgres_password`, `grafana_admin_password`.
- Static API bearer tokens are configured through `TRADING_API_*_TOKEN_FILE` and `TRADING_WS_TICKET_SECRET_FILE`; production must reject `X-API-Role`-only requests.
- `python scripts/run_launch_readiness.py --mode production-preflight` is green.
- T-Bank SDK TLS verification is enabled with `SSL_TBANK_VERIFY=true`; Russian Trusted Root/Sub CA is trusted in the runtime environment or T-Invest endpoints are excluded from HTTPS inspection.
- No real token is present in git, `.env`, docs, shell history snippets, or CI config.
- `python scripts/run_controlled_launch_acceptance.py` is green locally or in a staging workspace.
- `python -m alembic upgrade head` is applied.
- `trade-core`, `api`, `report-worker`, `report-worker-health`, `frontend`, `postgres`, `redis` are healthy.
- Prometheus, Grafana, Loki and Fluent Bit are reachable.
- Grafana dashboards are provisioned.
- `docker compose exec -T report-worker celery -A report_worker.celery_app.celery_app inspect ping` returns at least one worker response.
- `report-worker-health` exposes `/health` and `/metrics`; Prometheus scrapes `report-worker-health:8002`.
- `make report-worker-smoke` completes a queued `build_hourly_report` result in Redis before live start.
- Report worker is running and can build hourly/daily reports without FastAPI BackgroundTasks.
- `TRADING_RUNTIME_MODE=production` is visible in service health/log context.
- `trade-core` startup log/audit shows `database_backend=postgresql` and a redacted Postgres URL shared with `api` and `report-worker`.
- `SBER`/`GAZP` are resolved through the broker instruments API; no `runtime-placeholder` instrument UID remains.
- Historical candle backfill has been run for the configured instruments, raw `1m`
  and derived `5m/10m/15m` `market_candle` rows are present, and replay/report
  calibration checks were reviewed before enabling live orders.
- T-Bank dividend sync through `GetDividends` has been run for the historical calibration
  period and future window; manual CSV/JSON is fallback/override only.
- Corporate action classification has been run with future dividend windows included.
- Primary calibration uses `calibration_scope=primary_normal_days` and has
  `calibration_clean=true`.
- Dividend/corporate-action days are excluded from primary calibration by default or
  reviewed separately with `calibration_scope=special_days_only`.
- Execution thresholds are confirmed by shadow live data, not only historical candles.
- Risk limits and max position limits are reviewed for the session template.
- `allow_long`, `allow_short`, `max_long_lots`, `max_short_lots`,
  `max_gross_exposure_rub` and `max_net_exposure_rub` are reviewed per
  instrument/timeframe override.
- Short selling is enabled only if broker/account/instrument availability and
  margin/collateral are explicitly confirmed.
- Cost assumptions are reviewed: commission not lower than `5 bps` per side,
  spread included from market state, slippage assumption set.
- `min_edge_after_total_costs_bps` is non-negative and documented for the
  active strategy version.
- Session manager shows the correct `session_type`, `session_phase`, `trading_date`, `calendar_date`.
- Alerts for stream freshness, rejected orders, report backlog and health are active.
- Operator stop path through `POST /robot/stop` is tested.
- `emergency_stop` cancellation path is tested: working/submitted/partially-filled orders receive `cancel_reason_code=manual_operator_emergency_stop`; failures put runtime into `degraded`.
- Production preflight must fail if dividend sync is older than the configured threshold,
  if future dividend windows are not classified, or if dividend calendar is unavailable
  while `TRADING_DIVIDEND_SYNC_FAIL_OPEN=false`.

## Start

```powershell
docker compose up -d --build
python -m alembic upgrade head
docker compose ps
```

## Abort Conditions

- Missing dashboard or health endpoint.
- Broker trading status mismatch.
- Stale market data.
- Report backlog growing.
- Unknown blocker/reject/cancel reason.
- Any uncertainty about token/account environment.

## Historical calibration gate

Перед controlled minimal live нужно зафиксировать результаты исторического
прогона:

- backfill минимум `90d` по активным инструментам завершён без placeholder
  `instrument_uid`;
- `historical_data_quality_report.coverage_pct` и invalid OHLC reviewed;
- `historical_db_replay` повторно проходит идемпотентно без дублей;
- `counterfactual_result` построен для blocked/cancelled/rejected
  opportunities по горизонтам `+5m/+10m/+15m`;
- `historical_report_rebuild` построил hourly/daily reports по
  `session_type`, `instrument_id`, `timeframe`;
- `calibration_report` содержит blocker ranking, missed opportunity summary,
  gross/net PnL proxy, cost sensitivity и recommendations;
- ни один historical/shadow replay шаг не вызвал real `PostOrder` или
  `CancelOrder`.

Проверка:

```powershell
python scripts/run_launch_readiness.py --mode historical-replay
python scripts/run_launch_readiness.py --mode historical-final-calibration
```

## Instrument Registry Gate

Production preflight requires clean T-Bank instrument resolution:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
python scripts/run_launch_readiness.py --mode production-preflight
```

Fail production if any enabled row in `instrument_registry` is still
`source=seed`, `source=safe_noop`, `resolution_status!=resolved`, or has no
`instrument_uid`/`figi`. `MOEX:*` is an internal canonical id and is never a valid
broker id for real T-Bank calls.

## Data-only Shadow Evidence

Production preflight must not treat candle-only historical research as execution calibration.
Before any strategy shadow or live attempt, collect data-only shadow evidence for spread, depth,
imbalance, freshness, stream gaps and latency:

```powershell
set TRADING_DATA_ONLY_SHADOW=true
python scripts/run_data_shadow_summary_report.py --lookback-hours 6 --json-output
python scripts/run_launch_readiness.py --mode data-shadow --instruments SBER,GAZP
```

Data-only shadow is readonly. It cannot be used to justify live orders by itself; it only supplies
microstructure inputs required for later spread/depth/slippage calibration.

## Analytics and Calibration Center Gate

Before any strategy shadow or live consideration, run diagnostic-only analytics:

```powershell
python scripts/run_intraday_analytics.py --date YYYY-MM-DD --mode data_shadow --json-output
python scripts/run_calibration_observatory.py --universe SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --lookback-days 20 --mode data_shadow --json-output
```

Required review:

- Intraday Analytics explains session/hour/instrument activity and no-trade reasons.
- Calibration Center diagnosis is reviewed with warnings and blocking issues visible.
- Small sample warnings are not hidden.
- 10-20 trading days are early evidence, not final truth.
- No timeframe/session/side/instrument contour is permanently disabled from low sample alone.
- Any `strategy_config_candidate` remains proposal storage only.
- `approved_for_shadow` does not apply live config and does not start strategy shadow.
- Applying an actual config remains a separate operator/admin-approved future workflow.

Production remains blocked if any process attempts to treat Calibration Center output as automatic
runtime config or as permission for real `PostOrder`/`CancelOrder`.

## Session preflight and balance visibility

Production, strategy shadow and live trading remain forbidden until both are true:

- clean session/calendar preflight exists for the target universe;
- broker balance visibility is present on `/robot/status` and `/portfolio/summary`.

`market_closed_expected` must not be treated as a strategy failure. A live data-only smoke must not start streams before session preflight. Production is forbidden without clean session calendar/preflight and balance visibility.

# Production Checklist

Production mode is not default. Start only after every item is green.

## Required Env

```powershell
$env:TRADING_RUNTIME_MODE = "production"
$env:TRADING_PRODUCTION_CONFIRM = "I_UNDERSTAND_LIVE_ORDERS"
$env:TBANK_ENVIRONMENT = "live"
```

## Final Live Checklist

- Docker Compose secrets exist for `tbank_full_access_token`, `tbank_readonly_token`, `postgres_password`, `grafana_admin_password`.
- No real token is present in git, `.env`, docs, shell history snippets, or CI config.
- `python -m alembic upgrade head` is applied.
- `trade-core`, `api`, `report-worker`, `report-worker-health`, `frontend`, `postgres`, `redis` are healthy.
- Prometheus, Grafana, Loki and Fluent Bit are reachable.
- Grafana dashboards are provisioned.
- `docker compose exec -T report-worker celery -A report_worker.celery_app.celery_app inspect ping` returns at least one worker response.
- `report-worker-health` exposes `/health` and `/metrics`; Prometheus scrapes `report-worker-health:8002`.
- `make report-worker-smoke` completes a queued `build_hourly_report` result in Redis before live start.
- Report worker is running and can build hourly/daily reports without FastAPI BackgroundTasks.
- `TRADING_RUNTIME_MODE=production` is visible in service health/log context.
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

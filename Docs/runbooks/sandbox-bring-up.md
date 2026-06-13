# Sandbox Bring-Up Runbook

Цель: проверить adapter wiring, health, domain events, logs и reports без реальных денег.

## Preconditions

- `TRADING_RUNTIME_MODE=sandbox`.
- T-Bank sandbox token лежит в Docker Compose secret или локальном ignored `secrets/`.
- Production confirm не задан.
- Risk limits минимальные.

## Start

```powershell
Copy-Item .env.example .env
$env:TRADING_RUNTIME_MODE = "sandbox"
python scripts/run_sandbox_smoke.py --dry-run
docker compose up -d --build
python -m alembic upgrade head
```

## Checks

```powershell
docker compose ps
Invoke-WebRequest http://localhost:8001/health
Invoke-WebRequest http://localhost:8000/health
Invoke-WebRequest http://localhost:8002/health
Invoke-WebRequest http://localhost:9090/-/healthy
Invoke-WebRequest http://localhost:3100/ready
```

## Broker Adapter Smoke

- Confirm `TBankBrokerConfig.environment=sandbox`.
- Fetch schedules/status with sandbox credentials.
- Submit only controlled sandbox smoke orders from explicit test tooling.
- Reconcile order state and verify `request_order_id`.
- Build hourly report from sandbox events.

## Exit Criteria

- No live account/token was used.
- PostgreSQL has domain events.
- Loki has technical logs.
- Prometheus/Grafana are reachable.
- Sandbox results are labeled as sandbox and not used as real execution-quality evidence.

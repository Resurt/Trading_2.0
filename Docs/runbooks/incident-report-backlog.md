# Incident: Report Backlog

Цель: восстановить hourly/daily analytics без запуска тяжелых отчетов в FastAPI.

## Detect

- Celery queue length grows.
- Hourly reports are missing after `report_requested`.
- Daily report rebuild does not finish.
- Grafana report-worker health is degraded.

## Immediate Actions

```powershell
docker compose ps report-worker redis
docker compose logs report-worker --tail=300
docker compose logs redis --tail=100
```

- Keep API serving read models; do not calculate heavy reports in API.
- Check Redis connectivity and worker process health.
- Restart only `report-worker` if it is stuck; do not restart `trade-core` for backlog.
- Re-run reports manually after worker is healthy:

```powershell
python scripts/run_hourly_report.py --micro-session-id <micro_session_id> --strategy-id baseline
python scripts/run_daily_report.py --trading-date <YYYY-MM-DD> --strategy-id baseline
python scripts/run_counterfactual.py --trading-date <YYYY-MM-DD> --strategy-id baseline
```

## Evidence

- Celery task id.
- `micro_session_id`, `trading_date`, `strategy_id`.
- `report_requested` state event.
- report-worker logs and exception reason.

## Recovery Criteria

- Hourly reports exist for closed micro-sessions.
- Daily report has fresh `generated_at`.
- Counterfactual rows are present for blocked/cancelled sources.
- Queue length returns to normal.

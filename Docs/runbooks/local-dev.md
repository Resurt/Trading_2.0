# Local Development Runbook

## Purpose

Run the local stack with fake/dev secrets and no real trading keys.

## Expected Commands

Commands available after the repository bootstrap:

```bash
make test
make lint
make frontend-build
```

The compose commands are placeholders until step 02:

```bash
make up
make logs
make down
```

On Windows, use `npm.cmd` if PowerShell blocks `npm.ps1`:

```powershell
cd apps/frontend
npm.cmd run build
```

Single-command local smoke check:

```powershell
python scripts/check.py
```

## Secret Policy

Use local development fallback only with fake/sample values. Do not commit real tokens, `.env` files with real secrets, or generated credentials.

Production-like secret names:

- `tbank_full_access_token`
- `tbank_readonly_token`
- `postgres_password`
- `grafana_admin_password`

## Validation Checklist

- API health endpoint is green.
- `trade-core` health endpoint is green.
- `report-worker` starts and can receive a test task.
- PostgreSQL migrations apply.
- Prometheus can scrape services.
- Grafana has Prometheus and Loki datasources.

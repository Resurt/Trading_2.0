# Local Development Runbook

## Purpose

Run the local stack with fake/dev secrets and no real trading keys.

## Expected Commands

These commands will be implemented during repository bootstrap and compose setup:

```bash
make up
make logs
make test
make lint
make down
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

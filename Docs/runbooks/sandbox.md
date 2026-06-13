# Sandbox Runbook

## Purpose

Validate infrastructure, broker adapter behavior, session handling, reporting, and observability without risking real funds.

## Preconditions

- Sandbox-compatible T-Bank token is configured through secrets or local dev fallback.
- Production trading mode is disabled.
- Risk limits are minimal.

## Checklist

- Start stack.
- Confirm broker adapter authenticates against sandbox.
- Fetch schedules and instrument metadata.
- Run market data flow if available in sandbox mode.
- Submit a controlled sandbox order only from an explicit test command.
- Verify order lifecycle, reconciliation, domain events, technical logs, and metrics.
- Build hourly report from sandbox events.

## Exit Criteria

- No real secret or real account is used.
- Domain events are written to PostgreSQL.
- Technical logs are visible in Loki.
- Metrics are visible in Grafana.

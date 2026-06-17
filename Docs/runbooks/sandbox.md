# Sandbox Runbook

## Purpose

Validate infrastructure, broker adapter behavior, session handling, reporting, and observability without risking real funds.

## Preconditions

- Sandbox-compatible T-Bank token is configured through secrets or local dev fallback.
- Production trading mode is disabled.
- Risk limits are minimal.
- T-Bank SDK extra is installed: `python scripts/run_tbank_sdk_import_check.py` must pass.
- Real sandbox orders are disabled unless `TRADING_SANDBOX_ORDERS_CONFIRM=I_UNDERSTAND_SANDBOX_ORDERS`.

## Checklist

- Start stack.
- Confirm `trade-core`, `api` and `report-worker` use the same PostgreSQL database.
- Confirm broker adapter authenticates against sandbox.
- Resolve `SBER`/`GAZP` through T-Bank instruments API and verify no placeholder `instrument_uid` remains.
- Fetch schedules, accounts, trading status, candles and order book in readonly mode.
- Run market data flow if available in sandbox mode.
- Submit a controlled sandbox order only from an explicit test command.
- Verify order lifecycle, reconciliation, domain events, technical logs, and metrics.
- Build hourly report from sandbox events.

## Commands

```powershell
$env:TRADING_RUNTIME_MODE = "sandbox"
$env:TBANK_ENVIRONMENT = "sandbox"
python scripts/run_tbank_sdk_import_check.py
python scripts/run_launch_readiness.py --mode sandbox
```

Do not run a sandbox `PostOrder` smoke unless the confirmation variable is set deliberately:

```powershell
$env:TRADING_SANDBOX_ORDERS_CONFIRM = "I_UNDERSTAND_SANDBOX_ORDERS"
```

## Exit Criteria

- No real secret or real account is used.
- Domain events are written to PostgreSQL.
- Technical logs are visible in Loki.
- Metrics are visible in Grafana.

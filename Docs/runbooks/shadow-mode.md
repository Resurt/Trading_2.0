# Shadow Mode Runbook

## Purpose

Run on live market data without real order submission. Shadow mode must write the same analytics spine as production: candidates, blockers, pseudo-order intents, risk events, reports, and counterfactual results.

## Behavior

- Market data is live.
- Strategy and risk logic run normally.
- Execution creates pseudo-orders only.
- No real `PostOrder` call is made.
- No real `CancelOrder` call is made for pseudo-orders.
- T-Bank SDK extra must be installed and instruments must be resolved to real `instrument_uid` values before live market streams start.
- Long/short gates run normally: `allow_short=false` blocks short candidates
  with `short_not_allowed_by_config`, while long candidates still pass through
  cost/exposure/session gates.
- Cost gate uses commission not lower than `5 bps` per side, plus spread and
  slippage assumptions.
- Reports and counterfactual analysis run as in production.

## Validation Checklist

- `python scripts/run_launch_readiness.py --mode shadow` is green for replay/report determinism before a live shadow run.
- `python scripts/run_controlled_launch_acceptance.py --skip-full-check` is green before starting.
- Live dashboard shows market state.
- Candidate funnel is populated.
- Blocker reasons are structured.
- Blocked opportunities have both `signal_candidate_created` market snapshot
  and `counterfactual_seed_snapshot`.
- Pseudo-order lifecycle is visible.
- Hourly and daily reports can be built.
- Counterfactual windows are populated after enough market data exists.

## Exit Criteria

- A full trading day can be explained from PostgreSQL domain events.
- Morning/main/evening/weekend segments are not mixed.
- No real order submission occurred.

## Start

```powershell
$env:TRADING_RUNTIME_MODE = "shadow"
$env:TBANK_ENVIRONMENT = "live"
$env:SSL_TBANK_VERIFY = "true"
$env:TBANK_UNARY_TIMEOUT_FLOOR_SECONDS = "5.0"
python scripts/run_tbank_sdk_import_check.py
docker compose up -d --build trade-core api report-worker frontend
python -m alembic upgrade head
```

## Stop

```powershell
Invoke-RestMethod -Method Post -Headers @{ "X-API-Role" = "operator" } http://localhost:8000/robot/stop
docker compose logs trade-core --tail=200
```

Do not kill `trade-core` for hourly rollovers. Micro-sessions are logical and must close in-process.

## Evidence Checks

- `order_intent.intent_payload.real_broker_call=false`.
- `broker_order.broker_payload.data.real_broker_call=false`.
- `broker_order.broker_status=pseudo_posted` for pseudo submissions.
- `cancel_reason_code` is present for any pseudo cancellation.
- Daily reports and counterfactual rows are buildable after market data is available.
- `stream_gap_recovery_requested/completed` events appear after reconnect tests.
- `position_snapshot` rows are written on micro-session boundaries and before risk-sensitive decisions.

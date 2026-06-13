# Shadow Mode Runbook

## Purpose

Run on live market data without real order submission. Shadow mode must write the same analytics spine as production: candidates, blockers, pseudo-order intents, risk events, reports, and counterfactual results.

## Behavior

- Market data is live.
- Strategy and risk logic run normally.
- Execution creates pseudo-orders only.
- No real `PostOrder` call is made.
- Reports and counterfactual analysis run as in production.

## Validation Checklist

- Live dashboard shows market state.
- Candidate funnel is populated.
- Blocker reasons are structured.
- Pseudo-order lifecycle is visible.
- Hourly and daily reports can be built.
- Counterfactual windows are populated after enough market data exists.

## Exit Criteria

- A full trading day can be explained from PostgreSQL domain events.
- Morning/main/evening/weekend segments are not mixed.
- No real order submission occurred.

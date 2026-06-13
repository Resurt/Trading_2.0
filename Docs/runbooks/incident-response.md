# Incident Response Runbook

## First Checks

1. Identify affected service: `trade-core`, `api`, `report-worker`, database, Redis, broker transport, frontend.
2. Check Grafana dashboards for health, latency, reconnects, stale data, rejected orders, and report failures.
3. Use correlation IDs: `run_id`, `micro_session_id`, `candidate_id`, `order_intent_id`, `request_order_id`, `exchange_order_id`.
4. Query PostgreSQL domain events for the trading facts.
5. Query Loki for technical details around the same IDs.

## Trading Safety

- Prefer controlled stop through API/runbook over killing containers.
- Do not restart `trade-core` just because a micro-session boundary is near.
- If broker status is unknown, treat execution as forbidden until confirmed.
- If market data is stale, freeze new entries.

## Evidence To Preserve

- service logs around incident window;
- relevant domain events;
- order lifecycle records;
- broker tracking ids;
- report task ids;
- Grafana screenshots or exported panel data when useful.

## Post-Incident Follow-Up

- Add or refine reason codes if the incident was not classifiable.
- Add replay fixture or test for the failure mode.
- Update docs and runbooks in the same change as the fix.

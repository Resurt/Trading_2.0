# Incident: Stream Broken

Цель: восстановить market/order streams без смешивания session logs и без физического hourly restart `trade-core`.

## Detect

- `market_stream_alive=0`.
- `last_closed_candle_age_seconds` растет.
- `reconnect_total` растет быстрее обычного.
- Dashboard показывает stale market data.

## Immediate Actions

```powershell
docker compose logs trade-core --tail=300
Invoke-RestMethod http://localhost:8000/robot/status
```

- Freeze new entries through session/risk policy if data is stale.
- Do not submit new orders while broker stream status is unknown.
- Trigger gap recovery hooks: backfill candles, refresh open orders, refresh positions.
- Keep `trade-core` alive unless the process is unhealthy; do not restart for micro-session rollover.

## Evidence

- `run_id`, `micro_session_id`, `instrument_id`, `stream_name`.
- Loki logs around reconnect window.
- `audit_event` reconnect rows.
- Market candles/status snapshots before and after gap.

## Recovery Criteria

- `market_stream_alive=1`.
- Closed candle delivery lag is back within threshold.
- Open orders and positions reconciled.
- A report can be built for the affected micro-session.

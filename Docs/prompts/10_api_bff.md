# Step 10 Prompt: FastAPI BFF

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: реализовать FastAPI BFF для live-торговли, управления и отчётов.

Сделай REST endpoints из `Docs/api-contract.md`.

Сделай WebSocket каналы:

- `/ws/dashboard`
- `/ws/orders`
- `/ws/market`
- `/ws/reports`

На `/robot/status` отдавай balance, instruments/timeframes, strategy state, session type/phase, broker status, micro-session countdown, current blocker/candidate, PnL, positions, active orders, feed health, last report status.

Тяжёлые отчёты только через `report-worker`.

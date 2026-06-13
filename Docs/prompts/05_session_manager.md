# Step 05 Prompt: Session Manager

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: реализовать session manager и модель hourly micro-sessions.

Сделай:

- `SessionManager`;
- `HourlyMicroSessionManager`;
- `session_type`: `weekend`, `weekday_morning`, `weekday_main`, `weekday_evening`;
- `session_phase`: `opening_auction`, `continuous_trading`, `closing_auction`, `break`, `dealer_mode`, `closed`;
- источник правды: `TradingSchedules` + `GetTradingStatus`/`Info` stream;
- micro-session по биржевому часу, а не по времени запуска процесса;
- freeze new entries за configurable 60-90 секунд;
- snapshot состояния на границе;
- `session_run_closed`;
- enqueue report task;
- тесты partial-hour start, hourly boundary, exchange-session boundary, weekend, auction/break phases.

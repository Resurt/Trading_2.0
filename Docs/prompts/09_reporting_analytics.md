# Step 09 Prompt: Reporting And Counterfactual Analytics

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: реализовать report-worker, hourly/daily analytics и counterfactual разбор.

Сделай:

- Celery + Redis для `report-worker`;
- задачи `build_hourly_report`, `build_daily_report`, `rebuild_reports_for_date`, `run_counterfactual_analysis_for_date`;
- CLI `scripts/run_hourly_report.py`, `scripts/run_daily_report.py`, `scripts/run_counterfactual.py`;
- hourly report с PnL, комиссиями, slippage, signals, entries/exits, fill ratio, rejects/cancels/replaces, reconnects, API/broker errors, risk blockers, stale data, missed candles, drawdown, idle time, latency histograms;
- daily report с market regime, candidate funnel, blocker ranking, execution quality, counterfactual, session segmentation, infra health;
- тесты на отчёты и counterfactual windows.

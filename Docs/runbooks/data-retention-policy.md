# Data Retention Policy

This policy records the recommended future retention windows for analytics data. It does not
delete existing data and does not enable any cleanup job by itself.

## Recommended Retention

| Data set | Retention |
| --- | --- |
| raw `market_microstructure_snapshot` | 30-60 days |
| aggregated microstructure `1m` / `5m` / `15m` | 1-2 years |
| raw `market_candle` `1m` | 1-2 years |
| derived bars `5m` / `10m` / `15m` | 3+ years |
| decision journal (`signal_candidate`, stages, blockers, intents, orders, counterfactuals) | minimum 2-3 years |
| calibration reports and diagnostic runs | indefinite |
| rolling performance cube | indefinite |

## Cleanup Rules

- Do not delete data now.
- First implementation must be an explicit/manual cleanup job.
- The first cleanup job must support dry-run output with row counts by table and date range.
- Automatic schedules can be considered only after manual cleanup has been reviewed.
- Cleanup must never remove `calibration_report`, `calibration_diagnostic_run`,
  `strategy_config_candidate` or `rolling_performance_cube` rows by default.
- Raw microstructure cleanup must preserve already-built aggregate rows.
- Cleanup must be documented in an operator runbook before use.

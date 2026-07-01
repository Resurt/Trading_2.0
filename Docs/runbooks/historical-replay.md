# Historical Replay Runbook

## Назначение

Historical replay превращает уже загруженные `market_candle` в полноценный
decision journal для калибровки стратегии:

- проверяет качество исторических свечей;
- прогоняет closed bars `5m/10m/15m` через `ConfigDrivenStrategyEngine`;
- пишет `signal_candidate`, `candidate_stage_result`, `blocker_event`,
  `risk_event`, `order_intent`, pseudo `broker_order`, `order_state_event`;
- пересчитывает counterfactual `+5m/+10m/+15m`;
- строит hourly/daily reports и calibration report.

Replay всегда работает в `historical_replay` mode и не вызывает real
`PostOrder`/`CancelOrder`.

## Порядок запуска

1. Dry-run загрузки свечей за 10 дней:

```powershell
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP --lookback-days 10 --dry-run
```

2. Readonly backfill за 10 дней:

```powershell
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP --lookback-days 10
```

3. Quality report:

```powershell
python scripts/run_historical_data_quality_report.py --lookback-days 10 --instruments SBER,GAZP --timeframes 1m,5m,10m,15m --json-output
```

4. Corporate actions import and special-day classification:

```powershell
python scripts/run_corporate_actions_import.py --file data/corporate_actions/sample_dividends.csv --source manual --json-output
python scripts/run_market_special_day_classification.py --lookback-days 10 --instruments SBER,GAZP --json-output
```

5. Final quality report with classification required:

```powershell
python scripts/run_historical_data_quality_report.py --lookback-days 10 --instruments SBER,GAZP --timeframes 1m,5m,10m,15m --require-special-day-classification --json-output
```

6. Replay from DB:

```powershell
python scripts/run_historical_replay_from_db.py --lookback-days 10 --instruments SBER,GAZP --timeframes 5m,10m,15m --strategy-id baseline --json-output
```

Replay now loads active `strategy_config` / `risk_limits` from PostgreSQL. If config is
missing, replay fails by default. `--allow-default-strategy-config` is allowed only for
explicit local dry-runs.

7. Counterfactual rebuild:

```powershell
python scripts/run_historical_counterfactual_rebuild.py --lookback-days 10 --strategy-id baseline --instruments SBER,GAZP --timeframes 5m,10m,15m --json-output
```

8. Historical reports:

```powershell
python scripts/run_historical_report_rebuild.py --lookback-days 10 --strategy-id baseline --include-counterfactual --json-output
```

9. Calibration report:

```powershell
python scripts/run_calibration_report.py --lookback-days 10 --strategy-id baseline --instruments SBER,GAZP --timeframes 5m,10m,15m --calibration-scope primary_normal_days --require-special-day-classification --json-output
```

10. Acceptance gate:

```powershell
python scripts/run_launch_readiness.py --mode historical-replay --dry-run
```

## Session Classification

Historical candles получают fallback-классификацию, если нет broker schedule:

- `weekday_morning`: 07:00-10:00 MSK;
- `weekday_main`: [10:00,19:00) MSK;
- `weekday_evening`: 19:00-23:50 MSK;
- `weekend`: отдельный weekend contour;
- вне окна: `session_phase=closed`;
- внутри окна: `session_phase=continuous_trading`.

`micro_session_id` имеет формат:

```text
historical:{trading_date}:{session_type}:{HH}
```

Пример:

```text
historical:2026-06-18:weekday_main:10
```

## Idempotency

Replay использует deterministic candidate fingerprint:

```text
strategy_id|strategy_version|instrument_id|timeframe|bar_close_ts|side|action|historical_db_replay
```

Повторный запуск без `--reset-derived-events` не дублирует:

- `signal_candidate`;
- `candidate_stage_result`;
- `blocker_event`;
- `risk_event`;
- `order_intent`;
- `broker_order`;
- `order_state_event`;
- `counterfactual_result`.

`--reset-derived-events` удаляет только события с `source=historical_db_replay`
за выбранный период и стратегию. Live/shadow/sandbox события не удаляются.

## Ограничения

- Historical replay не является оценкой реального execution quality.
- Strategy config автоматически не меняется.
- Calibration recommendations сохраняются только в payload.
- Technical JSON logs не являются источником аналитики; источник аналитики -
  PostgreSQL domain tables.

## Corporate Actions / Special Days

Historical replay исключает `dividend_gap_day` и `corporate_action_day` из primary replay
по умолчанию. Поведение можно изменить только явно:

- `--include-special-days`;
- `--include-dividend-gap-days`;
- `--include-corporate-action-days`;
- `--special-day-policy include_with_flags|shadow_only`.

## Instrument Resolution Boundary

Historical replay from DB uses already persisted candles and never calls
`PostOrder`/`CancelOrder`. However the prerequisite data-loading steps are real
readonly broker calls.

Before real dividend sync or real historical candle backfill:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
```

Replay may still run in dry-run/local mode with seed rows, but final calibration
and shadow readiness require `source=tbank_resolved` and
`resolution_status=resolved` for enabled instruments.

В payload каждого replay-generated event попадают `special_day_type`,
`dividend_gap_day`, `corporate_action_flag`, `abnormal_gap_day`,
`excluded_from_primary_calibration` и `eligible_for_live_calibration`.

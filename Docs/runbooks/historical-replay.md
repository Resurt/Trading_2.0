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

4. Replay from DB:

```powershell
python scripts/run_historical_replay_from_db.py --lookback-days 10 --instruments SBER,GAZP --timeframes 5m,10m,15m --strategy-id baseline --json-output
```

5. Counterfactual rebuild:

```powershell
python scripts/run_historical_counterfactual_rebuild.py --lookback-days 10 --strategy-id baseline --instruments SBER,GAZP --timeframes 5m,10m,15m --json-output
```

6. Historical reports:

```powershell
python scripts/run_historical_report_rebuild.py --lookback-days 10 --strategy-id baseline --include-counterfactual --json-output
```

7. Calibration report:

```powershell
python scripts/run_calibration_report.py --lookback-days 10 --strategy-id baseline --instruments SBER,GAZP --timeframes 5m,10m,15m --json-output
```

8. Acceptance gate:

```powershell
python scripts/run_launch_readiness.py --mode historical-replay --dry-run
```

## Session Classification

Historical candles получают fallback-классификацию, если нет broker schedule:

- `weekday_morning`: 07:00-10:00 MSK;
- `weekday_main`: 10:00-18:59 MSK;
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

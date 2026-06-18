# Calibration Runbook

## Назначение

Calibration report собирает historical replay facts в отчёт для настройки
порогов стратегии. Он не меняет `strategy_config` автоматически.

Источник данных:

- `signal_candidate`;
- `candidate_stage_result`;
- `blocker_event`;
- `order_intent`;
- pseudo `broker_order`;
- `counterfactual_result`;
- `hourly_report`;
- `daily_report`;
- `market_candle`.

## Команда

```powershell
python scripts/run_calibration_report.py `
  --lookback-days 90 `
  --strategy-id baseline `
  --instruments SBER,GAZP `
  --timeframes 5m,10m,15m `
  --calibration-scope primary_normal_days `
  --require-special-day-classification `
  --group-by session_type,instrument_id,timeframe,blocker_code `
  --json-output
```

Make target:

```powershell
make calibration-report LOOKBACK_DAYS=90 STRATEGY_ID=baseline INSTRUMENTS=SBER,GAZP TIMEFRAMES=5m,10m,15m
```

## Что считает отчёт

- `candidate_count`;
- `approved_count`;
- `blocked_count`;
- `pseudo_order_count`;
- candidate funnel;
- blocker ranking;
- final blocker ranking;
- blocker false positive proxy;
- missed opportunity summary;
- avoided loss summary;
- gross simulated PnL;
- net simulated PnL;
- total assumed fees;
- total assumed slippage;
- long vs short candidate count;
- long vs short net PnL proxy;
- best/worst `session_type`;
- best/worst `timeframe`;
- best/worst `instrument`;
- cost sensitivity для fee/slippage assumptions;
- recommended threshold changes.

## Cost Model

По умолчанию для акций:

- commission: не ниже `5 bps` per side;
- round-trip fee: не ниже `10 bps`;
- slippage: configurable, default `2 bps`;
- net result = gross result - fee - slippage.

## Recommendations

Recommendations сохраняются в `calibration_report.report_payload`:

- `max_spread_bps`;
- `min_market_quality_score`;
- `min_edge_after_total_costs_bps`;
- `max_data_age_ms`;
- `allow_short`.

`allow_short` может быть только рекомендацией. Реальный `strategy_config`
меняется отдельным операторским действием через API/UI и audit trail.

## UI

Frontend имеет страницу `Calibration`:

- blocker ranking;
- candidate funnel summary;
- gross/net simulation;
- missed opportunity;
- best/worst session/timeframe/instrument;
- threshold recommendations.

## Acceptance

Минимальная локальная приёмка:

```powershell
python scripts/run_historical_data_quality_report.py --lookback-days 10 --json-output
python scripts/run_historical_replay_from_db.py --lookback-days 10 --strategy-id baseline --dry-run --json-output
python scripts/run_historical_counterfactual_rebuild.py --lookback-days 10 --strategy-id baseline --dry-run --json-output
python scripts/run_calibration_report.py --lookback-days 10 --strategy-id baseline --json-output
python scripts/run_launch_readiness.py --mode historical-replay --dry-run
```

Ограничение: если в БД ещё нет `market_candle`, replay/calibration вернут
пустые summaries. Это корректно для dry-run, но не является готовностью к
shadow/live.

## Candle-only vs Shadow Confirmation

Recommendations разделены на два контура:

- `safe_from_historical_candles`: preliminary session/timeframe/instrument/holding-horizon
  выводы, которые можно обсуждать после candle-only replay;
- `requires_shadow_confirmation`: spread, market quality, slippage, execution thresholds
  и live order policy, которые нельзя считать финальными без shadow live.

`calibration_data_mode=historical_candles_only` всегда сопровождается caveats по
`real_spread`, `order_book_depth`, `book_imbalance`, `market_quality_score`,
`real_slippage`, `broker_rejects`, `partial_fills`, `latency`.

## Corporate Actions Scope

Primary calibration по умолчанию использует `calibration_scope=primary_normal_days` и
исключает:

- `dividend_gap_day`;
- `corporate_action_day`;
- `exclude_from_primary_calibration=true`.

Special days анализируются отдельно:

```powershell
python scripts/run_calibration_report.py --lookback-days 90 --strategy-id baseline --calibration-scope special_days_only --json-output
```

Если special-day classification отсутствует, report возвращает
`calibration_clean=false`, warning `corporate_action_classification_missing` и
recommendation `run_market_special_day_classification_before_final_calibration`.

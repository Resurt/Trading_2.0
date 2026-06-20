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

Frontend имеет страницу `Calibration` / Calibration Center:

- run diagnostics action;
- diagnosis status: `market_dead`, `robot_too_strict`, `data_quality_problem`,
  `regime_changed`, `not_enough_data`, `normal_no_action_needed`,
  `calibration_recommended`;
- rolling performance cube filters by window, instrument, session, timeframe, side and mode;
- market regime summary;
- top/dead contours;
- warnings and blocking issues;
- candidate config proposals with approve/reject controls;
- blocker ranking;
- candidate funnel summary;
- gross/net simulation;
- missed opportunity;
- best/worst session/timeframe/instrument;
- threshold recommendations.

Candidate configs are not applied to live trading automatically. Approval changes only
`strategy_config_candidate.status`; applying any config to active runtime remains a separate
future operator/admin workflow.

## Calibration Observatory

CLI:

```powershell
python scripts/run_calibration_observatory.py --universe SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --lookback-days 20 --json-output
```

Optional draft proposal:

```powershell
python scripts/run_calibration_observatory.py --universe SBER,GAZP --lookback-days 20 --create-candidate-config --json-output
```

The observatory writes `.local/collection_reports/calibration_observatory/` and persists:

- `calibration_diagnostic_run`;
- `rolling_performance_cube`;
- `market_regime_snapshot`;
- optional `strategy_config_candidate` with `status=draft`.

Rules:

- no live `strategy_config` mutation;
- no production or strategy shadow startup;
- no real `PostOrder` or `CancelOrder`;
- small samples produce warnings and do not hard-disable contours;
- 10-20 trading days are early evidence, not final truth;
- operator/admin approval is required before any future workflow can apply a config.

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

## Dividend Sync Requirement

Before final calibration run broker dividend sync and special-day classification:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH --strict --json-output
python scripts/run_tbank_dividend_sync.py --lookback-days 730 --lookahead-days 365 --json-output
python scripts/run_market_special_day_classification.py --lookback-days 730 --include-future --lookahead-days 365 --require-dividend-sync --json-output
```

Manual CSV/JSON corporate actions are fallback/override only. If there are no
`source=api_import` dividend events, primary calibration must show
`manual_corporate_actions_only` or `dividend_sync_missing` and is not final unless the
operator explicitly allows manual corporate actions.

Clean calibration is also blocked if enabled instruments are unresolved in
`instrument_registry`. `MOEX:*` ids are internal analytics ids, not broker ids.

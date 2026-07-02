# Calibration Runbook

## РќР°Р·РЅР°С‡РµРЅРёРµ

Calibration report СЃРѕР±РёСЂР°РµС‚ historical replay facts РІ РѕС‚С‡С‘С‚ РґР»СЏ РЅР°СЃС‚СЂРѕР№РєРё
РїРѕСЂРѕРіРѕРІ СЃС‚СЂР°С‚РµРіРёРё. РћРЅ РЅРµ РјРµРЅСЏРµС‚ `strategy_config` Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё.

РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…:

- `signal_candidate`;
- `candidate_stage_result`;
- `blocker_event`;
- `order_intent`;
- pseudo `broker_order`;
- `counterfactual_result`;
- `hourly_report`;
- `daily_report`;
- `market_candle`.

## РљРѕРјР°РЅРґР°

```powershell
python scripts/run_calibration_report.py `
  --lookback-days 90 `
  --strategy-id baseline `
  --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T `
  --timeframes 5m,10m,15m `
  --calibration-scope primary_normal_days `
  --require-special-day-classification `
  --group-by session_type,instrument_id,timeframe,blocker_code `
  --json-output
```

Make target:

```powershell
make calibration-report LOOKBACK_DAYS=90 STRATEGY_ID=baseline INSTRUMENTS=SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T TIMEFRAMES=5m,10m,15m
```

## Р§С‚Рѕ СЃС‡РёС‚Р°РµС‚ РѕС‚С‡С‘С‚

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
- cost sensitivity РґР»СЏ fee/slippage assumptions;
- recommended threshold changes.

## Cost Model

РџРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РґР»СЏ Р°РєС†РёР№:

- commission: РЅРµ РЅРёР¶Рµ `5 bps` per side;
- round-trip fee: РЅРµ РЅРёР¶Рµ `10 bps`;
- slippage: configurable, default `2 bps`;
- net result = gross result - fee - slippage.

## Recommendations

Recommendations СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ РІ `calibration_report.report_payload`:

- `max_spread_bps`;
- `min_market_quality_score`;
- `min_edge_after_total_costs_bps`;
- `max_data_age_ms`;
- `allow_short`.

`allow_short` РјРѕР¶РµС‚ Р±С‹С‚СЊ С‚РѕР»СЊРєРѕ СЂРµРєРѕРјРµРЅРґР°С†РёРµР№. Р РµР°Р»СЊРЅС‹Р№ `strategy_config`
РјРµРЅСЏРµС‚СЃСЏ РѕС‚РґРµР»СЊРЅС‹Рј РѕРїРµСЂР°С‚РѕСЂСЃРєРёРј РґРµР№СЃС‚РІРёРµРј С‡РµСЂРµР· API/UI Рё audit trail.

## UI

Frontend РёРјРµРµС‚ СЃС‚СЂР°РЅРёС†Сѓ `Calibration` / Calibration Center:

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
python scripts/run_calibration_observatory.py --universe SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --lookback-days 20 --json-output
```

Optional draft proposal:

```powershell
python scripts/run_calibration_observatory.py --universe SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --lookback-days 20 --create-candidate-config --json-output
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

РњРёРЅРёРјР°Р»СЊРЅР°СЏ Р»РѕРєР°Р»СЊРЅР°СЏ РїСЂРёС‘РјРєР°:

```powershell
python scripts/run_historical_data_quality_report.py --lookback-days 10 --json-output
python scripts/run_historical_replay_from_db.py --lookback-days 10 --strategy-id baseline --dry-run --json-output
python scripts/run_historical_counterfactual_rebuild.py --lookback-days 10 --strategy-id baseline --dry-run --json-output
python scripts/run_calibration_report.py --lookback-days 10 --strategy-id baseline --json-output
python scripts/run_launch_readiness.py --mode historical-replay --dry-run
```

РћРіСЂР°РЅРёС‡РµРЅРёРµ: РµСЃР»Рё РІ Р‘Р” РµС‰С‘ РЅРµС‚ `market_candle`, replay/calibration РІРµСЂРЅСѓС‚
РїСѓСЃС‚С‹Рµ summaries. Р­С‚Рѕ РєРѕСЂСЂРµРєС‚РЅРѕ РґР»СЏ dry-run, РЅРѕ РЅРµ СЏРІР»СЏРµС‚СЃСЏ РіРѕС‚РѕРІРЅРѕСЃС‚СЊСЋ Рє
shadow/live.

## Candle-only vs Shadow Confirmation

Recommendations СЂР°Р·РґРµР»РµРЅС‹ РЅР° РґРІР° РєРѕРЅС‚СѓСЂР°:

- `safe_from_historical_candles`: preliminary session/timeframe/instrument/holding-horizon
  РІС‹РІРѕРґС‹, РєРѕС‚РѕСЂС‹Рµ РјРѕР¶РЅРѕ РѕР±СЃСѓР¶РґР°С‚СЊ РїРѕСЃР»Рµ candle-only replay;
- `requires_shadow_confirmation`: spread, market quality, slippage, execution thresholds
  Рё live order policy, РєРѕС‚РѕСЂС‹Рµ РЅРµР»СЊР·СЏ СЃС‡РёС‚Р°С‚СЊ С„РёРЅР°Р»СЊРЅС‹РјРё Р±РµР· shadow live.

`calibration_data_mode=historical_candles_only` РІСЃРµРіРґР° СЃРѕРїСЂРѕРІРѕР¶РґР°РµС‚СЃСЏ caveats РїРѕ
`real_spread`, `order_book_depth`, `book_imbalance`, `market_quality_score`,
`real_slippage`, `broker_rejects`, `partial_fills`, `latency`.

## Corporate Actions Scope

Primary calibration РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РёСЃРїРѕР»СЊР·СѓРµС‚ `calibration_scope=primary_normal_days` Рё
РёСЃРєР»СЋС‡Р°РµС‚:

- `dividend_gap_day`;
- `corporate_action_day`;
- `exclude_from_primary_calibration=true`.

Special days Р°РЅР°Р»РёР·РёСЂСѓСЋС‚СЃСЏ РѕС‚РґРµР»СЊРЅРѕ:

```powershell
python scripts/run_calibration_report.py --lookback-days 90 --strategy-id baseline --calibration-scope special_days_only --json-output
```

Р•СЃР»Рё special-day classification РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚, report РІРѕР·РІСЂР°С‰Р°РµС‚
`calibration_clean=false`, warning `corporate_action_classification_missing` Рё
recommendation `run_market_special_day_classification_before_final_calibration`.

## Dividend Sync Requirement

Before final calibration run broker dividend sync and special-day classification:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --strict --json-output
python scripts/run_tbank_dividend_sync.py --lookback-days 730 --lookahead-days 365 --json-output
python scripts/run_market_special_day_classification.py --lookback-days 730 --include-future --lookahead-days 365 --require-dividend-sync --json-output
```

Manual CSV/JSON corporate actions are fallback/override only. If there are no
`source=api_import` dividend events, primary calibration must show
`manual_corporate_actions_only` or `dividend_sync_missing` and is not final unless the
operator explicitly allows manual corporate actions.

Clean calibration is also blocked if enabled instruments are unresolved in
`instrument_registry`. `MOEX:*` ids are internal analytics ids, not broker ids.

## Data-only preflight before calibration evidence

Calibration based on live microstructure must only use samples collected after data-only shadow preflight. If preflight returns `market_closed_expected`, no live samples are expected and the period must not be interpreted as robot failure. 10-20 trading days remain early evidence, not final truth, and must not hard-disable a contour by themselves.
## Calibration Sample Eligibility

Calibration uses official exchange data-only samples only. A broker quote shown while
MOEX is officially closed is not a calibration sample unless a future explicit
`include-broker-otc` workflow is introduced. Display quality can remain useful for the
operator, but calibration quality is zero/not applicable outside `official_exchange`.

The current market quality formula is a heuristic. Keep raw components so thresholds can
be tuned after 10-20 official exchange trading days without losing evidence.

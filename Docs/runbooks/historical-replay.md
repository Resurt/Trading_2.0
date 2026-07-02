# Historical Replay Runbook

## РќР°Р·РЅР°С‡РµРЅРёРµ

Historical replay РїСЂРµРІСЂР°С‰Р°РµС‚ СѓР¶Рµ Р·Р°РіСЂСѓР¶РµРЅРЅС‹Рµ `market_candle` РІ РїРѕР»РЅРѕС†РµРЅРЅС‹Р№
decision journal РґР»СЏ РєР°Р»РёР±СЂРѕРІРєРё СЃС‚СЂР°С‚РµРіРёРё:

- РїСЂРѕРІРµСЂСЏРµС‚ РєР°С‡РµСЃС‚РІРѕ РёСЃС‚РѕСЂРёС‡РµСЃРєРёС… СЃРІРµС‡РµР№;
- РїСЂРѕРіРѕРЅСЏРµС‚ closed bars `5m/10m/15m` С‡РµСЂРµР· `ConfigDrivenStrategyEngine`;
- РїРёС€РµС‚ `signal_candidate`, `candidate_stage_result`, `blocker_event`,
  `risk_event`, `order_intent`, pseudo `broker_order`, `order_state_event`;
- РїРµСЂРµСЃС‡РёС‚С‹РІР°РµС‚ counterfactual `+5m/+10m/+15m`;
- СЃС‚СЂРѕРёС‚ hourly/daily reports Рё calibration report.

Replay РІСЃРµРіРґР° СЂР°Р±РѕС‚Р°РµС‚ РІ `historical_replay` mode Рё РЅРµ РІС‹Р·С‹РІР°РµС‚ real
`PostOrder`/`CancelOrder`.

## РџРѕСЂСЏРґРѕРє Р·Р°РїСѓСЃРєР°

1. Dry-run Р·Р°РіСЂСѓР·РєРё СЃРІРµС‡РµР№ Р·Р° 10 РґРЅРµР№:

```powershell
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --lookback-days 10 --dry-run
```

2. Readonly backfill Р·Р° 10 РґРЅРµР№:

```powershell
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --lookback-days 10
```

3. Quality report:

```powershell
python scripts/run_historical_data_quality_report.py --lookback-days 10 --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --timeframes 1m,5m,10m,15m --json-output
```

4. Corporate actions import and special-day classification:

```powershell
python scripts/run_corporate_actions_import.py --file data/corporate_actions/sample_dividends.csv --source manual --json-output
python scripts/run_market_special_day_classification.py --lookback-days 10 --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --json-output
```

5. Final quality report with classification required:

```powershell
python scripts/run_historical_data_quality_report.py --lookback-days 10 --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --timeframes 1m,5m,10m,15m --require-special-day-classification --json-output
```

6. Replay from DB:

```powershell
python scripts/run_historical_replay_from_db.py --lookback-days 10 --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --timeframes 5m,10m,15m --strategy-id baseline --json-output
```

Replay now loads active `strategy_config` / `risk_limits` from PostgreSQL. If config is
missing, replay fails by default. `--allow-default-strategy-config` is allowed only for
explicit local dry-runs.

7. Counterfactual rebuild:

```powershell
python scripts/run_historical_counterfactual_rebuild.py --lookback-days 10 --strategy-id baseline --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --timeframes 5m,10m,15m --json-output
```

8. Historical reports:

```powershell
python scripts/run_historical_report_rebuild.py --lookback-days 10 --strategy-id baseline --include-counterfactual --json-output
```

9. Calibration report:

```powershell
python scripts/run_calibration_report.py --lookback-days 10 --strategy-id baseline --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --timeframes 5m,10m,15m --calibration-scope primary_normal_days --require-special-day-classification --json-output
```

10. Acceptance gate:

```powershell
python scripts/run_launch_readiness.py --mode historical-replay --dry-run
```

## Session Classification

Historical candles РїРѕР»СѓС‡Р°СЋС‚ fallback-РєР»Р°СЃСЃРёС„РёРєР°С†РёСЋ, РµСЃР»Рё РЅРµС‚ broker schedule:

- `weekday_morning`: 07:00-10:00 MSK;
- `weekday_main`: [10:00,19:00) MSK;
- `weekday_evening`: 19:00-23:50 MSK;
- `weekend`: РѕС‚РґРµР»СЊРЅС‹Р№ weekend contour;
- РІРЅРµ РѕРєРЅР°: `session_phase=closed`;
- РІРЅСѓС‚СЂРё РѕРєРЅР°: `session_phase=continuous_trading`.

`micro_session_id` РёРјРµРµС‚ С„РѕСЂРјР°С‚:

```text
historical:{trading_date}:{session_type}:{HH}
```

РџСЂРёРјРµСЂ:

```text
historical:2026-06-18:weekday_main:10
```

## Idempotency

Replay РёСЃРїРѕР»СЊР·СѓРµС‚ deterministic candidate fingerprint:

```text
strategy_id|strategy_version|instrument_id|timeframe|bar_close_ts|side|action|historical_db_replay
```

РџРѕРІС‚РѕСЂРЅС‹Р№ Р·Р°РїСѓСЃРє Р±РµР· `--reset-derived-events` РЅРµ РґСѓР±Р»РёСЂСѓРµС‚:

- `signal_candidate`;
- `candidate_stage_result`;
- `blocker_event`;
- `risk_event`;
- `order_intent`;
- `broker_order`;
- `order_state_event`;
- `counterfactual_result`.

`--reset-derived-events` СѓРґР°Р»СЏРµС‚ С‚РѕР»СЊРєРѕ СЃРѕР±С‹С‚РёСЏ СЃ `source=historical_db_replay`
Р·Р° РІС‹Р±СЂР°РЅРЅС‹Р№ РїРµСЂРёРѕРґ Рё СЃС‚СЂР°С‚РµРіРёСЋ. Live/shadow/sandbox СЃРѕР±С‹С‚РёСЏ РЅРµ СѓРґР°Р»СЏСЋС‚СЃСЏ.

## РћРіСЂР°РЅРёС‡РµРЅРёСЏ

- Historical replay РЅРµ СЏРІР»СЏРµС‚СЃСЏ РѕС†РµРЅРєРѕР№ СЂРµР°Р»СЊРЅРѕРіРѕ execution quality.
- Strategy config Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РЅРµ РјРµРЅСЏРµС‚СЃСЏ.
- Calibration recommendations СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ С‚РѕР»СЊРєРѕ РІ payload.
- Technical JSON logs РЅРµ СЏРІР»СЏСЋС‚СЃСЏ РёСЃС‚РѕС‡РЅРёРєРѕРј Р°РЅР°Р»РёС‚РёРєРё; РёСЃС‚РѕС‡РЅРёРє Р°РЅР°Р»РёС‚РёРєРё -
  PostgreSQL domain tables.

## Corporate Actions / Special Days

Historical replay РёСЃРєР»СЋС‡Р°РµС‚ `dividend_gap_day` Рё `corporate_action_day` РёР· primary replay
РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ. РџРѕРІРµРґРµРЅРёРµ РјРѕР¶РЅРѕ РёР·РјРµРЅРёС‚СЊ С‚РѕР»СЊРєРѕ СЏРІРЅРѕ:

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
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
```

Replay may still run in dry-run/local mode with seed rows, but final calibration
and shadow readiness require `source=tbank_resolved` and
`resolution_status=resolved` for enabled instruments.

Р’ payload РєР°Р¶РґРѕРіРѕ replay-generated event РїРѕРїР°РґР°СЋС‚ `special_day_type`,
`dividend_gap_day`, `corporate_action_flag`, `abnormal_gap_day`,
`excluded_from_primary_calibration` Рё `eligible_for_live_calibration`.

# Final Historical Calibration Runbook

## Strict Dividend Sync Gate

Final historical calibration is not clean when latest dividend sync is missing,
stale, partial, or failed.

Required latest sync state:

- `dividend_sync_run.status=completed`;
- `dividend_sync_run.clean=true`;
- `failed_instruments=0`;
- `error_count=0`;
- sync age is within `--max-dividend-sync-age-hours`.

`completed_with_errors` is treated as a launch blocker. Manual corporate-action data
can be used only with an explicit operator override and must not hide a failed
T-Bank `GetDividends` run.

## РќР°Р·РЅР°С‡РµРЅРёРµ

Р¤РёРЅР°Р»СЊРЅР°СЏ historical calibration РЅСѓР¶РЅР° С‚РѕР»СЊРєРѕ РєР°Рє РїРѕРґРіРѕС‚РѕРІРєР° Рє shadow live. РћРЅР° РЅРµ
РґРѕРєР°Р·С‹РІР°РµС‚ РїСЂРёР±С‹Р»СЊРЅРѕСЃС‚СЊ СЃС‚СЂР°С‚РµРіРёРё Рё РЅРµ Р·Р°РјРµРЅСЏРµС‚ live spread/depth/slippage/latency
РЅР°Р±Р»СЋРґРµРЅРёСЏ.

## РћР±СЏР·Р°С‚РµР»СЊРЅС‹Р№ РїРѕСЂСЏРґРѕРє

1. `historical candle backfill` РЅР° 10 РґРЅРµР№ РІ `--dry-run`.
2. `historical candle backfill` РЅР° 10 РґРЅРµР№ readonly.
3. `T-Bank dividend sync` С‡РµСЂРµР· `GetDividends`:
   `python scripts/run_tbank_dividend_sync.py --lookback-days 730 --lookahead-days 365 --json-output`.
4. `market special day classification` СЃ `--require-dividend-sync --include-future`.
5. `historical data quality report` СЃ `--require-special-day-classification`.
6. `historical replay from DB` С‚РѕР»СЊРєРѕ РїРѕ DB `strategy_config`.
7. `historical counterfactual rebuild`.
8. `historical reports rebuild`.
9. `calibration report` СЃ `calibration_scope=primary_normal_days`.
10. Р Р°СЃС€РёСЂРµРЅРёРµ РїРµСЂРёРѕРґР° РґРѕ 90d.
11. Р Р°СЃС€РёСЂРµРЅРёРµ РїРµСЂРёРѕРґР° РґРѕ 365d.
12. Shadow live 10-20 С‚РѕСЂРіРѕРІС‹С… РґРЅРµР№.
13. Sandbox order smoke.
14. Controlled minimal live.

## Final Gate

```powershell
python scripts/run_launch_readiness.py --mode historical-final-calibration
```

Gate РґРѕР»Р¶РµРЅ РїР°РґР°С‚СЊ, РµСЃР»Рё:

- РЅРµС‚ `market_candle`;
- РЅРµС‚ quality report;
- РЅРµ Р·Р°РїСѓСЃРєР°Р»Р°СЃСЊ special day classification;
- РЅРµ РІС‹РїРѕР»РЅРµРЅ T-Bank dividend sync, РµСЃР»Рё С‚РѕР»СЊРєРѕ РѕРїРµСЂР°С‚РѕСЂ СЏРІРЅРѕ РЅРµ СЂР°Р·СЂРµС€РёР» manual fallback;
- РµСЃС‚СЊ future dividend risk window Р±РµР· РєР»Р°СЃСЃРёС„РёРєР°С†РёРё Рё risk policy;
- `calibration_clean=false`;
- replay РёСЃРїРѕР»СЊР·РѕРІР°Р» default strategy config;
- dividend/corporate-action РґРЅРё РЅРµ РёСЃРєР»СЋС‡РµРЅС‹ РёР»Рё РЅРµ РїРѕРјРµС‡РµРЅС‹ РѕС‚РґРµР»СЊРЅРѕ;
- РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ counterfactual;
- РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ calibration report;
- secret scan РЅР°С€С‘Р» raw secrets.

## Р§С‚Рѕ СЃС‡РёС‚Р°РµС‚СЃСЏ С‡РёСЃС‚РѕР№ РєР°Р»РёР±СЂРѕРІРєРѕР№

`calibration_clean=true` РґРѕРїСѓСЃС‚РёРј С‚РѕР»СЊРєРѕ РєРѕРіРґР°:

- `calibration_scope=primary_normal_days`;
- special day classification РІС‹РїРѕР»РЅРµРЅР°;
- dividend calendar Р·Р°РіСЂСѓР¶РµРЅ С‡РµСЂРµР· T-Bank `GetDividends` (`source=api_import`) РёР»Рё
  РѕРїРµСЂР°С‚РѕСЂ СЏРІРЅРѕ Р·Р°РїСѓСЃС‚РёР» РѕС‚С‡С‘С‚ СЃ `--allow-manual-corporate-actions`;
- `dividend_gap_day` Рё `corporate_action_day` РёСЃРєР»СЋС‡РµРЅС‹ РёР· primary scope;
- recommendations СЃРѕС…СЂР°РЅРµРЅС‹ С‚РѕР»СЊРєРѕ РІ `calibration_report.report_payload`;
- `strategy_config` РЅРµ РёР·РјРµРЅС‘РЅ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё.

## Candle-only Caveats

Historical candles РЅРµ РєР°Р»РёР±СЂСѓСЋС‚:

- `real_spread`;
- `order_book_depth`;
- `book_imbalance`;
- `market_quality_score`;
- `real_slippage`;
- `broker_rejects`;
- `partial_fills`;
- `latency`.

## Instrument Resolution Gate

Final historical calibration requires broker-resolved instruments even though
replay itself does not place real orders. Dividend sync and real historical
backfill both call readonly T-Bank methods.

Run before final calibration:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
```

The readiness gate fails if any requested enabled row in `instrument_registry`
has `source=seed`, `resolution_status!=resolved`, and no `instrument_uid`/`figi`.
Clean calibration is not allowed while GetDividends/GetCandles would use
`MOEX:*` as a broker id.

Р­С‚Рё РїР°СЂР°РјРµС‚СЂС‹ С‚СЂРµР±СѓСЋС‚ shadow live calibration.

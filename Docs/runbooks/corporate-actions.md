# Corporate Actions Runbook

## Strict Dividend Sync Status

Partial T-Bank dividend sync is not clean.

Accepted clean state:

- latest `dividend_sync_run.status=completed`;
- `dividend_sync_run.clean=true`;
- `failed_instruments=0`;
- `error_count=0`;
- sync age is within the configured launch threshold.

Rejected states for final calibration, shadow, and production:

- `dry_run`;
- `completed_with_errors`;
- `failed`;
- `clean=false`;
- `failed_instruments > 0`;
- `error_count > 0`;
- stale sync.

Manual CSV/JSON remains fallback/override only. It cannot silently replace a failed
`api_import` sync unless the operator explicitly uses an override such as
`--allow-manual-corporate-actions`.

## РќР°Р·РЅР°С‡РµРЅРёРµ

`corporate_action_event` Рё `market_special_day` РЅСѓР¶РЅС‹, С‡С‚РѕР±С‹ historical replay Рё
calibration РЅРµ СЃРјРµС€РёРІР°Р»Рё РѕР±С‹С‡РЅС‹Рµ РґРЅРё СЃ dividend gap / split / corporate-action РґРЅСЏРјРё.
РўР°РєРёРµ РґРЅРё РјРѕРіСѓС‚ РІС‹РіР»СЏРґРµС‚СЊ РєР°Рє СЃРёР»СЊРЅС‹Р№ directional move РІ СЃРІРµС‡Р°С…, РЅРѕ СЌС‚Рѕ РЅРµ С‚РѕСЂРіРѕРІС‹Р№
СЃРёРіРЅР°Р» СЃС‚СЂР°С‚РµРіРёРё.

## РРјРїРѕСЂС‚

РћСЃРЅРѕРІРЅРѕР№ РїСѓС‚СЊ С‚РµРїРµСЂСЊ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРёР№: `trade-core` Рё CLI РІС‹Р·С‹РІР°СЋС‚ readonly broker method
`BrokerGateway.get_dividends`, РєРѕС‚РѕСЂС‹Р№ РІРЅСѓС‚СЂРё `infra/tbank` РјР°РїРїРёС‚СЃСЏ РЅР° T-Bank / T-Invest
`GetDividends`. Р СѓС‡РЅРѕР№ CSV/JSON import РѕСЃС‚Р°С‘С‚СЃСЏ С‚РѕР»СЊРєРѕ fallback/override, РєРѕРіРґР° РґР°РЅРЅС‹Рµ Р±СЂРѕРєРµСЂР°
РЅРµРґРѕСЃС‚СѓРїРЅС‹ РёР»Рё РѕРїРµСЂР°С‚РѕСЂ С…РѕС‡РµС‚ СЏРІРЅРѕ РїРµСЂРµРѕРїСЂРµРґРµР»РёС‚СЊ СЃРѕР±С‹С‚РёРµ.

```powershell
python scripts/run_tbank_dividend_sync.py `
  --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T `
  --lookback-days 730 `
  --lookahead-days 365 `
  --json-output
```

РџРѕСЃР»Рµ СѓСЃРїРµС€РЅРѕРіРѕ sync СЃРѕР±С‹С‚РёСЏ СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ РІ `corporate_action_event` СЃ
`source=api_import`, `confidence=confirmed`, `action_type=dividend`. Р‘СѓРґСѓС‰РёРµ ex-date
РїРѕРјРµС‡Р°СЋС‚СЃСЏ РІ `market_special_day` РєР°Рє `future_dividend_risk_window` РёР»Рё
`dividend_gap_day`, `exclude_from_primary_calibration=true`, `trade_policy=shadow_only`.

Р СѓС‡РЅРѕР№ fallback:

```powershell
python scripts/run_corporate_actions_import.py `
  --file data/corporate_actions/sample_dividends.csv `
  --source manual `
  --json-output
```

Р Р°Р·РѕРІС‹Р№ СЂСѓС‡РЅРѕР№ РІРІРѕРґ:

```powershell
python scripts/run_corporate_actions_import.py `
  --ticker SBER `
  --action-type dividend `
  --ex-date 2025-07-10 `
  --amount-per-share 34.84 `
  --currency RUB `
  --source manual `
  --json-output
```

CSV columns:

- `ticker`
- `instrument_id` optional
- `action_type`
- `ex_date`
- `registry_close_date` optional
- `payment_date` optional
- `amount_per_share` optional
- `currency` optional
- `source` optional
- `confidence` optional

## Special Day Classification

РџРѕСЃР»Рµ dividend sync РёР»Рё manual fallback РЅСѓР¶РЅРѕ РєР»Р°СЃСЃРёС„РёС†РёСЂРѕРІР°С‚СЊ РїРµСЂРёРѕРґ:

```powershell
python scripts/run_market_special_day_classification.py `
  --lookback-days 90 `
  --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T `
  --include-future `
  --lookahead-days 365 `
  --require-dividend-sync `
  --json-output
```

РљР»Р°СЃСЃРёС„РёРєР°С‚РѕСЂ:

- СЃРІСЏР·С‹РІР°РµС‚ `corporate_action_event.ex_date` СЃ `trading_date`;
- СЃС‡РёС‚Р°РµС‚ open gap: previous session close -> current session open;
- РїРёС€РµС‚ `dividend_gap_day`, `corporate_action_day`, `abnormal_gap_day`;
- РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ СЃС‚Р°РІРёС‚ `exclude_from_primary_calibration=true`;
- РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ СЃС‚Р°РІРёС‚ `trade_policy=shadow_only`.

## РћРїРµСЂР°С†РёРѕРЅРЅС‹Рµ РїСЂР°РІРёР»Р°

- Primary source РґР»СЏ dividend calendar: T-Bank `GetDividends`.
- Manual CSV/JSON: С‚РѕР»СЊРєРѕ fallback/override, РІ РѕС‚С‡С‘С‚Р°С… РѕС‚РѕР±СЂР°Р¶Р°РµС‚СЃСЏ warning
  `manual_corporate_actions_only`, РµСЃР»Рё РЅРµС‚ `api_import`.
- Dividend ex-date / dividend gap day РЅРµР»СЊР·СЏ СЃРјРµС€РёРІР°С‚СЊ СЃ РѕР±С‹С‡РЅС‹РјРё РґРЅСЏРјРё primary calibration.
- Future dividend risk window РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РїРµСЂРµРІРѕРґРёС‚ entries РІ `shadow_only`/block policy.
- Special days РјРѕР¶РЅРѕ Р°РЅР°Р»РёР·РёСЂРѕРІР°С‚СЊ РѕС‚РґРµР»СЊРЅРѕ С‡РµСЂРµР· `calibration_scope=special_days_only`.
- Р•СЃР»Рё classification РЅРµ Р·Р°РїСѓСЃРєР°Р»Р°СЃСЊ, `historical data quality` Рё `calibration` РґРѕР»Р¶РЅС‹
  РїРѕРєР°Р·С‹РІР°С‚СЊ warning `corporate_action_classification_missing`.
- Live/shadow risk layer РґРѕР»Р¶РµРЅ Р±Р»РѕРєРёСЂРѕРІР°С‚СЊ РёР»Рё РїРµСЂРµРІРѕРґРёС‚СЊ entries РІ shadow-only РїРѕ
  `RiskLimits.special_day_trade_policy`.

## Make Targets

```powershell
make dividend-sync
make dividend-sync-730d
make corporate-actions-import
make market-special-days
make market-special-days-future
```

## Instrument Resolution Prerequisite

Dividend sync is readonly, but it is still a real T-Bank broker call. It must not
send internal ids such as `MOEX:SBER` to `GetDividends`.

Before real dividend sync:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
```

Expected state in `instrument_registry`:

- `instrument_id` remains canonical, for example `MOEX:SBER`;
- `instrument_uid` or `figi` is present;
- `source=tbank_resolved`;
- `resolution_status=resolved`.

`source=seed` / `resolution_status=unresolved` is allowed only for local or
historical dry-run. It is not clean for final calibration, shadow or production.

## Definition Of Done

- `corporate_action_event` Р·Р°РїРѕР»РЅРµРЅР° РїРѕ РЅСѓР¶РЅС‹Рј РёРЅСЃС‚СЂСѓРјРµРЅС‚Р°Рј С‡РµСЂРµР· `source=api_import`
  РёР»Рё manual fallback СЏРІРЅРѕ СЂР°Р·СЂРµС€С‘РЅ РѕРїРµСЂР°С‚РѕСЂРѕРј.
- `market_special_day` РµСЃС‚СЊ Р·Р° replay/calibration РїРµСЂРёРѕРґ.
- Р‘СѓРґСѓС‰РёРµ dividend risk windows РєР»Р°СЃСЃРёС„РёС†РёСЂРѕРІР°РЅС‹.
- `python scripts/run_historical_data_quality_report.py --require-special-day-classification ...`
  РїСЂРѕС…РѕРґРёС‚ Р±РµР· РѕС€РёР±РєРё.
- Primary calibration РІРѕР·РІСЂР°С‰Р°РµС‚ `calibration_clean=true`; Р±РµР· dividend sync СЌС‚Рѕ Р·Р°РїСЂРµС‰РµРЅРѕ,
  РєСЂРѕРјРµ СЏРІРЅРѕРіРѕ `--allow-manual-corporate-actions`.

# Historical Candle Backfill

Р­С‚РѕС‚ РґРѕРєСѓРјРµРЅС‚ С„РёРєСЃРёСЂСѓРµС‚ РєРѕРЅС‚СѓСЂ Р·Р°РіСЂСѓР·РєРё РёСЃС‚РѕСЂРёС‡РµСЃРєРёС… СЃРІРµС‡РµР№ T-Bank/T-Invest РїРµСЂРµРґ
sandbox/shadow/production. Р¦РµР»СЊ - РЅР°РєРѕРїРёС‚СЊ Р±Р°Р·Сѓ `market_candle`, РїСЂРѕРіРЅР°С‚СЊ replay Рё
РїРѕСЃС‚СЂРѕРёС‚СЊ Р°РЅР°Р»РёС‚РёРєСѓ candidates/blockers/counterfactual Р±РµР· СЂРµР°Р»СЊРЅС‹С… РѕСЂРґРµСЂРѕРІ.

## РРЅРІР°СЂРёР°РЅС‚С‹

- Backfill РІС‹Р·С‹РІР°РµС‚ С‚РѕР»СЊРєРѕ readonly `BrokerGateway.get_candles()` Рё
  `resolve_instruments()` РїСЂРё РЅРµРѕР±С…РѕРґРёРјРѕСЃС‚Рё.
- `PostOrder` Рё `CancelOrder` РІ СЌС‚РѕРј РєРѕРЅС‚СѓСЂРµ РЅРµ РІС‹Р·С‹РІР°СЋС‚СЃСЏ.
- РРЅСЃС‚СЂСѓРјРµРЅС‚С‹ Р±РµСЂСѓС‚СЃСЏ РёР· `instrument_registry` РёР»Рё СЂРµР·РѕР»РІСЏС‚СЃСЏ С‡РµСЂРµР·
  `InstrumentResolverService`.
- Р”Р»СЏ sandbox/shadow/production-like Р·Р°РіСЂСѓР·РєРё placeholder `instrument_uid` Р·Р°РїСЂРµС‰С‘РЅ.
- Raw interval РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ: `1m`.
- Derived intervals РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ: `5m`, `10m`, `15m`.
- Derived bars СЃС‚СЂРѕСЏС‚СЃСЏ С‡РµСЂРµР· СЃСѓС‰РµСЃС‚РІСѓСЋС‰РёР№ `BarEngine`, РЅРµ РѕС‚РґРµР»СЊРЅРѕР№ Р»РѕРіРёРєРѕР№.
- Р—Р°РїРёСЃСЊ РёРґС‘С‚ РІ `market_candle` С‡РµСЂРµР· `SqlAlchemyMarketDataStore` Рё
  repository-level upsert, РїРѕСЌС‚РѕРјСѓ РїРѕРІС‚РѕСЂРЅС‹Р№ Р·Р°РїСѓСЃРє РЅРµ РїР»РѕРґРёС‚ РґСѓР±Р»Рё.

## РЎРµСЂРІРёСЃ

РљРѕРґ РЅР°С…РѕРґРёС‚СЃСЏ РІ:

- `apps/trade-core/src/trade_core/market_data/historical_backfill.py`

РћСЃРЅРѕРІРЅС‹Рµ СЃСѓС‰РЅРѕСЃС‚Рё:

- `HistoricalCandleBackfillService`
- `HistoricalBackfillConfig`
- `HistoricalBackfillPlan`
- `HistoricalBackfillChunk`
- `HistoricalBackfillResult`
- `HistoricalBackfillInstrumentResult`
- `HistoricalBackfillQualitySummary`

Quality summary СЃС‡РёС‚Р°РµС‚:

- СЃРєРѕР»СЊРєРѕ raw candles СѓРІРёРґРµР»Рё Рё СЃРєРѕР»СЊРєРѕ closed;
- СЃРєРѕР»СЊРєРѕ candles СѓР¶Рµ СЃСѓС‰РµСЃС‚РІРѕРІР°Р»Рѕ;
- СЃРєРѕР»СЊРєРѕ РЅРµРїРѕР»РЅС‹С… candles РѕС‚Р±СЂРѕС€РµРЅРѕ;
- СЃРєРѕР»СЊРєРѕ candles РёРјРµСЋС‚ РЅРµРєРѕСЂСЂРµРєС‚РЅС‹Рµ OHLC С†РµРЅС‹;
- СЃРєРѕР»СЊРєРѕ gap РїРµСЂРµС…РѕРґРѕРІ РѕР±РЅР°СЂСѓР¶РµРЅРѕ РІ РїСЂРµРґРµР»Р°С… Р·Р°РіСЂСѓР¶РµРЅРЅРѕРіРѕ СЂСЏРґР°.

## CLI

Dry-run Р±РµР· С‚РѕРєРµРЅРѕРІ Рё Р±РµР· Р·Р°РїРёСЃРё:

```powershell
python scripts/run_historical_candle_backfill.py `
  --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T `
  --from-date 2025-01-01 `
  --to-date 2026-06-18 `
  --raw-interval 1m `
  --derive 5m,10m,15m `
  --chunk-days 7 `
  --strategy-id baseline `
  --dry-run
```

Р РµР°Р»СЊРЅР°СЏ readonly Р·Р°РіСЂСѓР·РєР° С‚СЂРµР±СѓРµС‚ СѓСЃС‚Р°РЅРѕРІР»РµРЅРЅС‹Р№ T-Bank SDK extra, С‚РѕРєРµРЅ РІ
ignored `secrets/` РёР»Рё Docker secret file env Рё PostgreSQL `DATABASE_URL` /
`POSTGRES_*`:

```powershell
$env:TRADING_BACKFILL_RUNTIME_MODE = "shadow"
$env:TBANK_ENVIRONMENT = "live"
$env:SSL_TBANK_VERIFY = "true"
python scripts/run_tbank_sdk_import_check.py
python scripts/run_historical_candle_backfill.py `
  --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T `
  --from-date 2025-01-01 `
  --to-date 2026-06-18 `
  --raw-interval 1m `
  --derive 5m,10m,15m `
  --chunk-days 7 `
  --strategy-id baseline
```

`TRADING_BACKFILL_RUNTIME_MODE=shadow` РІС‹Р±РёСЂР°РµС‚ live readonly broker target Рё
РѕСЃС‚Р°РІР»СЏРµС‚ execution pseudo-only. Р”Р»СЏ sandbox РјРѕР¶РЅРѕ СѓРєР°Р·Р°С‚СЊ `sandbox`, РЅРѕ sandbox
history РјРѕР¶РµС‚ РѕС‚Р»РёС‡Р°С‚СЊСЃСЏ РѕС‚ live market history Рё РЅРµ СЏРІР»СЏРµС‚СЃСЏ РѕС†РµРЅРєРѕР№ real
execution quality.

## Р”Р°Р»СЊС€Рµ РїРѕСЃР»Рµ Р·Р°РіСЂСѓР·РєРё

1. РџСЂРѕРІРµСЂРёС‚СЊ `market_candle` РїРѕ `instrument_id + timeframe + trading_date`.
2. Р—Р°РїСѓСЃС‚РёС‚СЊ replay РёР· Р·Р°РіСЂСѓР¶РµРЅРЅС‹С… СЃРІРµС‡РµР№, РєРѕРіРґР° replay harness Р±СѓРґРµС‚ РїРѕРґРєР»СЋС‡С‘РЅ Рє
   DB-backed candle source.
3. РџРµСЂРµСЃС‚СЂРѕРёС‚СЊ РѕС‚С‡С‘С‚С‹:

```powershell
python tools/reports/build_daily_report.py --date <YYYY-MM-DD> --strategy-id baseline --force-rebuild
python tools/reports/run_counterfactual_analysis.py --date <YYYY-MM-DD> --strategy-id baseline --force-rebuild
```

4. РЎРјРѕС‚СЂРµС‚СЊ daily report РїРѕ `session_type`, `instrument`, `timeframe`,
   `blocker_code`, `candidate funnel` Рё `missed opportunity summary`.

## РћРіСЂР°РЅРёС‡РµРЅРёСЏ

- Backfill СЃР°Рј РїРѕ СЃРµР±Рµ РЅРµ СЃРѕР·РґР°С‘С‚ `signal_candidate`: СЌС‚Рѕ РґРµР»Р°РµС‚ replay/strategy
  РєРѕРЅС‚СѓСЂ РЅР° РѕСЃРЅРѕРІРµ Р·Р°РєСЂС‹С‚С‹С… bars.
- Р”Р»СЏ СЂРµР°Р»СЊРЅРѕР№ Р·Р°РіСЂСѓР·РєРё РЅСѓР¶РµРЅ РґРѕСЃС‚СѓРї T-Bank SDK Рє РёСЃС‚РѕСЂРёС‡РµСЃРєРёРј candles РІС‹Р±СЂР°РЅРЅРѕРіРѕ
  РѕРєСЂСѓР¶РµРЅРёСЏ.
- Р”РЅРµРІРЅС‹Рµ РѕС‚С‡С‘С‚С‹ РїРѕ blocker/candidate/counterfactual Р±СѓРґСѓС‚ СЃРѕРґРµСЂР¶Р°С‚РµР»СЊРЅС‹РјРё С‚РѕР»СЊРєРѕ
  РїРѕСЃР»Рµ replay, РєРѕС‚РѕСЂС‹Р№ СЃРѕР·РґР°СЃС‚ decision journal.

## Historical replay and calibration continuation

РџРѕСЃР»Рµ backfill РѕР±СЏР·Р°С‚РµР»СЊРЅС‹Р№ РїРѕСЂСЏРґРѕРє С‚РµРїРµСЂСЊ С‚Р°РєРѕР№:

1. `python scripts/run_historical_data_quality_report.py --lookback-days 10 --json-output`
2. `python scripts/run_historical_replay_from_db.py --lookback-days 10 --strategy-id baseline --json-output`
3. `python scripts/run_historical_counterfactual_rebuild.py --lookback-days 10 --strategy-id baseline --json-output`
4. `python scripts/run_historical_report_rebuild.py --lookback-days 10 --strategy-id baseline --include-counterfactual --json-output`
5. `python scripts/run_calibration_report.py --lookback-days 10 --strategy-id baseline --json-output`

Historical replay С‡РёС‚Р°РµС‚ `market_candle`, РёСЃРїРѕР»СЊР·СѓРµС‚ С‚РѕС‚ Р¶Рµ
`ConfigDrivenStrategyEngine`, `DefaultRiskEngine`, `DefaultExecutionEngine` Рё
`SqlAlchemyStrategyEventStore`, РЅРѕ СЂР°Р±РѕС‚Р°РµС‚ С‚РѕР»СЊРєРѕ РІ
`RuntimeMode.HISTORICAL_REPLAY`. Р РµР°Р»СЊРЅС‹Рµ `PostOrder` Рё `CancelOrder` РІ СЌС‚РѕРј
РєРѕРЅС‚СѓСЂРµ Р·Р°РїСЂРµС‰РµРЅС‹: execution РїРёС€РµС‚ pseudo `broker_order` Рё `order_state_event`.

Р’СЃРµ generated С„Р°РєС‚С‹ РїРѕРјРµС‡Р°СЋС‚СЃСЏ `payload.source=historical_db_replay`, Р°
counterfactual/calibration СЂРµР·СѓР»СЊС‚Р°С‚С‹ РёРјРµСЋС‚ РѕС‚РґРµР»СЊРЅС‹Рµ `source` payload-РїРѕР»СЏ.
РџРѕРІС‚РѕСЂРЅС‹Р№ replay РёРґРµРјРїРѕС‚РµРЅС‚РµРЅ РїРѕ deterministic fingerprint; С„Р»Р°Рі
`--reset-derived-events` СѓРґР°Р»СЏРµС‚ С‚РѕР»СЊРєРѕ historical replay facts Р·Р° РІС‹Р±СЂР°РЅРЅС‹Р№
РїРµСЂРёРѕРґ Рё РЅРµ С‚СЂРѕРіР°РµС‚ live/shadow/sandbox СЃРѕР±С‹С‚РёСЏ.
## РџРѕСЃР»Рµ Backfill

Historical candle backfill СЃР°Рј РїРѕ СЃРµР±Рµ РЅРµ РґРµР»Р°РµС‚ РєР°Р»РёР±СЂРѕРІРєСѓ С„РёРЅР°Р»СЊРЅРѕР№. РџРѕСЃР»Рµ Р·Р°РіСЂСѓР·РєРё
`market_candle` РЅСѓР¶РЅРѕ РІС‹РїРѕР»РЅРёС‚СЊ:

1. `run_corporate_actions_import.py`;
2. `run_market_special_day_classification.py`;
3. `run_historical_data_quality_report.py --require-special-day-classification`;
4. `run_historical_replay_from_db.py` Р±РµР· real broker calls;
5. `run_historical_counterfactual_rebuild.py`;
6. `run_calibration_report.py --calibration-scope primary_normal_days`;
7. `run_launch_readiness.py --mode historical-final-calibration`.

Dividend/corporate-action days excluded from primary calibration by default.

## Instrument Resolution For Real Backfill

Dry-run backfill may use seed registry rows and does not require a T-Bank token.
Real readonly backfill calls `GetCandles`, so it requires resolved T-Bank
`instrument_uid` or `figi`.

Required order:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --strict --json-output
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --lookback-days 10 --json-output
```

`run_historical_candle_backfill.py` resolves instruments by default for real
runs and fails if an enabled instrument remains `source=seed` /
`resolution_status=unresolved`. `--allow-unresolved` is intended only for
dry-run/local smoke.

Backfill also clamps broker request chunks to T-Bank GetCandles interval
limits. For the default raw `1m` interval one broker request is at most one
day, even if CLI `--chunk-days` is larger. This prevents API error `30014`
(`maximum request period for the given candle interval has been exceeded`).

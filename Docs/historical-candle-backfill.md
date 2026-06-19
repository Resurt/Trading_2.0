# Historical Candle Backfill

Этот документ фиксирует контур загрузки исторических свечей T-Bank/T-Invest перед
sandbox/shadow/production. Цель - накопить базу `market_candle`, прогнать replay и
построить аналитику candidates/blockers/counterfactual без реальных ордеров.

## Инварианты

- Backfill вызывает только readonly `BrokerGateway.get_candles()` и
  `resolve_instruments()` при необходимости.
- `PostOrder` и `CancelOrder` в этом контуре не вызываются.
- Инструменты берутся из `instrument_registry` или резолвятся через
  `InstrumentResolverService`.
- Для sandbox/shadow/production-like загрузки placeholder `instrument_uid` запрещён.
- Raw interval по умолчанию: `1m`.
- Derived intervals по умолчанию: `5m`, `10m`, `15m`.
- Derived bars строятся через существующий `BarEngine`, не отдельной логикой.
- Запись идёт в `market_candle` через `SqlAlchemyMarketDataStore` и
  repository-level upsert, поэтому повторный запуск не плодит дубли.

## Сервис

Код находится в:

- `apps/trade-core/src/trade_core/market_data/historical_backfill.py`

Основные сущности:

- `HistoricalCandleBackfillService`
- `HistoricalBackfillConfig`
- `HistoricalBackfillPlan`
- `HistoricalBackfillChunk`
- `HistoricalBackfillResult`
- `HistoricalBackfillInstrumentResult`
- `HistoricalBackfillQualitySummary`

Quality summary считает:

- сколько raw candles увидели и сколько closed;
- сколько candles уже существовало;
- сколько неполных candles отброшено;
- сколько candles имеют некорректные OHLC цены;
- сколько gap переходов обнаружено в пределах загруженного ряда.

## CLI

Dry-run без токенов и без записи:

```powershell
python scripts/run_historical_candle_backfill.py `
  --instruments SBER,GAZP `
  --from-date 2025-01-01 `
  --to-date 2026-06-18 `
  --raw-interval 1m `
  --derive 5m,10m,15m `
  --chunk-days 7 `
  --strategy-id baseline `
  --dry-run
```

Реальная readonly загрузка требует установленный T-Bank SDK extra, токен в
ignored `secrets/` или Docker secret file env и PostgreSQL `DATABASE_URL` /
`POSTGRES_*`:

```powershell
$env:TRADING_BACKFILL_RUNTIME_MODE = "shadow"
$env:TBANK_ENVIRONMENT = "live"
$env:SSL_TBANK_VERIFY = "true"
python scripts/run_tbank_sdk_import_check.py
python scripts/run_historical_candle_backfill.py `
  --instruments SBER,GAZP,LKOH `
  --from-date 2025-01-01 `
  --to-date 2026-06-18 `
  --raw-interval 1m `
  --derive 5m,10m,15m `
  --chunk-days 7 `
  --strategy-id baseline
```

`TRADING_BACKFILL_RUNTIME_MODE=shadow` выбирает live readonly broker target и
оставляет execution pseudo-only. Для sandbox можно указать `sandbox`, но sandbox
history может отличаться от live market history и не является оценкой real
execution quality.

## Дальше после загрузки

1. Проверить `market_candle` по `instrument_id + timeframe + trading_date`.
2. Запустить replay из загруженных свечей, когда replay harness будет подключён к
   DB-backed candle source.
3. Перестроить отчёты:

```powershell
python tools/reports/build_daily_report.py --date <YYYY-MM-DD> --strategy-id baseline --force-rebuild
python tools/reports/run_counterfactual_analysis.py --date <YYYY-MM-DD> --strategy-id baseline --force-rebuild
```

4. Смотреть daily report по `session_type`, `instrument`, `timeframe`,
   `blocker_code`, `candidate funnel` и `missed opportunity summary`.

## Ограничения

- Backfill сам по себе не создаёт `signal_candidate`: это делает replay/strategy
  контур на основе закрытых bars.
- Для реальной загрузки нужен доступ T-Bank SDK к историческим candles выбранного
  окружения.
- Дневные отчёты по blocker/candidate/counterfactual будут содержательными только
  после replay, который создаст decision journal.

## Historical replay and calibration continuation

После backfill обязательный порядок теперь такой:

1. `python scripts/run_historical_data_quality_report.py --lookback-days 10 --json-output`
2. `python scripts/run_historical_replay_from_db.py --lookback-days 10 --strategy-id baseline --json-output`
3. `python scripts/run_historical_counterfactual_rebuild.py --lookback-days 10 --strategy-id baseline --json-output`
4. `python scripts/run_historical_report_rebuild.py --lookback-days 10 --strategy-id baseline --include-counterfactual --json-output`
5. `python scripts/run_calibration_report.py --lookback-days 10 --strategy-id baseline --json-output`

Historical replay читает `market_candle`, использует тот же
`ConfigDrivenStrategyEngine`, `DefaultRiskEngine`, `DefaultExecutionEngine` и
`SqlAlchemyStrategyEventStore`, но работает только в
`RuntimeMode.HISTORICAL_REPLAY`. Реальные `PostOrder` и `CancelOrder` в этом
контуре запрещены: execution пишет pseudo `broker_order` и `order_state_event`.

Все generated факты помечаются `payload.source=historical_db_replay`, а
counterfactual/calibration результаты имеют отдельные `source` payload-поля.
Повторный replay идемпотентен по deterministic fingerprint; флаг
`--reset-derived-events` удаляет только historical replay facts за выбранный
период и не трогает live/shadow/sandbox события.
## После Backfill

Historical candle backfill сам по себе не делает калибровку финальной. После загрузки
`market_candle` нужно выполнить:

1. `run_corporate_actions_import.py`;
2. `run_market_special_day_classification.py`;
3. `run_historical_data_quality_report.py --require-special-day-classification`;
4. `run_historical_replay_from_db.py` без real broker calls;
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
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH --strict --json-output
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP --lookback-days 10 --json-output
```

`run_historical_candle_backfill.py` resolves instruments by default for real
runs and fails if an enabled instrument remains `source=seed` /
`resolution_status=unresolved`. `--allow-unresolved` is intended only for
dry-run/local smoke.

Backfill also clamps broker request chunks to T-Bank GetCandles interval
limits. For the default raw `1m` interval one broker request is at most one
day, even if CLI `--chunk-days` is larger. This prevents API error `30014`
(`maximum request period for the given candle interval has been exceeded`).

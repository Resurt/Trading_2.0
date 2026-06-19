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

## Назначение

Финальная historical calibration нужна только как подготовка к shadow live. Она не
доказывает прибыльность стратегии и не заменяет live spread/depth/slippage/latency
наблюдения.

## Обязательный порядок

1. `historical candle backfill` на 10 дней в `--dry-run`.
2. `historical candle backfill` на 10 дней readonly.
3. `T-Bank dividend sync` через `GetDividends`:
   `python scripts/run_tbank_dividend_sync.py --lookback-days 730 --lookahead-days 365 --json-output`.
4. `market special day classification` с `--require-dividend-sync --include-future`.
5. `historical data quality report` с `--require-special-day-classification`.
6. `historical replay from DB` только по DB `strategy_config`.
7. `historical counterfactual rebuild`.
8. `historical reports rebuild`.
9. `calibration report` с `calibration_scope=primary_normal_days`.
10. Расширение периода до 90d.
11. Расширение периода до 365d.
12. Shadow live 10-20 торговых дней.
13. Sandbox order smoke.
14. Controlled minimal live.

## Final Gate

```powershell
python scripts/run_launch_readiness.py --mode historical-final-calibration
```

Gate должен падать, если:

- нет `market_candle`;
- нет quality report;
- не запускалась special day classification;
- не выполнен T-Bank dividend sync, если только оператор явно не разрешил manual fallback;
- есть future dividend risk window без классификации и risk policy;
- `calibration_clean=false`;
- replay использовал default strategy config;
- dividend/corporate-action дни не исключены или не помечены отдельно;
- отсутствует counterfactual;
- отсутствует calibration report;
- secret scan нашёл raw secrets.

## Что считается чистой калибровкой

`calibration_clean=true` допустим только когда:

- `calibration_scope=primary_normal_days`;
- special day classification выполнена;
- dividend calendar загружен через T-Bank `GetDividends` (`source=api_import`) или
  оператор явно запустил отчёт с `--allow-manual-corporate-actions`;
- `dividend_gap_day` и `corporate_action_day` исключены из primary scope;
- recommendations сохранены только в `calibration_report.report_payload`;
- `strategy_config` не изменён автоматически.

## Candle-only Caveats

Historical candles не калибруют:

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
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
```

The readiness gate fails if any requested enabled row in `instrument_registry`
has `source=seed`, `resolution_status!=resolved`, and no `instrument_uid`/`figi`.
Clean calibration is not allowed while GetDividends/GetCandles would use
`MOEX:*` as a broker id.

Эти параметры требуют shadow live calibration.

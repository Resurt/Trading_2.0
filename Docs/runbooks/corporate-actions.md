# Corporate Actions Runbook

## Назначение

`corporate_action_event` и `market_special_day` нужны, чтобы historical replay и
calibration не смешивали обычные дни с dividend gap / split / corporate-action днями.
Такие дни могут выглядеть как сильный directional move в свечах, но это не торговый
сигнал стратегии.

## Импорт

Первый шаг поддерживает ручной CSV/JSON import без внешнего API:

```powershell
python scripts/run_corporate_actions_import.py `
  --file data/corporate_actions/sample_dividends.csv `
  --source manual `
  --json-output
```

Разовый ручной ввод:

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

После импорта нужно классифицировать период:

```powershell
python scripts/run_market_special_day_classification.py `
  --lookback-days 90 `
  --instruments SBER,GAZP `
  --json-output
```

Классификатор:

- связывает `corporate_action_event.ex_date` с `trading_date`;
- считает open gap: previous session close -> current session open;
- пишет `dividend_gap_day`, `corporate_action_day`, `abnormal_gap_day`;
- по умолчанию ставит `exclude_from_primary_calibration=true`;
- по умолчанию ставит `trade_policy=shadow_only`.

## Операционные правила

- Dividend ex-date / dividend gap day нельзя смешивать с обычными днями primary calibration.
- Special days можно анализировать отдельно через `calibration_scope=special_days_only`.
- Если classification не запускалась, `historical data quality` и `calibration` должны
  показывать warning `corporate_action_classification_missing`.
- Live/shadow risk layer должен блокировать или переводить entries в shadow-only по
  `RiskLimits.special_day_trade_policy`.

## Make Targets

```powershell
make corporate-actions-import
make market-special-days
```

## Definition Of Done

- `corporate_action_event` заполнена по нужным инструментам.
- `market_special_day` есть за replay/calibration период.
- `python scripts/run_historical_data_quality_report.py --require-special-day-classification ...`
  проходит без ошибки.
- Primary calibration возвращает `calibration_clean=true`.

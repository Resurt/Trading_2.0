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

## Назначение

`corporate_action_event` и `market_special_day` нужны, чтобы historical replay и
calibration не смешивали обычные дни с dividend gap / split / corporate-action днями.
Такие дни могут выглядеть как сильный directional move в свечах, но это не торговый
сигнал стратегии.

## Импорт

Основной путь теперь автоматический: `trade-core` и CLI вызывают readonly broker method
`BrokerGateway.get_dividends`, который внутри `infra/tbank` маппится на T-Bank / T-Invest
`GetDividends`. Ручной CSV/JSON import остаётся только fallback/override, когда данные брокера
недоступны или оператор хочет явно переопределить событие.

```powershell
python scripts/run_tbank_dividend_sync.py `
  --instruments SBER,GAZP `
  --lookback-days 730 `
  --lookahead-days 365 `
  --json-output
```

После успешного sync события сохраняются в `corporate_action_event` с
`source=api_import`, `confidence=confirmed`, `action_type=dividend`. Будущие ex-date
помечаются в `market_special_day` как `future_dividend_risk_window` или
`dividend_gap_day`, `exclude_from_primary_calibration=true`, `trade_policy=shadow_only`.

Ручной fallback:

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

После dividend sync или manual fallback нужно классифицировать период:

```powershell
python scripts/run_market_special_day_classification.py `
  --lookback-days 90 `
  --instruments SBER,GAZP `
  --include-future `
  --lookahead-days 365 `
  --require-dividend-sync `
  --json-output
```

Классификатор:

- связывает `corporate_action_event.ex_date` с `trading_date`;
- считает open gap: previous session close -> current session open;
- пишет `dividend_gap_day`, `corporate_action_day`, `abnormal_gap_day`;
- по умолчанию ставит `exclude_from_primary_calibration=true`;
- по умолчанию ставит `trade_policy=shadow_only`.

## Операционные правила

- Primary source для dividend calendar: T-Bank `GetDividends`.
- Manual CSV/JSON: только fallback/override, в отчётах отображается warning
  `manual_corporate_actions_only`, если нет `api_import`.
- Dividend ex-date / dividend gap day нельзя смешивать с обычными днями primary calibration.
- Future dividend risk window по умолчанию переводит entries в `shadow_only`/block policy.
- Special days можно анализировать отдельно через `calibration_scope=special_days_only`.
- Если classification не запускалась, `historical data quality` и `calibration` должны
  показывать warning `corporate_action_classification_missing`.
- Live/shadow risk layer должен блокировать или переводить entries в shadow-only по
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
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH --strict --json-output
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

- `corporate_action_event` заполнена по нужным инструментам через `source=api_import`
  или manual fallback явно разрешён оператором.
- `market_special_day` есть за replay/calibration период.
- Будущие dividend risk windows классифицированы.
- `python scripts/run_historical_data_quality_report.py --require-special-day-classification ...`
  проходит без ошибки.
- Primary calibration возвращает `calibration_clean=true`; без dividend sync это запрещено,
  кроме явного `--allow-manual-corporate-actions`.

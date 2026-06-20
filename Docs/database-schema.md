# Схема данных PostgreSQL

Документ фиксирует каноническую стартовую схему для шага 03. PostgreSQL остается source of truth для состояния, ордеров, доменных событий, отчетов, counterfactual analytics и аудита.

## Принципы

- Аналитически важные поля вынесены в отдельные колонки.
- JSONB используется только для `*_payload`, `risk_limits`, `config_payload` и расширенного контекста.
- Все события и отчеты, где это применимо, содержат `calendar_date`, `trading_date`, `session_type`, `session_phase`, `micro_session_id`, `broker_trading_status`.
- `request_order_id` является внутренним идемпотентным ключом ордера.
- `exchange_order_id` хранит внешний broker/exchange id и индексируется отдельно.
- Сырые technical logs не пишутся в PostgreSQL как аналитический источник.

## ER-like summary

```text
instrument_registry
  1 -> many signal_candidate

strategy_config
  versioned by strategy_id + version + session_template

session_run
  1 -> many signal_candidate
  1 -> many hourly_report

signal_candidate
  1 -> many blocker_event
  1 -> many order_intent
  1 -> many counterfactual_result

order_intent
  request_order_id unique
  1 -> 0..1 broker_order
  1 -> many fill_event by request_order_id
  1 -> many counterfactual_result

broker_order
  request_order_id unique
  exchange_order_id indexed unique when present
  lifecycle_seq prevents stale status rewrites

risk_event
  references candidate/order context by ids

position_snapshot
  keyed by micro-session + instrument + account + snapshot_ts

market_candle
  closed candles and bars by instrument + timeframe + UTC bucket

market_status_snapshot
  broker status/info observations by instrument + ts_utc

order_book_summary
  lightweight book summaries by instrument + ts_utc

hourly_report
  one report per micro_session_id + strategy_id

daily_report
  aggregate by trading_date, strategy_id, optional session/instrument scope

audit_event
  partitioned event stream for operator/system actions
```

## Таблицы

| Таблица | Назначение |
| --- | --- |
| `instrument_registry` | Реестр инструментов MOEX, seed: `SBER`, `GAZP`, `LKOH`. |
| `strategy_config` | Версионированные настройки стратегии по `session_template`: `weekday_morning`, `weekday_main`, `weekday_evening`, `weekend`. |
| `session_run` | Логический hourly micro-session run без рестарта `trade-core`. |
| `signal_candidate` | Потенциальный сигнал до прохождения blocker/risk/execution gates. |
| `blocker_event` | Причинная цепочка blockers/gates с `reason_code`. |
| `order_intent` | Идемпотентное внутреннее намерение разместить/отменить/заменить ордер. |
| `broker_order` | Наблюдаемый broker lifecycle по `request_order_id` и `exchange_order_id`. |
| `fill_event` | Исполнения и частичные исполнения. |
| `risk_event` | Решения risk engine и нарушения лимитов. |
| `position_snapshot` | Снимки позиции на границах micro-session и risk events. |
| `market_candle` | Закрытые свечи и бары с UTC/exchange timestamps. |
| `market_status_snapshot` | Нормализованные status/info snapshots. |
| `order_book_summary` | Lightweight агрегаты стакана без хранения полного стакана на каждый тик. |
| `strategy_state_event` | Переходы состояния стратегии для replay/diagnostics. |
| `hourly_report` | Агрегат по закрытой micro-session. |
| `daily_report` | Дневной агрегат по `trading_date`. |
| `counterfactual_result` | Аналитика blocked/cancelled сделок на окнах 5/10/15 минут. |
| `audit_event` | Структурированный аудит действий системы и оператора. |

## Partitioned tables

Эти таблицы проектируются как event-heavy и partitioned by `RANGE (trading_date)`:

- `fill_event` - высокая частота частичных исполнений и удобная очистка/архивация по торговым датам.
- `audit_event` - потенциально большой поток операторских и системных действий.
- `blocker_event` - много gate-level событий на каждого `signal_candidate`.
- `strategy_state_event` - поток state transitions для replay.
- `counterfactual_result` - тяжелая аналитика blocked/cancelled cases по датам.
- `market_candle` - поток свечей и закрытых баров для replay и калибровки.
- `market_status_snapshot` - поток broker status/info observations.
- `order_book_summary` - частые lightweight snapshots стакана без полного raw book.

В стартовой миграции создаются default partitions:

- `fill_event_default`
- `audit_event_default`
- `blocker_event_default`
- `strategy_state_event_default`
- `counterfactual_result_default`
- `market_candle_default`
- `market_status_snapshot_default`
- `order_book_summary_default`

Следующие миграции могут добавлять месячные или дневные partitions без изменения application layer.

## Миграции

Текущие миграции:

- `20260613_0001_initial_postgres_schema.py`
- `20260613_0002_market_data_tables.py`

Команды:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic downgrade -1
```

Через Makefile:

```powershell
make migrate
make migrate-down
```

## Current schema index

This document started as the bootstrap schema description. The current schema map also includes:

- `instrument_registry` with broker resolution fields: `instrument_uid`, `figi`, `class_code`, `ticker`, `lot_size`, `min_price_increment`, `resolution_status`, support flags and payloads.
- `market_candle` for raw 1m candles and derived bars.
- `corporate_action_event` for dividend/corporate-action facts.
- `market_special_day` for dividend gap and special-day classification.
- `dividend_sync_run` for T-Bank dividend sync status and readiness.
- `market_microstructure_snapshot` for data-only shadow spread/depth/imbalance/freshness facts.
- `historical_data_quality_report` for coverage and OHLC quality.
- `calibration_report` for calibration evidence and recommendations.
- `intraday_session_analytics` for current-day session/hour/instrument analytics.
- `rolling_performance_cube` for rolling contour statistics.
- `calibration_diagnostic_run` for no-trade, health and drift diagnostics.
- `strategy_config_candidate` for draft/proposal config candidates only.
- `market_regime_snapshot` for regime and drift snapshots.

Current migration source of truth is `packages/common/alembic/versions/` and SQLAlchemy models in `packages/common/src/trading_common/db/models.py`.

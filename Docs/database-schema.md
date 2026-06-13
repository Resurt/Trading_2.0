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

В стартовой миграции создаются default partitions:

- `fill_event_default`
- `audit_event_default`
- `blocker_event_default`
- `strategy_state_event_default`
- `counterfactual_result_default`

Следующие миграции могут добавлять месячные или дневные partitions без изменения application layer.

## Миграции

Текущая миграция:

- `20260613_0001_initial_postgres_schema.py`

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

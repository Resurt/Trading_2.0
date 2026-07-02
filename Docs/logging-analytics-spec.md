# Спецификация логирования и аналитики

Логирование в этом проекте нужно не только для отладки. Его главная цель - дать материал для:

- операционного контроля;
- отчетов;
- калибровки стратегии;
- анализа заблокированных сделок;
- анализа отмененных сделок;
- counterfactual-разбора: что было бы, если бы сделка не была заблокирована или отменена.

## Разделение контуров

| Контур | Хранилище | Назначение |
| --- | --- | --- |
| `technical logs` | stdout/stderr -> Fluent Bit -> Loki | Диагностика, ошибки, reconnect, latency, tracking id, rate limits, incidents. |
| `domain events` | PostgreSQL | Машинная аналитика, отчеты, replay, калибровка, blocked/cancelled trades. |
| `metrics` | Prometheus | Latency, counters, gauges, health checks, dashboard time series. |
| `reports` | PostgreSQL | Готовые hourly/daily агрегаты, metadata отчетов, counterfactual summaries. |

Сырые технические логи не должны становиться основным аналитическим источником в файлах или PostgreSQL.

## PostgreSQL analytics schema

Каноническое описание таблиц бизнес-фактов, partitioning, ключей корреляции и read-model helpers находится в [logging_analytics_schema.md](logging_analytics_schema.md). Этот документ является детальной схемой для `session_run`, `micro_session`, `signal_candidate`, `candidate_stage_result`, `blocker_event`, `order_intent`, `broker_order`, `order_state_event`, `fill_event`, `market_context_snapshot`, `counterfactual_result`, `hourly_report`, `daily_report` и `audit_event`.

### Intraday and calibration read models

The analytics layer also persists diagnostic read models:

- `intraday_session_analytics`: current-day session/hour/micro-session summaries by
  instrument, timeframe and side.
- `rolling_performance_cube`: rolling 7d/20d/60d/90d/180d/365d contour statistics.
- `calibration_diagnostic_run`: no-trade, robot-health and drift diagnostic runs.
- `strategy_config_candidate`: draft/proposal-only candidate configs.
- `market_regime_snapshot`: market regime, spread/depth/imbalance and drift snapshots.

These tables are built from domain events, market candles and data-only shadow microstructure.
They are not live decision sources. `strategy_config_candidate` approval changes candidate status
only; it must not mutate active `strategy_config` or runtime state.

Small samples are stored with warnings such as `small_sample_is_early_evidence_not_final_truth`.
10-20 trading days of data-only evidence can support investigation but must not permanently disable
timeframe/session/side/instrument contours.

Data-only lifecycle events are part of the calibration audit trail. One Start is a
daily intent and must emit structured `audit_event` rows for start, window close,
pause until next window, resume, day complete, stop, auto-stop and resume failure.
Payloads include `trading_date`, current window boundaries, `next_collection_window_at`,
requested/working instruments, `day_collection_state`, `collector_state`,
`readonly_calls_only=true`, `real_orders_disabled=true`, and
`strategy_trading_disabled=true`. Future data-only events must not include
misleading `uses_pseudo_orders=true` or `shadow_pseudo_order` metadata.

Operator Start control-plane events are also structured audit evidence. The API
first writes `robot_command_start_requested` with `reason_code=preflight_pending`.
`trade-core` then emits `data_only_shadow_preflight_started`,
`data_only_shadow_preflight_retrying` when broker readonly preflight is transiently
unavailable, `data_only_shadow_collection_started` on success, or
`robot_command_blocked_preflight`/`data_only_shadow_collection_preflight_blocked`
on a safe block. These events must include `command_id`, `preflight_phase`,
`readonly_calls_only=true`, `real_orders_disabled=true`, and
`strategy_trading_disabled=true`.

Primary calibration/logging tables must contain only valid active-window samples.
No `market_microstructure_snapshot` or `order_book_summary` rows may be written
between morning/main/evening windows, after final close, during official exchange
closure, or for OTC/dealer/indicative/stale/local-history display data. Known-invalid
primary rows are purged with a manifest and audit evidence, not merely hidden with
`not_for_calibration`.

Data-only microstructure persistence rejects invalid primary rows before insert.
The rejection audit action is `data_only_microstructure_row_rejected`; payloads
include `reason` with one of `crossed_book`, `invalid_spread`, `invalid_depth`,
`invalid_imbalance`, `missing_bid_ask`, `outside_session_window`, or
`non_calibration_source`. Historical deterministic metadata repair uses
`data_only_quality_rows_repaired`; invalid market-value purge uses
`data_only_invalid_rows_purged`. These actions never delete `audit_event` and do
not create `signal_candidate`, `order_intent`, `broker_order`, pseudo-orders, or
real broker calls.

Data-only exchange timestamp semantics:

- `exchange_ts` is the broker/exchange event timestamp when the source payload
  provides one.
- `received_ts` is the local receive/write timestamp.
- `received_ts` must never be copied into `exchange_ts`.
- If `exchange_ts` is absent, rows are marked
  `freshness_basis=received_ts_only`,
  `strict_dual_freshness_eligible=false`, and
  `exchange_ts_missing_reason` explains why.
- `received_ts_only` rows may be used for partial diagnostics, but strict
  dual-freshness calibration requires exchange-timestamp-confirmed rows.

Data-only trade tape persistence stores real broker market-trade events in
`market_trade_sample`. Stream events are preferred; if they do not produce
samples, the data-only collector may use bounded readonly `GetLastTrades`
polling inside the allowed collection window. Empty `GetLastTrades` responses or
missing stream trades produce explicit status/reason diagnostics and must not
create fake trade rows. Fallback rows remain `include_in_calibration=false` by
default.
Daily trend and summary reports expose `trade_tape_sample_count` and
`tape_confirmed_candidate_count`; on one-day diagnostics, missing tape means
windows are order-book/mid confirmed only.

## JSON structured logging

Технические логи пишутся в JSON через стандартный Python logging.

Контекст должен автоматически прокидываться через `contextvars`, `LoggerAdapter` или logging filters.

Реализация foundation:

- основной пакет: `trading_common.telemetry`;
- совместимый старый импорт: `trading_common.observability`;
- контекст: `contextvars` через `bind_context(...)` / `log_context(...)`;
- formatter: `JsonLogFormatter`;
- filters: `LogContextFilter` и `RedactionFilter`;
- настройка stdout JSON logs через `dictConfig`: `configure_logging(...)` или `configure_json_logging(service=...)`;
- helper API: `get_logger()`, `bind_context()`, `clear_context()`, `log_event(event_type=..., **payload)`;
- dev text formatter допустим через `TRADING_LOG_FORMAT=text`, production/default path остается JSON в stdout;
- endpoint `/metrics`: `TradingMetrics` + Prometheus exposition format.

Severity rule: `level` отражает техническую важность записи (`INFO`,
`WARNING`, `ERROR`), а доменный смысл всегда идет через `event_type`,
`event_name`, `stage_name` и `payload`. Нельзя кодировать бизнес-смысл только
через `WARNING`/`ERROR` или free-text `message`.

Redaction rule: `Authorization`, bearer/basic headers, tokens, passwords,
secrets, API keys и credential-like поля должны редактироваться фильтром до
попадания в stdout/Loki.

### Canonical log schema

Каждая строка technical log должна быть валидным JSON object.

Обязательные поля каждой JSON-строки:

- `ts_utc`
- `exchange_ts`
- `level`
- `service`
- `component`
- `event_type`
- `event_version`
- `session_type`
- `exchange_phase`
- `micro_session_id`
- `instrument`
- `timeframe`
- `strategy_id`
- `strategy_version`
- `candidate_id`
- `order_intent_id`
- `request_order_id`
- `exchange_order_id`
- `tracking_id`
- `latency_ms`
- `error_code`
- `error_message`
- `payload`

Поля присутствуют всегда. Если значение еще неизвестно, оно равно `null`.
Дополнительные поля совместимости (`logger`, `message`, `run_id`,
`session_phase`, `instrument_id`, `blocker_id`, `cancel_reason_code`,
`reject_reason_code`) могут присутствовать сверх обязательной схемы, но новые
интеграции должны читать канонические `exchange_phase` и `instrument`.

Каноническая структура:

```json
{
  "ts_utc": "2026-06-13T10:00:00.000000Z",
  "exchange_ts": "2026-06-13T10:00:00+03:00",
  "level": "INFO",
  "service": "trade-core",
  "component": "execution.engine",
  "event_type": "broker_order_posted",
  "event_version": "1",
  "session_type": "weekday_main",
  "exchange_phase": "continuous",
  "micro_session_id": "2026-06-13:weekday_main:1000",
  "instrument": "MOEX:SBER",
  "timeframe": "5m",
  "strategy_id": "baseline",
  "strategy_version": "v1",
  "candidate_id": "uuid",
  "order_intent_id": "uuid",
  "request_order_id": "uuid",
  "exchange_order_id": "string",
  "tracking_id": "string",
  "latency_ms": 42,
  "error_code": null,
  "error_message": null,
  "payload": {
    "event_name": "broker order posted",
    "stage_name": "post_order",
    "rate_limit_limit": 100,
    "rate_limit_remaining": 99,
    "rate_limit_reset": "2026-06-13T10:01:00+00:00"
  }
}
```

### Python helper API

```python
from trading_common.telemetry import bind_context, get_logger, log_event

logger = get_logger(__name__)

with bind_context(
    candidate_id=candidate_id,
    instrument="MOEX:SBER",
    timeframe="5m",
    strategy_version="v1",
    session_type="weekday_main",
    exchange_phase="continuous",
    micro_session_id=micro_session_id,
    order_intent_id=order_intent_id,
):
    log_event(
        logger=logger,
        event_type="order_intent_created",
        component="execution.engine",
        stage_name="intent_creation",
        latency_ms=12.5,
        order_type="limit",
    )
```

Legacy aliases `session_phase` and `instrument_id` пока поддерживаются для
существующего кода, но новый код должен использовать `exchange_phase` и
`instrument`.

### Preflight diagnostics

Session preflight reports and related technical logs should preserve broker
schedule/status diagnostics as structured fields when available:

- `schedule_source`
- `schedule_error_code`
- `schedule_error_message`
- `status_source`
- `status_success_count`
- `status_error_count`
- `fallback_used`
- `working_instruments`
- `blocked_instruments`

For T-Bank `TradingSchedules INVALID_ARGUMENT 30003`, record
`schedule_source=tbank_error`, `schedule_error_code=30003`, and keep the
original broker message in `schedule_error_message`. This error is not by itself
permission to start collection; fallback schedule decisions must be confirmed by
per-instrument broker trading status.

When broker schedules are syntactically valid but miss the active evening window
for the current calendar date while `GetTradingStatus` succeeds with exchange
trading available, preflight logs should keep the contradiction visible:
`source=broker_status_fallback_time_rules`,
`schedule_source=broker_trading_schedules_status_fallback`,
`fallback_used=true`, and warnings
`broker_schedule_missing_active_window` plus
`broker_status_open_schedule_closed`. If all broker status calls fail, the same
fallback window must not start collection.

### Strict event types

Строгие `event_type` для доменных событий и корреляции логов:

- `signal_candidate_created`
- `candidate_stage_result_recorded`
- `market_context_snapshot_written`
- `blocker_triggered`
- `order_intent_created`
- `broker_order_posted`
- `broker_order_updated`
- `broker_order_cancelled`
- `order_state_changed`
- `fill_received`
- `strategy_state_changed`
- `risk_event_recorded`
- `session_snapshot_written`
- `strategy_config_loaded`
- `strategy_config_reloaded`
- `strategy_config_reload_failed`
- `stream_gap_recovery_requested`
- `stream_gap_backfill_started`
- `stream_gap_backfill_completed`
- `stream_gap_recovery_failed`
- `order_reconciliation_completed`
- `position_reconciliation_completed`
- `runtime_emergency_cancel_failed`
- `market_status_changed`
- `bar_closed`
- `stream_gap_recovery_completed`

Свободный текст может быть в `message`, но смысл события должен задаваться `event_type` и structured fields.

### Loki labels scheme

Loki labels должны быть низкой/ограниченной кардинальности:

- `job`
- `environment`
- `container_name`
- `source`
- `service`
- `level`
- `event_type`
- `session_type`
- `exchange_phase`
- `instrument`
- `timeframe`

Запрещено выносить в Loki labels значения с высокой кардинальностью:

- `run_id`
- `candidate_id`
- `blocker_id`
- `order_intent_id`
- `request_order_id`
- `exchange_order_id`
- `tracking_id`
- exception text

Эти поля остаются внутри JSON body и доступны через log query/parsing, но не индексируются как labels.

## Канонический контекст domain events

Доменные события в PostgreSQL должны быть пригодны для машинного анализа. Базовая структура:

```yaml
event_core:
  event_id: uuid
  ts_utc: datetime
  exchange_ts: datetime
  trading_date: date
  calendar_date: date
  service: string
  event_type: string
  severity: string

session_context:
  session_type: weekend | weekday_morning | weekday_main | weekday_evening
  session_phase: opening_auction | continuous_trading | closing_auction | break | dealer_mode | closed
  micro_session_id: string
  broker_trading_status: string
  run_id: uuid

market_context:
  instrument_id: string
  ticker: string
  timeframe: 5m | 10m | 15m
  last_price: decimal
  mid_price: decimal
  spread_abs: decimal
  spread_bps: decimal
  market_quality_score: decimal
  book_imbalance: decimal
  candle_age_ms: int
  data_freshness_ms: int

strategy_context:
  strategy_id: string
  strategy_state: string
  candidate_id: uuid
  blocker_id: uuid
  blocker_code: string
  blocker_rank: int
  expected_edge_bps: decimal
  expected_holding_minutes: int

execution_context:
  order_intent_id: uuid
  request_order_id: uuid
  exchange_order_id: string
  order_type: string
  lot_qty: int
  price: decimal
  time_in_force: string
  cancel_reason_code: string
  reject_reason_code: string

analytics_context:
  fee_bps_assumed: decimal
  slippage_bps_assumed: decimal
  mfe_5m_bps: decimal
  mae_5m_bps: decimal
  mfe_10m_bps: decimal
  mae_10m_bps: decimal
  mfe_15m_bps: decimal
  mae_15m_bps: decimal
  would_profit_5m: bool
  would_profit_10m: bool
  would_profit_15m: bool
```

## Domain event tables

Минимальные event/domain таблицы:

- `session_run`
- `signal_candidate`
- `blocker_event`
- `order_intent`
- `broker_order`
- `fill_event`
- `risk_event`
- `position_snapshot`
- `market_candle`
- `market_status_snapshot`
- `order_book_summary`
- `strategy_state_event`
- `hourly_report`
- `daily_report`
- `robot_command`
- `report_job_outbox`
- `counterfactual_result`
- `audit_event`

### `signal_candidate`

Фиксирует потенциальный вход или выход до прохождения risk/execution gates.

Минимум:

- `candidate_id`;
- `strategy_id`;
- версия стратегии;
- инструмент;
- таймфрейм;
- сессионный контекст;
- рыночный контекст;
- ожидаемый edge после комиссий и slippage assumptions;
- сторона сделки;
- ожидаемое окно удержания.

### `blocker_event`

Фиксирует результат каждой проверки и финальный блокер.

Нельзя хранить только строку `blocked`. Нужна причинная цепочка:

- `candidate_id`;
- `gate_name`;
- `gate_rank`;
- `passed`;
- `reason_code`;
- `reason_payload`;
- признак финального blocker;
- рыночный контекст;
- сессионный контекст.

### `order_intent`

Фиксирует внутреннее намерение отправить, отменить, заменить или пропустить ордер.

Минимум:

- `order_intent_id`;
- `candidate_id`;
- side;
- order type;
- lot quantity;
- intended price;
- time in force;
- `request_order_id`;
- версия execution policy.

### `broker_order`

Фиксирует жизненный цикл брокерского ордера.

Минимум:

- `request_order_id`;
- `exchange_order_id`;
- broker status;
- posted/cancelled/rejected timestamps;
- reject reason;
- broker tracking id.

### `fill_event`

Фиксирует исполнение или частичное исполнение.

Минимум:

- broker order ids;
- fill id;
- quantity;
- price;
- commission;
- exchange timestamp;
- received timestamp.

### `counterfactual_result`

Фиксирует результат постфактум-анализа для blocked/cancelled candidates.

Минимальные окна:

- 5 минут;
- 10 минут;
- 15 минут.

Хранить:

- MFE;
- MAE;
- `would_profit_5m`;
- `would_profit_10m`;
- `would_profit_15m`;
- комиссии и slippage assumptions;
- ссылку на исходный `candidate_id` или `order_intent_id`.

## Blocker taxonomy

Первичные `blocker_code`:

- `spread_too_wide`
- `market_quality_low`
- `stale_market_data`
- `no_edge_after_costs`
- `risk_budget_exceeded`
- `session_forbidden`
- `phase_forbidden`
- `order_type_forbidden`
- `weekend_broker_mode`
- `max_drawdown_reached`
- `open_order_conflict`
- `position_limit_reached`
- `short_not_allowed_by_config`
- `short_not_allowed_by_broker`
- `insufficient_margin`
- `max_short_exposure_reached`
- `max_long_exposure_reached`
- `total_costs_exceed_edge`
- `position_side_conflict`
- `position_state_stale`
- `position_reconciliation_mismatch`
- `instrument_not_tradable`
- `broker_status_forbidden`
- `rate_limit_pressure`
- `missing_closed_candle`
- `strategy_disabled`

Первичные `cancel_reason_code`:

- `hourly_rollover`
- `exchange_session_boundary`
- `strategy_exit`
- `risk_reduction`
- `stale_order`
- `price_moved`
- `manual_operator_action`
- `broker_reject_followup`

Первичные `reject_reason_code`:

- `broker_rejected`
- `inappropriate_trading_session`
- `insufficient_balance`
- `invalid_lot_size`
- `order_type_not_allowed`
- `instrument_not_available`
- `rate_limit_exceeded`
- `transport_error`
- `unknown_broker_error`

Новые reason codes можно добавлять только вместе с обновлением этой спецификации и тестов.

## Counterfactual analytics

Counterfactual pipeline отвечает на вопросы:

- дала бы blocked сделка прибыль через 5/10/15 минут;
- была ли отмена ордера оправданной;
- слишком ли строгий `spread_too_wide`;
- слишком ли строгий `market_quality_low`;
- сколько потенциальных сделок ушло из-за `session_forbidden`;
- сколько возможностей было потеряно из-за stale market data или broker status.

Для каждого blocked/cancelled случая нужно сохранять:

- исходный `candidate_id`;
- исходный `blocker_code` или `cancel_reason_code`;
- market snapshot;
- `counterfactual_seed_snapshot` как отдельный `market_context_snapshot`
  рядом с финальным blocker/cancel event;
- fee/slippage assumptions;
- MFE/MAE по окнам 5/10/15 минут;
- итог `would_profit_*`.

### Long/short cost assumptions

Для акций default commission assumption не может быть ниже `5 bps` на сторону.
Round trip commission не может быть ниже `10 bps`. Полные expected costs для
risk gate:

```text
total_expected_costs_bps =
  max(commission_bps_per_side, 5) * 2
  + current_spread_bps
  + max(assumed_slippage_bps, 0)
```

Если `expected_edge_bps - total_expected_costs_bps` меньше
`min_edge_after_total_costs_bps`, пишется:

- `candidate_stage_result.stage_name=total_expected_costs`;
- `blocker_event.blocker_code=total_costs_exceed_edge`;
- `blocker_event.measured_value=edge_after_total_costs_bps`;
- `blocker_event.threshold_value=min_edge_after_total_costs_bps`.

Short opportunities дополнительно проходят gates
`short_allowed_by_config`, `short_allowed_by_account`,
`short_allowed_by_instrument`, `margin_or_collateral_available` и
`forced_cover_policy`. Это позволяет отличать запрет short из конфигурации от
запрета брокера/аккаунта и от нехватки обеспечения.

### Counterfactual algorithm v1

Реализация находится в `report_worker.analytics`.

Вход:

- blocked `signal_candidate` с финальным `blocker_event`;
- cancelled `order_intent` с `cancel_reason_code`;
- closed `market_candle` после времени события;
- assumptions: `fee_bps`, `slippage_bps`, `take_profit_bps`, `stop_loss_bps`.

Окна:

- 5 минут;
- 10 минут;
- 15 минут.

Для long/buy:

- `MFE = (max(high_price) - entry_price) / entry_price * 10000`;
- `MAE = (min(low_price) - entry_price) / entry_price * 10000`;
- `close_return = (last_close - entry_price) / entry_price * 10000`.

Для short/sell:

- `MFE = (entry_price - min(low_price)) / entry_price * 10000`;
- `MAE = (entry_price - max(high_price)) / entry_price * 10000`;
- `close_return = (entry_price - last_close) / entry_price * 10000`.

Затем:

- `theoretical_pnl_bps = close_return - fee_bps - slippage_bps`;
- `theoretical_pnl_rub = entry_price * lot_qty * theoretical_pnl_bps / 10000`;
- `would_profit = theoretical_pnl_bps > 0`;
- `tp_hit = MFE >= take_profit_bps`;
- `sl_hit = MAE <= -stop_loss_bps`.

Результат сохраняется в `counterfactual_result`:

```json
{
  "source_event_type": "blocked_candidate",
  "candidate_id": "uuid",
  "order_intent_id": null,
  "instrument_id": "MOEX:SBER",
  "strategy_id": "baseline",
  "blocker_code": "spread_too_wide",
  "cancel_reason_code": null,
  "fee_bps_assumed": "2.0",
  "slippage_bps_assumed": "2.0",
  "mfe_5m_bps": "100.0000",
  "mae_5m_bps": "-50.0000",
  "mfe_10m_bps": "150.0000",
  "mae_10m_bps": "-50.0000",
  "mfe_15m_bps": "200.0000",
  "mae_15m_bps": "-50.0000",
  "would_profit_5m": true,
  "would_profit_10m": true,
  "would_profit_15m": true,
  "result_payload": {
    "algorithm": "mfe_mae_directional_close_after_fees_slippage_v1",
    "windows": {
      "5": {
        "tp_hit": true,
        "sl_hit": false,
        "theoretical_pnl_bps": "76.0000",
        "theoretical_pnl_rub": "7.6000"
      }
    }
  }
}
```

## Hourly report

Hourly report строится при закрытии `micro_session`.

Минимальное содержимое:

- realised/unrealised PnL;
- комиссии;
- estimated slippage;
- число сигналов;
- число входов/выходов;
- fill ratio;
- rejects/cancels/replaces;
- reconnect count;
- API/broker errors;
- risk blockers;
- stale market data incidents;
- missed/late candles;
- max drawdown за час;
- idle time;
- latency histograms;
- список risk events.

Формат `report_payload`:

```json
{
  "format": "hourly_report_v1",
  "estimated_slippage": "0.2000",
  "replace_count": 0,
  "posted_count": 1,
  "filled_count": 1,
  "broker_error_count": 0,
  "risk_blockers": {
    "spread_too_wide": 1
  },
  "stale_market_data_incidents": 0,
  "latency_ms": {
    "count": 1,
    "p50": "120.0000",
    "p95": "120.0000"
  },
  "funnel": {
    "candidates": 1,
    "blockers": 1,
    "approved": 0,
    "posted": 1,
    "filled": 1,
    "profitable": 1
  }
}
```

## Daily report

Daily report строится `report-worker` по `trading_date`.

Обязательные блоки:

1. `market regime` - день был long/short/mixed/ranging.
2. `candidate funnel` - сколько кандидатов появилось и где они отсеялись.
3. `blocker ranking` - какие blocker codes чаще и дороже всего блокировали сделки.
4. `execution quality` - fill ratio, cancel ratio, median/p95 latency, rejects.
5. `counterfactual` - outcomes blocked/cancelled cases через 5/10/15 минут после комиссий и slippage assumptions.
6. `session segmentation` - morning/main/evening/weekend.
7. `infra health` - reconnects, stale data, broker/API errors, rate limit pressure.

### Day trend classification v1

Алгоритм:

1. Берем closed `market_candle` за `trading_date`.
2. Для каждого `instrument_id` считаем доходность от первого `open_price` до последнего `close_price`.
3. Считаем равновзвешенное среднее по инструментам.
4. Если среднее `>= +25 bps`, классификация `long_bias`.
5. Если среднее `<= -25 bps`, классификация `short_bias`.
6. Иначе `mixed_flat`.

Алгоритм детерминированный и сохраняет `instrument_returns_bps` в `report_payload.trend`.

### Daily report JSON

`daily_report.report_payload`:

```json
{
  "format": "daily_report_v1",
  "trend": {
    "market_regime": "long_bias",
    "average_return_bps": "175.0000",
    "instrument_returns_bps": {
      "MOEX:GAZP": "150.0000",
      "MOEX:SBER": "200.0000"
    },
    "algorithm": "daily_first_open_to_last_close_equal_weight_v1"
  },
  "summary_by_session_type": {
    "weekday_main": {
      "signal_count": 10,
      "entry_count": 7,
      "exit_count": 3,
      "blocked_count": 4
    }
  },
  "summary_by_instrument": {},
  "summary_by_timeframe": {},
  "blocker_ranking": [
    {
      "reason_code": "spread_too_wide",
      "count": 3
    }
  ],
  "execution_quality": {
    "posted_count": 6,
    "filled_count": 4,
    "reject_count": 1,
    "cancel_count": 1,
    "replace_count": 0,
    "fill_ratio": "0.6667"
  },
  "missed_opportunity_summary": {
    "would_profit_5m": 2,
    "would_profit_10m": 3,
    "would_profit_15m": 3,
    "total_counterfactuals": 5
  },
  "strategy_state_time_distribution_seconds": {
    "candidate": 20.0,
    "wait": 3400.0
  },
  "funnel": {
    "candidates": 10,
    "blockers": 4,
    "approved": 6,
    "posted": 6,
    "filled": 4,
    "profitable": 2
  }
}
```

### Celery tasks

Канонические task names:

- `report_worker.build_hourly_report`
- `report_worker.build_daily_report`
- `report_worker.rebuild_reports_for_date`
- `report_worker.run_counterfactual_analysis_for_date`

`report_worker.rebuild_reports_for_date(trading_date, strategy_id, include_counterfactual=True)`
перестраивает hourly reports по закрытым `session_run`, при необходимости запускает
counterfactual analysis и затем строит daily report уже с `missed_opportunity_summary`.

Задачи берут `DATABASE_URL` или Postgres env/secrets через `trading_common.db.config`.
Redis используется как Celery broker/result backend через:

- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`

Hourly scheduling не должен опираться на distant future Celery `eta/countdown`.
`trade-core` закрывает micro-session и ставит ближайшую задачу отчета.

### CLI

Ручные команды:

```bash
python scripts/run_hourly_report.py --micro-session-id 2026-06-12:weekday_main:1000 --strategy-id baseline
python scripts/run_daily_report.py --trading-date 2026-06-12 --strategy-id baseline
python scripts/run_counterfactual.py --trading-date 2026-06-12 --strategy-id baseline
```

### Frontend read models

Read models формируются из materialized report rows:

- `hourly_report.report_payload`;
- `daily_report.report_payload`;
- `counterfactual_result.result_payload`.

Python helpers:

- `hourly_report_read_model`;
- `daily_report_read_model`;
- `counterfactual_read_model`.

## Metrics

Histograms:

- `broker_post_order_latency_seconds`
- `order_state_convergence_seconds`
- `candle_close_delivery_lag_seconds`
- `session_rollover_duration_seconds`
- `report_generation_duration_seconds`

Counters:

- `stream_reconnect_total`
- `rejected_orders_total`
- `risk_events_total`
- `counterfactual_jobs_total`
- `report_jobs_failed_total`

Gauges:

- `open_orders`
- `active_positions`
- `market_stream_alive`
- `last_stream_message_age_seconds`
- `celery_queue_backlog`
- `emergency_stop_total`
- `emergency_cancel_failed_total`
- `working_orders_after_stop`
- `gap_recovery_duration_seconds`
- `recovered_candles_total`
- `reconciliation_mismatch_total`

### Launch readiness audit fields

`trade-core` startup logs and `audit_event` must include:

- `database_backend`: expected `postgresql` in compose/sandbox/shadow/production;
- `database_url_redacted`: credentials must be replaced with `***`;
- `runtime_mode`;
- `strategy_id`;
- `strategy_version`;
- `resolved_instrument_count`.

SQLite is allowed only when `TRADING_RUNTIME_LOCAL_SQLITE=1` is set explicitly for local experiments. Compose readiness must fail if `trade-core`, `api` and `report-worker` are not pointed at the same PostgreSQL config.

CI data-only smoke tests must isolate their database state with an explicit
`database_url` fixture or CLI option. They must not pass because a developer
machine happens to expose `secrets/postgres_password`, and Docker/CI jobs must
not silently fall back to SQLite unless the test or local experiment requested
that database explicitly.

Strategy config reloads are domain/audit facts, not only technical logs. Events `strategy_config_loaded`, `strategy_config_reloaded` and `strategy_config_reload_failed` must carry strategy id, version, session template and machine-readable failure code when present.

Prometheus labels не должны содержать raw ids, exception text, arbitrary order id,
candidate id, request id или tracking id. Все такие значения остаются в JSON body логов
и в нормализованных таблицах PostgreSQL.

### Prometheus label scheme

Разрешенные bounded labels:

- `service`
- `instrument`
- `timeframe`
- `session_type`
- `stream_type`
- `status`
- `result`

Запрещенные labels:

- raw order ids;
- candidate ids;
- blocker ids;
- request/exchange order ids;
- exception text;
- произвольный free-text reason.

Совместимость: старые helper-вызовы `inc_reconnect(stream_name=...)`,
`inc_rejected_order(reason_code=...)`, `inc_risk_event(reason_code=...)`,
`set_active_positions(..., instrument_id=...)` и
`set_last_closed_candle_age(..., instrument_id=..., timeframe=...)` могут сохраняться
в коде как aliases, но наружу они должны экспонировать новые metric names и labels.

### Alert rules

Prometheus rule files лежат в `deploy/prometheus/rules/*.yml` и подключаются через
`deploy/prometheus/prometheus.yml`. Базовый набор alert scenarios:

- `TradingServiceDown`;
- `TradingServiceMissingMetrics`;
- `BrokerPostOrderLatencyHigh`;
- `MarketStreamDown`;
- `MarketStreamStale`;
- `StreamReconnectSpike`;
- `SessionRolloverSlow`;
- `ReportGenerationFailures`;
- `CounterfactualJobFailures`;
- `CeleryQueueBacklogHigh`;
- `RejectedOrdersSpike`;
- `RiskEventsSpike`.

### Grafana dashboards

Provisioning находится в `deploy/grafana/provisioning`.

Dashboard files:

- `observability-stack.json`
- `broker-api-health.json`
- `market-data-health.json`
- `order-execution-quality.json`
- `risk-blockers.json`
- `session-rollovers.json`
- `trading-overview.json`

## Controlled launch context

Для replay/sandbox/shadow/production разборов в analytics payload нужно сохранять:

- `launch_mode`;
- `order_submission_mode`;
- `real_broker_call`;
- `real_order_block_reason_code`.

В `historical_replay` и `shadow` execution layer сохраняет pseudo-order lifecycle без реального broker call. Эти записи пригодны для funnels и counterfactual analysis, но не должны интерпретироваться как реальное execution quality.

В `sandbox` real sandbox orders также выключены по умолчанию. Для отправки
реальной sandbox-заявки нужна явная policy:

```text
TRADING_SANDBOX_ORDERS_CONFIRM=I_UNDERSTAND_SANDBOX_ORDERS
```

Без подтверждения `order_submission_mode=sandbox_pseudo_order`, а
`real_order_block_reason_code=sandbox_orders_not_confirmed`.

## Reporting analytics v2

Аналитика отчетов реализуется в `report_worker.analytics` и запускается только из
`report-worker` Celery tasks или CLI scripts. FastAPI не считает тяжелые отчеты
inline и не использует `BackgroundTasks` для этих расчетов.

Canonical Celery tasks:

- `report_worker.build_hourly_report`
- `report_worker.build_daily_report`
- `report_worker.rebuild_reports_for_date`
- `report_worker.run_counterfactual_analysis_for_date`

Canonical CLI scripts:

```bash
python tools/reports/build_hourly_report.py --date 2026-06-13 --strategy-id baseline
python tools/reports/build_daily_report.py --date 2026-06-13 --strategy-id baseline --force-rebuild
python tools/reports/run_counterfactual_analysis.py --date 2026-06-13 --strategy-id baseline
```

Общие CLI filters:

- `--date`
- `--instrument`
- `--timeframe`
- `--session-type`
- `--strategy-version`
- `--force-rebuild`

Daily report `market_regime` v2:

- `trend_up` - first-open to last-close return >= +25 bps.
- `trend_down` - first-open to last-close return <= -25 bps.
- `choppy` - absolute return is flat, but intraday range >= 80 bps.
- `flat` - all other cases.

Классификация считается отдельно по `instrument_id + timeframe` и затем
агрегируется в daily summary. Payload хранит `regime_by_instrument_timeframe`,
`scope_returns_bps`, `scope_range_bps` и объяснение алгоритма.

Candidate funnel v2:

```text
created -> passed_gates -> blocked -> order_intent -> posted -> filled -> exited
```

Blocker ranking v2 хранит:

- количество срабатываний;
- missed gross/net PnL по counterfactual;
- avoided loss;
- false positive rate по горизонту 15 минут;
- counterfactual count.

Canceled-order analytics v2 считается отдельным блоком daily report и группирует
отмены по `cancel_reason_code`, включая missed gross/net PnL и avoided loss.

Counterfactual scenarios v2:

- `blocked-as-if-entered`
- `kept-limit-order`
- `aggressive-fill`

Горизонты остаются `+5m`, `+10m`, `+15m`. Для каждого окна сохраняются MFE/MAE,
gross PnL и net PnL. Комиссия параметризуется через `AnalyticsAssumptions`;
default для акций: `0.05%` на сторону, то есть `10 bps` round trip. Slippage
остается отдельным допущением.

Frontend outputs:

- JSON остается основным structured payload в `hourly_report.report_payload`,
  `daily_report.report_payload`, `counterfactual_result.result_payload`;
- HTML preview хранится в `html_output` внутри payload и возвращается read models
  как поле `html`.

## Historical replay analytics extensions

Для подготовки калибровки добавлен отдельный historical contour поверх
PostgreSQL domain tables:

- `historical_data_quality_report` хранит агрегированный отчёт качества
  `market_candle`: coverage, expected/actual candle count, missing intervals,
  duplicate count, invalid OHLC count, abnormal gaps, source/session/timeframe
  distributions.
- `calibration_report` хранит итоговую витрину по candidates, blockers,
  pseudo-orders, counterfactual PnL proxy, cost sensitivity и recommended
  threshold changes. Этот отчёт не меняет `strategy_config` автоматически.
- `counterfactual_result.result_payload.source=historical_counterfactual_rebuild`
  отделяет historical пересчёт от live/shadow/sandbox аналитики.
- Replay факты имеют `source=historical_db_replay` в payload, а
  `signal_candidate.signal_fingerprint` строится детерминированно из
  `strategy_id|strategy_version|instrument_id|timeframe|bar_close_ts|side|action|historical_db_replay`.

Quality/replay/calibration CLI должны печатать JSON summary и не писать raw
technical logs как аналитический источник. Источник истины для отчётов и
калибровки остаётся PostgreSQL.

Historical replay execution policy:

- runtime mode только `historical_replay`;
- `PostOrder` и `CancelOrder` запрещены на уровне fake gateway и launch policy;
- успешный risk path создаёт `order_intent`, pseudo `broker_order` и
  `order_state_event`;
- blocked/rejected/cancelled paths получают `counterfactual_result` по
  горизонтам `+5m`, `+10m`, `+15m` с assumptions `commission>=5 bps per side`
  и round-trip fee минимум `10 bps`.
## Corporate Actions And Calibration Cleanliness

Calibration facts include two additional domain tables:

- `corporate_action_event` - manual/csv/api/synthetic facts for dividend, split,
  reverse split and other corporate actions;
- `market_special_day` - per instrument/trading_date flags for `dividend_gap_day`,
  `corporate_action_day`, `abnormal_gap_day` and `excluded_from_calibration`.

Every historical replay payload generated on a special day must include:

- `special_day_type`;
- `corporate_action_flag`;
- `dividend_gap_day`;
- `abnormal_gap_day`;
- `excluded_from_primary_calibration`;
- `special_day_policy`;
- `eligible_for_live_calibration`.

Primary calibration must set `calibration_clean=false` if special-day classification is
missing. A clean primary calibration requires `calibration_scope=primary_normal_days` and
excluded dividend/corporate-action days. Recommendations remain report payload only and
must not auto-update `strategy_config`.

## Dividend Sync Analytics

Corporate-action analytics now distinguish source quality:

- `source=api_import`: primary path from T-Bank `GetDividends`;
- `source=manual`, `csv_import`, `manual_unverified`: fallback/override only;
- `source=synthetic_test`: tests only.

Historical quality report payload includes:

- `dividend_sync_status`;
- `api_import_dividend_events_count`;
- warning `manual_corporate_actions_only` when manual events exist without `api_import`;
- warning `dividend_sync_missing` when no broker dividend sync exists for the period.

Calibration report payload includes:

- `dividend_sync_status`;
- `future_dividend_windows_count`;
- warning `future_dividend_window_present` when upcoming ex-date risk exists;
- `calibration_clean=false` unless `api_import` dividend sync is complete or the operator
  explicitly runs with `--allow-manual-corporate-actions`.

Technical JSON logs are not the analytics source of truth. Dividend sync, special days,
replay decisions, blockers and calibration facts must be persisted in PostgreSQL domain
tables and report payloads.

## Instrument Resolution Events

`instrument_registry` stores broker-resolution state for every enabled instrument.
The internal `instrument_id` remains stable (`MOEX:SBER`), while real T-Bank calls
use `instrument_uid`/`figi`.

Runtime and readiness should emit/write these audit events:

- `instrument_resolution_started`;
- `instrument_resolution_completed`;
- `instrument_resolution_failed`;
- `unresolved_instrument_blocked_startup`.

Machine-readable failure code:

- `instrument_not_resolved_for_broker_call`.

This code must be present when dividend sync, historical backfill, market streams
or order placement would otherwise send an internal `MOEX:*` id to T-Bank.

## Data-only Shadow Events

Data-only shadow writes market microstructure facts to PostgreSQL and technical logs to JSON. The
analytics source of truth remains PostgreSQL, not logs.

Required event names:

- `data_only_shadow_started`;
- `data_only_shadow_strategy_disabled`;
- `data_only_shadow_stopped`;
- `live_data_collector_started`;
- `live_candle_received`;
- `live_order_book_snapshot_written`;
- `live_market_snapshot_written`.

Required metrics:

- `data_only_shadow_enabled`;
- `order_book_snapshots_total`;
- `market_microstructure_snapshots_total`;
- existing `market_stream_alive`, `last_stream_message_age_seconds`,
  `candle_close_delivery_lag_seconds`, and `stream_reconnect_total`.

Prometheus labels must stay bounded to service/instrument/status/stream/timeframe. Do not export
candidate ids, order ids, snapshot ids or broker tracking ids as metric labels.

## Session preflight, balance and observatory events

New structured events/payloads:

- `trading_session_preflight`: emitted around session/calendar checks before live data-only smoke.
- `market_closed_expected`: closed market by broker/fallback schedule, with `next_session_at`.
- `data_only_shadow_preflight`: preflight-only smoke result before stream startup.
- `market_data_availability_probe`: bounded readonly `GetLastPrices` and selected
  `GetOrderBook` probe used only by preflight when broker schedules are empty,
  missing, incomplete, or statuses are unavailable during a local open window.
  It must never call order APIs.
- `balance_refresh`: broker account/portfolio read model update; full account id must not be logged.
- `market_quotes_refresh`: explicit readonly T-Invest `GetLastPrices`/`GetOrderBook`
  refresh for dashboard quotes; no order methods are allowed. Payload should include
  `instrument_count`, `live_rows`, `stale_rows`, `cache_ttl_seconds`, timeout/fallback
  reason when present, `quotes_only`, `include_order_book`, and whether
  `/market/overview` cache overlay was updated.
- `dashboard_market_feed_snapshot`: readonly display feed refresh for the Live
  Dashboard. It may include `GetLastPrices` for the quote board and `GetOrderBook` /
  last trades only for the selected instrument. Payload should include
  `selected_instrument`, `quote_rows_count`, `order_book_available`,
  `trade_tape_status`, `trade_tape_reason`, `last_refresh_at`, `warnings`, and
  `errors`. It must distinguish `received_ts` from `exchange_ts` and expose
  `received_age_ms`, `exchange_age_ms`, `stale_by_exchange_time`,
  `freshness_status`, and `freshness_reason`. This event is display-only and must
  not write `market_microstructure_snapshot` calibration logs.
  `order_book_available=true` requires actual selected `bids[]` and `asks[]`
  arrays with at least five levels per side; a one-row top-of-book snapshot or
  `depth_levels` metadata alone is a partial/loading state.
- `market_instrument_details_read`: local/BFF selected-instrument read for
  `/market/instruments/{instrument_id}/details`. Payload should include
  `instrument_id`, `quote_source`, `quote_status`, `order_book_source`,
  `order_book_stale`, `market_trades_source`, and any `schedule_error_code` or
  status/freshness reason already present in the read model. This event is a
  read-model/live-display access, not a data-only collection start.
- `robot_command_rejected_preflight`: API rejected a Start command because preflight did
  not return `market_open=true` and `data_only_collection_allowed=true`; payload
  includes `reason_code` and `preflight_result`.
- `data_only_shadow_collection_started`: trade-core applied Start in data-only mode and
  started the minimal data-only market stream set after `market_open=true` and
  `data_only_collection_allowed=true` preflight. `trading_allowed` remains false.
  Payload includes `polling_fallback_enabled` and
  `order_book_poll_interval_seconds`.
- `data_only_order_book_poll_completed`: bounded readonly polling fallback wrote or
  attempted current order-book samples while data-only collector was running. Payload
  includes `successful_instruments`, `failed_instruments`, `readonly_calls_only`,
  `real_orders_disabled`, `strategy_trading_disabled`, and
  `include_in_calibration`.
- `data_only_stream_gap_recovery_skipped`: data-only stream reconnect skipped candle
  gap backfill because `data_only_polling_fallback_active` handles live
  microstructure without aggressive `GetCandles` retries.
- `data_only_position_snapshot_skipped`: trade-core skipped account-level position
  snapshotting during data-only collection. Balance diagnostics remain explicit
  readonly operator actions and are not part of Start.
- `data_only_shadow_collection_stopped`: trade-core applied Stop/Pause/Emergency Stop
  and stopped market streams/polling without order actions.
- `data_only_shadow_collection_auto_stopped`: trade-core stopped data-only
  streams/polling because the fresh preflight collection window closed or data-only
  collection was no longer allowed. Payload should include `reason_code`,
  `current_window_end_at`, `readonly_calls_only=true`, `real_orders_disabled=true`,
  and `strategy_trading_disabled=true`.
- `data_only_shadow_collection_preflight_blocked`: trade-core received Start without
  an open-market preflight and did not start streams.
- `intraday_analytics_rebuild`: rebuild of `intraday_session_analytics`.
- `calibration_observatory_run`: diagnostic run for rolling cube, regime and candidate proposals.

Required payload fields for preflight: `market_window_open`, `market_open`,
`market_closed_expected`, `data_only_collection_allowed`, `trading_allowed`,
`blocking_layer`, `now_msk`, `trading_date`, `calendar_date`, `session_type`,
`session_phase`, `broker_trading_status`, `api_trade_available`,
`next_session_at`, `reason_code`, `instruments_checked`,
`per_instrument_status`, `source`, `broker_schedule_windows_count`,
`fallback_reason`, `market_data_probe_success_count`,
`market_data_probe_error_count`, and `market_data_probe`.

Probe payloads include `instrument_id`, `last_price_available`,
`order_book_available`, `market_data_available`, `latency_ms`, `source`,
`error_code`, and `error_message`. If a local fallback window is open but both
broker status and market data probe are unavailable, the blocking reason is
`broker_status_and_market_data_unavailable`, not a false schedule-closed code.

`market_microstructure_snapshot.snapshot_payload` must carry
`include_in_calibration`, `calibration_allowed`, `venue_type`, and
`data_only_polling_fallback=true` when the snapshot was produced by readonly
polling rather than a stream order-book event.

Required balance payload fields: `total_portfolio_value_rub`, `available_cash_rub`, `blocked_cash_rub`, `expected_yield_rub`, `free_collateral_rub`, `account_id_masked`, `balance_currency`, `last_balance_refresh_at`, `balance_freshness_seconds`, `balance_degraded`, `balance_degraded_reason_code`.

Intraday analytics and Calibration Center payloads remain diagnostic-only. Candidate config proposal events must state that configs are draft/proposal only and are not applied automatically.

For data-shadow intraday analytics, a stale `session_run` row alone is not a
sample. If fresh/runtime evidence has zero `market_microstructure_snapshot`
samples and no other facts for the requested scope, the session summary must use
`session_phase=closed`, `session_status=no_samples`,
`analytics_payload.data_status=no_samples`,
`analytics_payload.calibration_eligible=false`, and
`no_trade_reason=market_closed_or_no_samples`. It must not persist a misleading
`continuous_trading` row from a stale runtime session.

For data-shadow intraday analytics, calibration metrics must be built from
calibration-eligible microstructure only. Rows outside the known session window
are rejected as `late_after_session_close` even if older snapshot payloads
incorrectly contain `include_in_calibration=true` or `calibration_allowed=true`.
Analytics payloads expose `microstructure_rows_total`,
`calibration_microstructure_rows`, `calibration_rejected_rows`, and
`calibration_rejection_reasons`.

Known-invalid primary market-data rows must not remain in PostgreSQL primary
calibration/logging tables. Rows created after session close, during an official
exchange closed override, in OTC/dealer/indicative mode, from stale/local
history, or with bugged session context must be purged from
`market_microstructure_snapshot` and dependent primary read models after a
manifest is written. Diagnostic metadata may remain in `.local` reports and
`audit_event`, but the invalid rows themselves are not kept as rejected
calibration rows. A purge writes an audit event with
`action=data_only_invalid_rows_purged`, reason, cutoff, affected tables, deleted
counts, and manifest path.
## Market Source And Quality Events

Market-source payloads must include official exchange and venue context:
`official_exchange_open`, `official_exchange_closed`, `venue_type`, `trading_mode`,
`quote_source`, `quote_allowed_for_data_collection`, and `include_in_calibration`.
Broker OTC/indicative quotes are display-only by default and must not silently enter
calibration reports.

Dashboard Live Feed and data-only collection remain separate. Dashboard feed freshness
fields (`last_refresh_at`, `received_ts`, `exchange_ts`, `received_age_ms`,
`exchange_age_ms`, `order_book_age_ms`, `market_trades_source`,
`trade_tape_status`, `trade_tape_reason`, `selected_order_book_stale`) are display
diagnostics. Persistent calibration evidence is created only by data-only
collection after Start/preflight and should be visible as
`market_microstructure_snapshot` deltas. A broker response received now does not
make an old exchange timestamp calibration-eligible.
After an official close, dashboard session metadata must be `closed/closed`.
Broker OTC/indicative quotes stay display-only venue/source metadata and must not
carry `include_in_calibration=true` or resurrect a cached live exchange order
book in selected details.

Order-book analytics store spread in separate units: `spread_abs_rub` and `spread_bps`.
Quality payloads store display and calibration scores plus transparent components. Trade
tape absence is logged as `no_market_trades_samples`; it is not hidden. Stale
readonly `GetLastTrades` diagnostics are not analytics samples and must be
reported with `trade_tape_status=stale`,
`trade_tape_reason=trade_exchange_ts_too_old`, and
`market_trades_source=tbank_get_last_trades`. Rows newer than
`DASHBOARD_TRADES_DELAYED_DISPLAY_SECONDS` may be displayed as a delayed tape,
but they must not be stored or labeled as live trade rows.

Data-shadow runtime status exposes supervisor observability separately from
analytics facts: `supervisor_enabled`, `supervisor_state`,
`stream_restart_count`, `last_restart_at`, `last_restart_reason`,
`stream_stale_count`, `last_stream_error`, and `per_stream_status`. These fields
are status/read-model diagnostics and do not create trading entities.
## Forward Return Horizon Alignment

Forward-return and daily trend research must expose horizon alignment explicitly.
For every retrospective candidate window the report payload includes:

- `requested_horizon_minutes`;
- `entry_ts`;
- `target_exit_ts`;
- `actual_exit_ts`;
- `actual_horizon_minutes`;
- `exit_alignment_seconds`;
- `horizon_valid`.

Reports must not use the next closed bucket beyond the requested horizon. If the
target exit sample is absent outside the documented tolerance, the candidate is
excluded with `horizon_mismatch`; it must not appear in top/worst windows. This
rule applies to 1m/5m/10m/15m pseudo-bars and to mirrored long/short cases.

Execution/risk payloads must preserve money semantics:

- `price_per_share`;
- `lot_qty`;
- `lot_size`;
- `estimated_notional_rub`;
- `original_intended_price`;
- `normalized_price`;
- `min_price_increment`;
- `reject_reason_code=price_tick_invalid` when the tick is unknown or invalid.

Unknown lot size or tick size is a blocker, not a silent default. Historical
diagnostics may label unknown metadata, but real/shadow risk and execution paths
must fail closed.

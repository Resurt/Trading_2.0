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

## JSON structured logging

Технические логи пишутся в JSON через стандартный Python logging.

Контекст должен автоматически прокидываться через `contextvars`, `LoggerAdapter` или logging filters.

Минимальные поля, где применимо:

- `ts_utc`
- `exchange_tz_ts`
- `level`
- `service`
- `run_id`
- `session_type`
- `session_phase`
- `micro_session_id`
- `instrument_id`
- `timeframe`
- `strategy_id`
- `candidate_id`
- `blocker_id`
- `signal_id`
- `order_intent_id`
- `request_order_id`
- `exchange_order_id`
- `position_side`
- `qty_lots`
- `price`
- `commission`
- `latency_ms`
- `tracking_id`
- `rate_limit_remaining`
- `event_type`
- `error_code`
- `error_message`

Технический лог может быть неполным для событий, где части контекста еще нет. Но если контекст известен, он должен быть прокинут.

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
- `strategy_state_event`
- `hourly_report`
- `daily_report`
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
- `order_type_forbidden`
- `max_drawdown_reached`
- `open_order_conflict`
- `position_limit_reached`
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
- fee/slippage assumptions;
- MFE/MAE по окнам 5/10/15 минут;
- итог `would_profit_*`.

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

## Metrics

Histograms:

- `broker_post_order_latency_seconds`
- `order_state_convergence_seconds`
- `candle_close_delivery_lag_seconds`
- `session_rollover_duration_seconds`

Counters:

- `reconnect_total`
- `rejected_orders_total`
- `risk_events_total`
- `blocked_candidates_total`
- `cancelled_orders_total`

Gauges:

- `open_orders`
- `active_positions`
- `market_stream_alive`
- `last_closed_candle_age_seconds`
- `current_micro_session_seconds_remaining`

Prometheus labels не должны содержать raw ids или exception text.

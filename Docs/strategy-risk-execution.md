# Strategy, Risk, Execution

Этот документ фиксирует шаг 07: расширяемый каркас стратегии, risk engine, execution engine и reconciliation без попытки реализовать прибыльную модель.

## Границы слоя

`trade-core` содержит четыре pluggable интерфейса:

- `StrategyEngine` - получает session context, closed bars и market state, возвращает кандидатов.
- `RiskEngine` - проверяет кандидата causal gate chain и возвращает machine-readable blockers.
- `ExecutionEngine` - создает `order_intent`, отправляет/отменяет/заменяет ордера через `BrokerGateway`.
- `ReconciliationService` - сверяет локальное состояние с broker state.

Стратегия не зависит от FastAPI, Vue или SDK-специфичных типов T-Bank. Внешняя брокерская граница остается `BrokerGateway`.

## Strategy state machine

Канонические состояния:

```text
idle
  -> warming_up
  -> wait
  -> candidate
  -> blocked
  -> placing_order
  -> working_order
  -> partially_filled
  -> in_position
  -> exiting
  -> degraded
  -> stopped
```

Основной happy path:

```text
idle -> warming_up -> wait -> candidate -> placing_order -> working_order -> in_position -> exiting -> wait
```

Blocked path:

```text
candidate -> blocked -> wait
```

Degraded/stop path:

```text
any active state -> degraded -> stopped
```

Все переходы пишутся в `strategy_state_event` с `event_type`, `reason_code`, `session_type`, `session_phase`, `micro_session_id`, `trading_date` и `calendar_date`.

## Config-driven strategy stub

Первая стратегия - `ConfigDrivenStrategyEngine`.

Свойства:

- работает только по closed bars;
- отдельно конфигурируется для `5m`, `10m`, `15m`;
- отдельно конфигурируется по `session_type`;
- weekend template по умолчанию выключен;
- entry/exit condition объяснима: directional move closed bar выше `min_move_bps`;
- `expected_edge_bps` не является прогнозом прибыли, а только входным числом для risk gates и последующей калибровки.

### Production-safe long/short config

`strategy_config` хранится версионированно в PostgreSQL: typed runtime-модель
`ConfigDrivenStrategyConfig` проецируется в `strategy_config.config_payload` и
`strategy_config.risk_limits`. Таблица остается JSONB-based для версионирования,
но обязательные поля должны быть machine-readable:

- `allow_long`;
- `allow_short`;
- `max_long_lots`;
- `max_short_lots`;
- `max_gross_exposure_rub`;
- `max_net_exposure_rub`;
- `min_expected_edge_bps`;
- `assumed_commission_bps_per_side`;
- `assumed_slippage_bps`;
- `min_edge_after_total_costs_bps`;
- `session_template`;
- `instrument_id/timeframe overrides`.

Консервативный default:

- `allow_long=true`;
- `allow_short=false`;
- weekend template выключен;
- комиссия не ниже `5 bps` на сторону;
- round trip commission не ниже `10 bps`;
- реальные заявки не включаются default launch mode.

Цель этого слоя - long/short framework, journaling и калибровка. Он не является
заявлением о прибыльности стратегии.

### Runtime config loading

`TradeCoreRuntime` больше не живёт на одном `conservative_default`. На startup и затем при reload/version check он использует `StrategyConfigLoader`:

- выбирает active `strategy_config` по `strategy_id + session_template`;
- маппит `config_payload` в `ConfigDrivenStrategyConfig`;
- маппит `risk_limits` в `RiskLimits`;
- пишет `audit_event`: `strategy_config_loaded`, `strategy_config_reloaded`, `strategy_config_reload_failed`;
- применяет новые `allow_long`, `allow_short`, cost model, exposure и loss limits без перезапуска `trade-core`.

Frontend/API update strategy config становится активным для runtime после reload/version check. Reload не должен менять исторические events: новая версия фиксируется через `strategy_version`.

## Cost model v1

Risk gate `total_expected_costs` считает:

```text
total_expected_costs_bps =
  commission_entry_bps
  + commission_exit_bps
  + current_spread_bps
  + assumed_slippage_bps
```

Где:

- `commission_entry_bps` и `commission_exit_bps` не ниже `5 bps`;
- `round_trip_commission_bps` не ниже `10 bps`;
- `spread_bps` берется из текущего `MarketState`;
- `assumed_slippage_bps` задается конфигом.

Если `expected_edge_bps - total_expected_costs_bps <
min_edge_after_total_costs_bps`, кандидат блокируется с
`blocker_code=total_costs_exceed_edge`.

## Risk blockers

Канонические blocker codes текущего шага:

| Code | UI label | Report dimension |
| --- | --- | --- |
| `spread_too_wide` | Широкий спред | Spread quality |
| `market_quality_low` | Низкое качество рынка | Market quality |
| `stale_market_data` | Устаревшие market data | Feed health |
| `no_edge_after_costs` | Нет edge после costs | Strategy calibration |
| `risk_budget_exceeded` | Превышен risk budget | Risk limits |
| `session_forbidden` | Сессия запрещает действие | Session policy |
| `order_type_forbidden` | Тип ордера запрещен фазой | Session policy |
| `max_drawdown_reached` | Достигнут max drawdown | Risk limits |
| `open_order_conflict` | Есть конфликт открытой заявки | Execution safety |
| `position_limit_reached` | Достигнут лимит позиции | Position limits |
| `short_not_allowed_by_config` | Short выключен конфигом | Short policy |
| `short_not_allowed_by_broker` | Short запрещен аккаунтом/инструментом | Short policy |
| `insufficient_margin` | Недостаточно маржи/обеспечения | Margin policy |
| `max_short_exposure_reached` | Достигнут short exposure limit | Exposure limits |
| `max_long_exposure_reached` | Достигнут long exposure limit | Exposure limits |
| `total_costs_exceed_edge` | Полные costs выше edge | Cost model |
| `position_side_conflict` | Конфликт стороны позиции | Position policy |
| `position_state_stale` | Локальный снимок позиции устарел | Position reconciliation |
| `position_reconciliation_mismatch` | Broker position не совпала с локальным snapshot | Position reconciliation |

`DefaultRiskEngine` сохраняет не только финальный blocker, но и всю causal chain как `blocker_event`. Failed gates дополнительно пишутся как `risk_event`.

Long-specific gates:

- `long_allowed_by_config`;
- `max_long_position`;
- `max_gross_exposure`;
- `max_net_exposure`;
- `no_new_entries_during_freeze`.

Short-specific gates:

- `short_allowed_by_config`;
- `short_allowed_by_account`;
- `short_allowed_by_instrument`;
- `margin_or_collateral_available`;
- `no_short_during_forbidden_session_phase`;
- `forced_cover_policy`;
- `max_short_position`;
- `position_side_conflict`.

Position reconciliation gates:

- `position_state_freshness`;
- `position_reconciliation`.

Special-day gates:

- `dividend_gap_risk` - entry blocked on dividend gap day when
  `block_entries_on_dividend_gap_day=true`;
- `corporate_action_window` - entry blocked on corporate-action day when
  `block_entries_on_corporate_action_day=true`;
- `special_day_shadow_only` - short / entry policy blocks live action when
  `special_day_trade_policy=shadow_only`.

`RiskLimits` also contains:

- `block_entries_on_dividend_gap_day`;
- `block_entries_on_corporate_action_day`;
- `block_short_on_special_day`;
- `special_day_trade_policy`.

Historical replay reads these flags from `market_special_day`. Live/shadow runtime looks
up special-day context by `trading_date + instrument_id`; if the calendar is unavailable,
runtime writes warning/audit `corporate_action_calendar_unavailable`.

## PositionService

`PositionService` refreshes account positions through `BrokerGateway.get_positions`
and `BrokerGateway.get_portfolio`, writes normalized `position_snapshot` rows and
builds a `PortfolioSnapshot` for `DefaultRiskEngine`.

Rules:

- T-Bank `instrument_uid` / ticker aliases are normalized back to project `instrument_id`
  through the configured `InstrumentRef`;
- micro-session open/close snapshots call `refresh_positions(account_id)`;
- before each entry candidate becomes an `order_intent`, runtime calls
  `validate_before_entry(...)`;
- stale local state blocks with `position_state_stale`;
- local/broker mismatch blocks with `position_reconciliation_mismatch`;
- long and short lots are tracked separately as `long_position_lots` and
  `short_position_lots`, while gross/net exposure are kept in RUB when broker data
  contains enough price/PnL fields;
- stream gap recovery refreshes positions after candle backfill and order reconciliation.

## Execution lifecycle

`DefaultExecutionEngine` делает:

- `order_intent` creation с UUID `request_order_id`;
- idempotency key на основе strategy/version/micro-session/candidate/action;
- `PostOrder` через `BrokerGateway`;
- `CancelOrder` через `BrokerGateway`;
- `replace_order` как cancel старого intent + create/post replacement intent;
- upsert `broker_order`;
- обновление `order_intent.status`.

Launch-mode safety:

- `historical_replay` и `shadow` пишут только pseudo-orders;
- `sandbox` по умолчанию тоже пишет pseudo-orders и делает real sandbox
  `PostOrder` только при явном `TRADING_SANDBOX_ORDERS_CONFIRM=I_UNDERSTAND_SANDBOX_ORDERS`
  или эквивалентном `LaunchModePolicy(..., sandbox_orders_confirmed=True)`;
- `production` требует `TRADING_PRODUCTION_CONFIRM=I_UNDERSTAND_LIVE_ORDERS`;
- `production` не является default mode.

Любая отмена обязана иметь:

- `cancel_reason_code`;
- структурированный `cancel_payload`;
- связь с `order_intent_id` и `request_order_id`.

### Emergency stop

`emergency_stop` является отдельной operator policy, а не сбросом счётчика open orders. При получении команды runtime:

1. freeze new entries;
2. находит все `order_intent` в `submitted`, `working`, `partially_filled`, `cancel_requested`;
3. вызывает `DefaultExecutionEngine.cancel_order`;
4. пишет `cancel_reason_code=manual_operator_emergency_stop`;
5. запускает reconciliation до terminal state или переводит runtime в `degraded`;
6. обновляет метрики `emergency_stop_total`, `emergency_cancel_failed_total`, `working_orders_after_stop`.

## Reconciliation

`DefaultReconciliationService` сверяет:

- один ордер через `reconcile_order_state`;
- открытые ордера через `reconcile_open_orders`.

Результат сохраняется в `broker_order`, terminal statuses синхронизируются обратно в `order_intent`.

## Точки расширения

- подключить реальные strategy configs из `strategy_config`;
- заменить stub entry/exit rules на калиброванные правила после накопления отчетов;
- расширить execution policy для stop/stop-limit и partial fill handling;
- добавить replay harness для детерминированной проверки стратегий на исторических данных;
- добавить counterfactual job для blocked/cancelled candidates.

## Domain journaling integration

Наблюдаемость торговой возможности строится вокруг `candidate_id`.

Путь записи:

1. `SqlAlchemyStrategyEventStore.record_candidate()` пишет `signal_candidate` и `market_context_snapshot`.
2. `record_blockers()` пишет `candidate_stage_result` для каждого risk gate.
3. Для непройденных gate создается `blocker_event` с `blocker_code`, `blocker_family`, `measured_value`, `threshold_value` и `is_final_blocker`.
4. Для финального blocked candidate пишется `market_context_snapshot` с
   `snapshot_kind=counterfactual_seed_snapshot` и горизонтами `5/10/15`.
5. `DefaultExecutionEngine.create_order_intent()` пишет идемпотентный `order_intent`.
6. `post_order()` и `cancel_order()` upsert-ят `broker_order`, сохраняют `latency_ms`, `tracking_id`, rate-limit headers и пишут `order_state_event`.
7. `DefaultReconciliationService` пишет все наблюдаемые broker state transitions в `order_state_event`.
8. Fills пишутся в `fill_event` только из source-of-truth по собственным ордерам: broker order state/reconciliation payload. Anonymous `market_trade` tape остается рыночным контекстом, а не источником собственных исполнений.

Idempotency:

- `signal_candidate` дедуплицируется по `signal_fingerprint`;
- `candidate_stage_result` по `candidate_id + stage_seq`;
- `blocker_event` по `candidate_id + gate_rank + reason_code`;
- `order_intent` по `request_order_id` и `idempotency_key`;
- `broker_order` по `request_order_id`;
- `order_state_event` по `order_intent_id + state_seq + event_type`;
- `fill_event` по `exchange_order_id + broker_fill_id + trading_date`.

## Historical DB replay for calibration

Historical replay не добавляет новую торговую стратегию и не меняет
`strategy_config`. Он использует существующий `ConfigDrivenStrategyEngine`,
`DefaultRiskEngine`, `DefaultExecutionEngine` и `SqlAlchemyStrategyEventStore`
для воспроизводимого прогона закрытых bars из `market_candle`.

Правила:

- входом являются только закрытые `5m/10m/15m` bars;
- `historical_replay` и calibration всегда работают с pseudo-orders;
- risk gates пишут те же `candidate_stage_result`, `blocker_event` и
  `risk_event`, что live runtime;
- approved candidates пишут `order_intent`, pseudo `broker_order` и
  `order_state_event`;
- deterministic fingerprint защищает от дублей при повторном запуске;
- calibration report может рекомендовать изменения порогов
  `max_spread_bps`, `min_market_quality_score`,
  `min_edge_after_total_costs_bps`, `max_data_age_ms` и `allow_short`, но не
  применяет их автоматически.

## Dividend Calendar Risk Gates

Dividend calendar is a risk input, not a strategy alpha signal. The primary source is
T-Bank `GetDividends`; manual CSV/JSON is fallback/override only.

`RiskAssessmentInput` carries:

- `dividend_calendar_available`;
- `future_dividend_risk_window`;
- `dividend_gap_day`;
- `days_to_ex_date`;
- `days_to_record_date`;
- `corporate_action_source`.

New machine-readable blockers:

- `dividend_calendar_unavailable`;
- `future_dividend_risk_window`;
- `short_blocked_dividend_window`;
- existing `dividend_gap_risk` and `corporate_action_window`.

Default policy is fail-closed for shadow/production when the dividend calendar is unavailable:
`TRADING_DIVIDEND_SYNC_FAIL_OPEN=false`. Future dividend windows and ex-date gap days are
`shadow_only`/entry-blocked by default. Shorts are blocked separately in dividend windows because
borrow, margin and dividend liability assumptions must be confirmed before live use.

## Instrument Identity Preconditions

Risk and execution receive internal canonical `instrument_id` values such as
`MOEX:SBER`, but real broker calls require resolved T-Bank `instrument_uid` or
`figi`. `trade-core` resolves instruments before dividend sync, market data
streams, historical real backfill and order placement.

If an enabled instrument remains `source=seed` / `resolution_status=unresolved`
in sandbox/shadow/production, startup/readiness must fail before strategy/risk can
approve an entry. This avoids treating `MOEX:*` as a broker id and prevents
misleading `NOT_FOUND` broker errors during calibration.
## Calibration Risk Hardening Invariants

These invariants are mandatory before any future strategy shadow or production
path:

- Market data price and `SignalCandidateDecision.intended_price` are price per
  one share/security, not price per lot.
- `lot_qty` is quantity in lots.
- `lot_size` is quantity of shares/securities in one lot.
- `estimated_notional_rub = price_per_share * lot_qty * lot_size`.
- Broker/SDK resolution and `instrument_registry` are the source of truth for
  `lot_size` and `min_price_increment`. Env/default instruments are legacy/dev
  identifiers only and must not silently supply lot/tick for real/shadow risk or
  execution.
- If `lot_size` is unknown for an entry, risk blocks with
  `instrument_lot_size_unknown`.
- If `min_price_increment` is unknown for a limit entry, risk blocks with
  `price_tick_invalid`.
- Position/exposure fallback may use `market_price * qty_lots * lot_size` only
  when broker exposure is absent and lot size is known. Broker-provided exposure
  remains authoritative when present.

Execution normalizes limit prices to the instrument tick before broker boundary:

- BUY limit: floor to tick, so the robot does not overpay.
- SELL limit: ceil to tick, so the robot does not undersell.
- MARKET orders are unaffected by price normalization.
- The original intended price and normalized price are written to the intent /
  broker request payload.
- If tick size is missing, execution marks the intent rejected with
  `reject_reason_code=price_tick_invalid` and does not call `PostOrder`.

Position limit semantics distinguish entry and exit:

- ENTRY increases projected exposure/position and is checked against configured
  long/short/max-position limits.
- EXIT reduces an existing position and must not be blocked by
  `position_limit_reached` when projected position decreases.
- EXIT at zero position blocks with `exit_without_position`.
- EXIT quantity greater than current position blocks with
  `exit_quantity_exceeds_position` unless a prior candidate-creation layer has
  explicitly capped quantity.

Core market freshness is dual:

- `received_age_ms` measures how old the local/API received sample is.
- `exchange_age_ms` measures how old the exchange-side data is.
- Entry risk treats stale received time, stale exchange time, and missing
  exchange timestamp as `stale_market_data`.
- `freshness_reason` must identify `fresh`, `received_ts_too_old`,
  `exchange_ts_too_old`, or `missing_exchange_ts`.

Short permission is fail-closed:

- Missing account short permission is `unknown`, not `true`.
- Missing instrument short availability is `unknown`, not `true`.
- Short entry blocks with `short_permission_unknown` when either required
  permission is unknown.
- `short_allowed_by_account=false` or `short_allowed_by_instrument=false` blocks
  with `short_not_allowed_by_broker`.
- Short exits / forced covers that reduce an existing short are not blocked by
  unknown short-entry permission.

Session/calendar authority:

- Local weekday calendar defaults are advisory and cannot independently allow
  real/shadow entry.
- Broker schedule/status and official exchange overrides are authoritative when
  available.
- Data-only logging may use market-data probe fallback for collection visibility,
  but that fallback keeps trading permission false.
- Full MOEX holiday-calendar synchronization remains a documented follow-up.

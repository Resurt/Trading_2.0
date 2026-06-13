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

`DefaultRiskEngine` сохраняет не только финальный blocker, но и всю causal chain как `blocker_event`. Failed gates дополнительно пишутся как `risk_event`.

## Execution lifecycle

`DefaultExecutionEngine` делает:

- `order_intent` creation с UUID `request_order_id`;
- idempotency key на основе strategy/version/micro-session/candidate/action;
- `PostOrder` через `BrokerGateway`;
- `CancelOrder` через `BrokerGateway`;
- `replace_order` как cancel старого intent + create/post replacement intent;
- upsert `broker_order`;
- обновление `order_intent.status`.

Любая отмена обязана иметь:

- `cancel_reason_code`;
- структурированный `cancel_payload`;
- связь с `order_intent_id` и `request_order_id`.

## Reconciliation

`DefaultReconciliationService` сверяет:

- один ордер через `reconcile_order_state`;
- открытые ордера через `reconcile_open_orders`.

Результат сохраняется в `broker_order`, terminal statuses синхронизируются обратно в `order_intent`.

## Точки расширения

- подключить реальные strategy configs из `strategy_config`;
- добавить portfolio/position service вместо тестового `PortfolioSnapshot`;
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
4. `DefaultExecutionEngine.create_order_intent()` пишет идемпотентный `order_intent`.
5. `post_order()` и `cancel_order()` upsert-ят `broker_order`, сохраняют `latency_ms`, `tracking_id`, rate-limit headers и пишут `order_state_event`.
6. `DefaultReconciliationService` пишет все наблюдаемые broker state transitions в `order_state_event`.
7. Fills пишутся в `fill_event` только из source-of-truth по собственным ордерам: broker order state/reconciliation payload. Anonymous `market_trade` tape остается рыночным контекстом, а не источником собственных исполнений.

Idempotency:

- `signal_candidate` дедуплицируется по `signal_fingerprint`;
- `candidate_stage_result` по `candidate_id + stage_seq`;
- `blocker_event` по `candidate_id + gate_rank + reason_code`;
- `order_intent` по `request_order_id` и `idempotency_key`;
- `broker_order` по `request_order_id`;
- `order_state_event` по `order_intent_id + state_seq + event_type`;
- `fill_event` по `exchange_order_id + broker_fill_id + trading_date`.

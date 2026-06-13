# Logging & Analytics Event Taxonomy

Документ задает каноническую таксономию событий для машинной аналитики,
counterfactual-разбора и операторского UI. Он выравнивает текущие таблицы и
модули репозитория с целевой моделью, не меняя торговую логику.

## Назначение

Таксономия должна давать однозначные ответы:

- какая exchange session и phase были активны;
- какой candidate был создан и почему;
- какие проверки прошел или не прошел candidate;
- какой blocker стал причиной отказа;
- какой order intent был создан;
- какой broker state пришел по заявке;
- какие fills были получены;
- какой контрфакт получился для blocked/cancelled case.

## Canonical sessions and phases

`session_type`:

- `weekend`
- `weekday_morning`
- `weekday_main`
- `weekday_evening`

Целевые `session_phase`:

- `opening_auction`
- `continuous`
- `closing_auction`
- `break`
- `discrete_auction`
- `session_closed`

Текущая совместимость с кодом:

| Целевое значение | Текущее значение в коде | Комментарий |
| --- | --- | --- |
| `opening_auction` | `opening_auction` | без преобразования |
| `continuous` | `continuous_trading` | нужен alias/read-model mapping |
| `closing_auction` | `closing_auction` | без преобразования |
| `break` | `break` | без преобразования |
| `discrete_auction` | `dealer_mode` | требует проверки по broker status |
| `session_closed` | `closed` | нужен alias/read-model mapping |

## Обязательные сущности

| Entity | Назначение | Текущее состояние в репозитории | Primary correlation |
| --- | --- | --- | --- |
| `session_run` | Факт жизненного цикла exchange session или micro-session: даты, тип, фаза, границы, статус. | Реализована таблица `session_run`. | `session_run_id`, `micro_session_id` |
| `micro_session` | Логическая hourly единица внутри exchange session. | Представлена через `session_run.micro_session_id`; отдельной таблицы пока нет. | `micro_session_id` |
| `signal_candidate` | Кандидат на вход/выход, сформированный strategy layer на закрытых барах и market context. | Реализована таблица `signal_candidate`. | `candidate_id`, `micro_session_id` |
| `candidate_stage_result` | Результат каждой стадии decision pipeline: signal, market quality, risk, session policy, execution eligibility. | Требуется как отдельная таблица/view; часть информации сейчас в `blocker_event` и `risk_event`. | `candidate_id`, `stage_code` |
| `blocker_event` | Machine-readable отказ/блокировка с `reason_code`, passed/failed, final blocker flag и payload. | Реализована таблица `blocker_event`. | `blocker_event_id`, `candidate_id` |
| `order_intent` | Намерение выставить/отменить/заменить заявку до broker call; содержит idempotency и cancel reason. | Реализована таблица `order_intent`. | `order_intent_id`, `request_order_id`, `candidate_id` |
| `broker_order` | Состояние заявки у брокера, exchange order id, lifecycle status. | Реализована таблица `broker_order`. | `broker_order_id`, `exchange_order_id`, `request_order_id` |
| `order_state_event` | Append-only событие изменения broker/order state. | Требуется как отдельная таблица/view; сейчас состояние хранится в `broker_order`, часть переходов видна через audit/strategy events. | `order_intent_id`, `exchange_order_id` |
| `fill_event` | Исполнение/частичное исполнение с ценой, количеством, комиссией, временем. | Реализована таблица `fill_event`, event-heavy storage. | `fill_event_id`, `exchange_order_id` |
| `market_context_snapshot` | Снимок market context рядом с candidate/order decision: spread, mid, book summary, freshness, quality. | Требуется каноническая таблица/view; текущие источники: `market_status_snapshot`, `order_book_summary`, candles и поля candidate payload. | `candidate_id`, `instrument_id`, `timeframe` |
| `counterfactual_result` | Результат 5/10/15 minute разбора blocked/cancelled cases: MFE/MAE, TP/SL, theoretical PnL. | Реализована таблица `counterfactual_result`. | `counterfactual_result_id`, `candidate_id`, `order_intent_id` |
| `hourly_report` | Агрегат по micro-session. | Реализована таблица `hourly_report`. | `micro_session_id`, `strategy_id` |
| `daily_report` | Агрегат по trading_date/session/instrument/timeframe/blockers/execution/counterfactual. | Реализована таблица `daily_report`. | `trading_date`, `strategy_id` |

## Correlation IDs

Обязательные IDs должны проходить через logs, domain events, reports и UI там,
где применимо.

| ID | Где рождается | Где используется |
| --- | --- | --- |
| `candidate_id` | При создании `signal_candidate`. | Stage results, blockers, order intent, counterfactual, reports, UI explanations. |
| `micro_session_id` | При открытии hourly micro-session. | Все session-aware events, hourly report, filters, calibration slices. |
| `order_intent_id` | При создании order intent. | Broker calls, cancel/replace lifecycle, order reports, counterfactual for cancelled cases. |
| `request_order_id` | Перед `PostOrder`, UUID для idempotency. | Broker idempotency mapping, retries, logs, reconciliation. |
| `exchange_order_id` | После подтверждения broker order. | Broker state updates, fills, reconciliation, UI open orders. |
| `tracking_id` | Из broker metadata `x-tracking-id`. | Technical diagnostics, support trace, Loki search, audit context. |

Дополнительные полезные IDs: `run_id`, `session_run_id`, `strategy_id`,
`instrument_id`, `timeframe`, `blocker_id`.

## Runtime log schema

Runtime logs пишутся structured JSON через стандартный Python logging. Они идут
в Loki и должны быть пригодны для поиска, но не заменяют decision journal.

Канонический минимум:

```json
{
  "ts": "2026-06-13T07:59:59.123456Z",
  "level": "INFO",
  "service": "trade-core",
  "logger": "trade_core.strategy.execution_engine",
  "message": "broker order posted",
  "event_type": "broker_order_posted",
  "runtime_mode": "shadow",
  "session_type": "weekday_morning",
  "session_phase": "continuous",
  "micro_session_id": "2026-06-13T07",
  "candidate_id": "uuid-or-null",
  "order_intent_id": "uuid-or-null",
  "request_order_id": "uuid-or-null",
  "exchange_order_id": "broker-id-or-null",
  "tracking_id": "x-tracking-id-or-null",
  "instrument_id": "SBER",
  "timeframe": "5m",
  "reason_code": null,
  "payload": {}
}
```

Правила:

- `message` может быть человекочитаемым, но не должен быть единственным
  носителем смысла;
- reason/cancel/blocker codes должны быть machine-readable fields;
- секреты и tokens в logs не попадают;
- high-cardinality IDs остаются JSON fields, не Loki labels.

## Decision journal event types

Канонические event types:

| Event type | Entity | Когда пишется |
| --- | --- | --- |
| `session_snapshot_written` | `session_run` | При snapshot/rollover/status transition. |
| `micro_session_opened` | `micro_session`/`session_run` | При открытии логической hourly micro-session. |
| `micro_session_closed` | `micro_session`/`session_run` | На часовой или exchange-session границе. |
| `report_requested` | `hourly_report`/task | После закрытия micro-session или ручного запуска отчета. |
| `signal_candidate_created` | `signal_candidate` | После закрытого бара и прохождения strategy preconditions. |
| `candidate_stage_result_recorded` | `candidate_stage_result` | После каждой стадии pipeline, включая passed stages. |
| `blocker_triggered` | `blocker_event` | Когда stage/risk/session policy блокирует candidate/order. |
| `risk_event_recorded` | `risk_event` | При risk decision, превышениях лимитов, degraded signals. |
| `order_intent_created` | `order_intent` | Перед broker call или pseudo-order в replay/shadow. |
| `broker_order_posted` | `broker_order` | После успешного `PostOrder` или pseudo-post. |
| `broker_order_updated` | `broker_order`/`order_state_event` | При изменении broker state. |
| `broker_order_cancelled` | `broker_order`/`order_state_event` | При отмене с explicit `cancel_reason_code`. |
| `fill_received` | `fill_event` | При подтвержденном исполнении. |
| `market_context_snapshot_written` | `market_context_snapshot` | Рядом с candidate/order decision и при stale/degraded market data. |
| `counterfactual_result_written` | `counterfactual_result` | После расчета outcome windows. |
| `hourly_report_built` | `hourly_report` | После завершения hourly report. |
| `daily_report_built` | `daily_report` | После завершения daily report. |

## Candidate stage taxonomy

Целевой `candidate_stage_result` должен фиксировать не только финальный отказ,
но и полный путь candidate:

| Stage | Пример result fields |
| --- | --- |
| `signal_precheck` | `passed`, `timeframe`, `closed_bar_ts`, `signal_strength` |
| `market_quality` | `passed`, `spread_bps`, `market_quality_score`, `staleness_seconds` |
| `session_policy` | `passed`, `session_type`, `session_phase`, `allowed_actions` |
| `risk_budget` | `passed`, `risk_budget_used`, `max_drawdown`, `position_limit` |
| `edge_after_costs` | `passed`, `expected_edge_bps`, `fees_bps`, `slippage_bps` |
| `execution_conflict` | `passed`, `open_order_conflict`, `position_conflict` |

Если stage не прошел, он должен ссылаться на `blocker_code`. Финальный blocker
помечается отдельно, чтобы отчеты не гадали по free-text.

## Blocker taxonomy

Базовые blocker codes из strategy/risk layer:

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

Session-specific deny codes:

- `phase_forbidden`
- `weekend_broker_mode`

Все новые blocker codes должны попадать в enum/catalog и иметь:

- machine-readable `reason_code`;
- human-readable label for UI;
- owner stage;
- payload contract;
- mapping to report category.

## Order lifecycle taxonomy

`order_intent` фиксирует намерение и idempotency до broker call:

- `order_intent_id`;
- `request_order_id`;
- `candidate_id`;
- `instrument_id`;
- `side`;
- `order_type`;
- `quantity`;
- `price`;
- `idempotency_key`;
- `cancel_reason_code`, если intent отменен/заменен;
- `payload` only for extended context.

`broker_order` фиксирует broker-facing состояние:

- `exchange_order_id`;
- `request_order_id`;
- `broker_status`;
- `lots_requested`;
- `lots_executed`;
- `posted_at`;
- `updated_at`;
- `tracking_id`;
- `payload`.

Целевой `order_state_event` должен быть append-only журналом изменений, чтобы
reconciliation и отчеты не зависели только от текущего snapshot в
`broker_order`.

## Как считаются дневной тренд, блокеры и контрфакты

### Дневной тренд

Текущий алгоритм v1 реализован в `report-worker`:

1. Берутся закрытые свечи за `trading_date`.
2. Для каждого инструмента считается доходность от первого `open` до
   последнего `close`.
3. Доходности инструментов усредняются равными весами.
4. Если средняя доходность `>= +25 bps`, режим дня `long_bias`.
5. Если средняя доходность `<= -25 bps`, режим дня `short_bias`.
6. Иначе режим дня `mixed_flat`.

Алгоритм воспроизводимый и должен сохранять в `daily_report`:

- `market_regime`;
- `average_return_bps`;
- `instrument_returns_bps`;
- version/assumption metadata.

### Блокеры

Blocker analytics строится из `blocker_event` и целевого
`candidate_stage_result`:

1. Каждый gate пишет passed/failed result.
2. Failed result получает `blocker_code`.
3. Один blocker для candidate/order помечается как final, если именно он
   остановил pipeline.
4. Ranking считает failed/final blockers по `trading_date`, `session_type`,
   `instrument_id`, `timeframe`, `strategy_id`.
5. UI показывает не только count, но и payload-поля, объясняющие причину:
   spread, freshness, market quality, budget, phase, conflict.

### Контрфакты

Counterfactual analyzer работает только по фактам, уже записанным в Postgres:

1. Источники: blocked `signal_candidate` и cancelled `order_intent`.
2. Для каждого source берется price path после события на окнах 5/10/15 минут.
3. Считаются MFE и MAE в bps.
4. Проверяется, был бы достигнут TP/SL по assumptions.
5. Считается theoretical PnL after fees/slippage assumptions.
6. Результат сохраняется в `counterfactual_result` с source metadata,
   assumptions и window outcomes.

Контрфакт не является доказательством прибыльности стратегии. Он нужен для
калибровки blockers и поиска missed opportunities.

## Open questions / TODO

- Ввести или явно отвергнуть отдельную таблицу `micro_session`.
- Спроектировать non-breaking migration для `candidate_stage_result`.
- Спроектировать append-only `order_state_event`, не ломая текущий
  `broker_order` snapshot.
- Свести `market_status_snapshot`, `order_book_summary` и candidate payload к
  каноническому `market_context_snapshot`.
- Решить, хранить ли `tracking_id` в каждой broker-related таблице или только в
  linked event/payload.
- Добавить UI labels для всех blocker/cancel/session reason codes.

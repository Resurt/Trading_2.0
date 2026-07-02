# Logging/Analytics Acceptance

Документ фиксирует definition of done для слоя logging/analytics: система должна не просто писать события, а позволять детерминированно восстановить путь торговой возможности для калибровки стратегии.

## Контур проверки

Acceptance-контур не меняет торговую логику и не требует T-Bank API:

1. `tests/fixtures/logging_analytics_acceptance.py` создаёт синтетический trading day `2026-06-12`.
2. `ReportAnalyticsService.rebuild_reports_for_date()` строит hourly/daily reports и counterfactual results.
3. `AnalyticsAcceptanceChecker` проверяет доменный журнал и витрину отчётов.
4. `scripts/run_replay_day.py` дважды прогоняет replay harness и сравнивает payload для детерминизма.

По умолчанию smoke-команды используют in-memory SQLite и не пишут секреты, raw logs или реальные брокерские данные.

## Reference Scenarios

| Scenario | Что проверяет |
| --- | --- |
| `data_only_microstructure_tape` | `market_microstructure_snapshot` and `market_trade_sample` grow only after allowed data-only Start; `exchange_ts` is never fabricated and no trading entities are created. |
| `dashboard_selected_trade_tape` | Selected dashboard tape distinguishes live broker rows from `persisted_data_only_trade_tape` fallback and never writes DB rows from display refresh. |
| `blocked_candidate` | `signal_candidate -> candidate_stage_result -> blocker_event`, финальный blocker содержит `measured_value` и `threshold_value`. |
| `broker_reject` | `order_intent -> broker_order -> order_state_event` с broker reject reason и correlation IDs. |
| `canceled_limit_order` | Отменённая лимитная заявка имеет `cancel_reason_code` и получает `counterfactual_result`. |
| `partial_fill` | Частичное исполнение пишет `fill_event`, PnL/slippage/commission и финальную отмену остатка. |
| `profitable_fill` | Полное прибыльное исполнение попадает в daily funnel и PnL summary. |
| `stream_reconnect_gap_recovery` | В audit/domain journal есть `stream_reconnect` и `gap_recovery_completed`. |
| `hourly_micro_session_rollover_without_restart` | Две соседние micro-session закрываются/открываются с одним `trade_core_instance_id` и `physical_restart=false`. |
| `weekend_session` | Weekend-сессия блокирует новые входы через machine-readable reason `weekend_broker_mode`. |

## Automated Acceptance Criteria

| Check code | Критерий |
| --- | --- |
| `candidate_terminal_outcome` | Каждый `candidate_id` за trading day восстанавливается до terminal outcome: final blocker, rejected/cancelled/filled broker state, fill или terminal intent. |
| `blocker_measured_threshold` | Каждый `blocker_event` с `passed=false` содержит `measured_value` и `threshold_value`. |
| `broker_order_correlation` | Каждый `broker_order` содержит `request_order_id`, `exchange_order_id` и `tracking_id`/`broker_tracking_id`. |
| `canceled_order_counterfactual` | Каждая отменённая заявка с `cancel_reason_code` имеет `counterfactual_result`. |
| `daily_report_calibration_shape` | Daily report содержит `market_regime`, `blocker_ranking`, `funnel` и PnL-поля. |
| `hourly_rollover_no_trade_core_restart` | Hourly rollover проходит как логическая micro-session смена без физического restart `trade-core`. |
| `stream_reconnect_gap_recovery` | Разрыв market stream сопровождается событием recovery после gap fill. |
| `weekend_session_scenario` | Weekend-сценарий присутствует и отделён от weekday trading day. |
| `no_raw_secrets_in_logs` | Audit payloads не содержат raw token/password/secret/Bearer values; redacted credential fields допустимы. |

Additional data-only acceptance checks:

- `data_only_no_trading_entities`: data-only collection may create market-data facts only;
  `signal_candidate`, `order_intent`, `broker_order`, `order_state_event`,
  `PostOrder`, and `CancelOrder` stay at zero.
- `trade_tape_source_truthful`: empty broker `GetLastTrades` is represented by
  status/reason; persisted fallback rows are labeled
  `persisted_data_only_trade_tape` and are not reported as live broker tape.
- `exchange_ts_not_fabricated`: `exchange_ts` is populated only from broker/source
  exchange timestamps; missing exchange time uses `freshness_basis=received_ts_only`
  and strict dual freshness is false.

## Local Commands

```bash
make analytics-smoke
make report-rebuild
make replay-day
make observability-up
make celery-inspect
make report-worker-smoke
```

Эквиваленты из frontend package:

```bash
cd apps/frontend
npm run analytics-smoke
npm run report-rebuild
npm run replay-day
npm run observability-up
```

Прямой запуск Python:

```bash
python scripts/run_logging_analytics_acceptance.py
python scripts/run_report_rebuild.py
python scripts/run_replay_day.py
```

Для проверки уже мигрированной БД можно отключить seed/schema:

```bash
python scripts/run_logging_analytics_acceptance.py \
  --database-url postgresql+psycopg://user:password@localhost:5432/trading \
  --no-seed-fixture \
  --no-create-schema \
  --date 2026-06-12 \
  --strategy-id baseline
```

## Definition Of Done

Logging/analytics слой считается пригодным для калибровки, если:

- `make analytics-smoke` возвращает `passed=true`;
- `make report-rebuild` строит daily report с regime, blocker ranking, funnel, missed opportunity summary и gross/net PnL;
- `make celery-inspect` получает ping от Celery worker, а `make report-worker-smoke` кладет `build_hourly_report` в очередь `reports` и получает completed result;
- `make replay-day` возвращает `deterministic=true` и подтверждает session rollover, blocker pipeline и counterfactual pipeline;
- `python -m pytest tests/test_logging_analytics_acceptance.py` проходит локально;
- ни один acceptance scenario не требует физического restart `trade-core`;
- новые blocker/cancel/reject reason codes добавляются как machine-readable поля, а не только free-text.

## Open Questions / TODO

- Подключить эти acceptance checks к CI после стабилизации времени выполнения полного pipeline.
- Добавить вариант fixtures для нескольких инструментов и разных session templates, не только `MOEX:SBER`/`weekday_main`.
- Добавить acceptance-проверку для materialized/read views фронта после появления реальных Postgres views.
- Расширить replay fixtures историческими CSV/Parquet наборами, когда появится безопасный anonymized dataset.
- Завести alert на регулярный провал `analytics-smoke` в sandbox/shadow окружении.

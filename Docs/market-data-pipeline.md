# Market Data Pipeline и Bar Engine

## Broker Display Refresh

The BFF separates fast quote refresh from selected-instrument detail refresh.
Universe refresh uses readonly last prices and must stay fast. Order book and
recent trade tape refresh are limited to the selected instrument to avoid
overloading the broker SDK/threadpool and to keep `/market/overview` responsive.

Broker OTC/indicative quote and trade rows are tagged as display-only and are
excluded from calibration by default.

Этот документ фиксирует реализацию шага 06. Он дополняет `Docs/architecture.md`,
`Docs/broker-gateway.md`, `Docs/session-manager.md` и `Docs/logging-analytics-spec.md`.

## Цель

Market data слой дает `trade-core` нормализованный поток рыночных событий,
закрытые бары 5m/10m/15m, lightweight market state и read models для live dashboard.

Стратегия на этом шаге не реализуется. Сигнальный контекст готовится только как
рыночный read model, без правил входа/выхода.

## Pipeline Flow

```text
T-Bank streams / unary backfill
  candles
  order book
  last prices
  trading status / info
  market trades
  user order state
        |
        v
MarketDataSubscriptionService
        |
        v
MarketEventBus
        |
        +--> BarEngine -> bar_closed 5m/10m/15m
        |
        +--> MarketStateCalculator
        |      spread, mid price, best bid/ask, depth, imbalance, quality, freshness
        |
        +--> MarketReadModelStore
        |      live order book, recent trades tape, current signal context
        |
        +--> SqlAlchemyMarketDataStore
               market_candle
               market_status_snapshot
               order_book_summary
```

## Historical Candle Backfill

Для накопления базы перед replay/calibration добавлен отдельный контур
`HistoricalCandleBackfillService` в `trade_core.market_data.historical_backfill`.
Он вызывает `BrokerGateway.get_candles()` для raw `1m` candles, сохраняет их в
`market_candle` и строит derived `5m/10m/15m` bars через тот же `BarEngine`.

Backfill использует `instrument_registry` или `InstrumentResolverService`, поэтому
SBER/GAZP/LKOH должны работать через canonical `instrument_id` без placeholder UID.
Повторный запуск идемпотентен благодаря upsert по
`instrument_id + timeframe + open_ts_utc + trading_date`.

CLI:

```powershell
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP --lookback-days 90 --dry-run
```

Полный runbook: `Docs/historical-candle-backfill.md`.

## Подписки

`MarketDataSubscriptionService` нормализует broker `StreamEvent` в typed events:

- `candles`;
- `order_book`;
- `last_prices`;
- `trading_status`;
- `info`;
- `market_trades`;
- `user_order_state`.

Сервис не подменяет gRPC trading methods. Он использует существующий
`BrokerGateway.stream_market_data()` и `BrokerGateway.stream_orders()`.

## Публикуемые События

Внутренний `MarketEventBus` публикует:

- `candle`;
- `order_book`;
- `last_price`;
- `trading_status`;
- `market_trade`;
- `user_order_state`;
- `bar_closed`;
- `market_state_updated`;
- `recovery_requested`;
- `recovery_completed`.

## Bar Engine

`BarEngine` строит closed bars:

- `5m`;
- `10m`;
- `15m`.

Правила:

- primary signal input - только `bar_closed`;
- формирующиеся свечи игнорируются, если явно не включен `include_forming`;
- timestamps хранятся и в UTC, и в exchange timezone;
- bucket считается по exchange timezone, затем сохраняется UTC-граница;
- если робот получает backfill после reconnect, такие свечи проходят через тот же pipeline.

## Market State Calculators

`MarketStateCalculator` считает:

- `best_bid`;
- `best_ask`;
- `mid_price`;
- `spread_abs`;
- `spread_bps`;
- `bid_depth_lots`;
- `ask_depth_lots`;
- `book_imbalance`;
- `market_quality_score`;
- `feed_freshness`.

`market_quality_score` - технический quality indicator, а не торговая стратегия.
Он нужен для dashboard, blocker analytics и будущей калибровки.

## Read Models для API/UI

Готовы in-memory read models:

- `live_order_book(instrument_id)` - стакан + derived market state;
- `recent_trades(instrument_id)` - последние anonymous market trades;
- `current_signal_context(instrument_id)` - latest closed bars, last price,
  trading status и market state.

Эти модели являются фундаментом для будущих REST/WebSocket endpoints BFF.

## Gap Recovery

`StreamGapRecoveryService` является текущей реализацией recovery-контура после reconnect/gap.
`GapRecoveryCoordinator` оставлен как backward-compatible alias для старых импортов.

Flow:

1. фиксирует `stream_gap_recovery_requested` в audit/domain контуре и публикует `recovery_requested`;
2. определяет `last_good_event_ts` по ключу `stream_name + instrument_id + timeframe`;
3. вызывает `BrokerGateway.get_candles()` только для пропущенных closed candles;
4. отбрасывает duplicate/replayed candles, у которых `close_ts_utc <= recovery_cursor`;
5. публикует восстановленные `candle` events в тот же `MarketEventBus`, поэтому `BarEngine`,
   `MarketDataPipeline`, read models и DB store получают backfill без отдельной ветки логики;
6. пишет `stream_gap_backfill_started` и `stream_gap_backfill_completed`;
7. вызывает `BrokerGateway.reconcile_open_orders()`;
8. вызывает `BrokerGateway.reconcile_order_state()` для всех known working orders;
9. вызывает `PositionService.refresh_positions()` через runtime hook;
10. пишет `order_reconciliation_completed` и `position_reconciliation_completed`;
11. публикует `recovery_completed`;
12. при ошибке пишет `stream_gap_recovery_failed`, метрику failed duration и переводит runtime в
    degraded state через failure hook.

Метрики recovery:

- `stream_reconnect_total{stream_type,result}`;
- `gap_recovery_duration_seconds{stream_type,status}`;
- `recovered_candles_total{instrument,timeframe,status}`;
- `reconciliation_mismatch_total{result}`.

Дедупликация выполняется до публикации в event bus. Дополнительно `market_candle` хранится через
repository-level upsert по `instrument_id + timeframe + open_ts_utc + trading_date`, поэтому повторный
backfill не должен плодить факты для аналитики.

## Хранение

Добавлены таблицы:

- `market_candle` - закрытые свечи и бары с UTC/exchange timestamps;
- `market_status_snapshot` - нормализованный status/info snapshot;
- `order_book_summary` - lightweight book summary.

Полный стакан на каждый тик не хранится в PostgreSQL. В БД попадают агрегаты:
best bid/ask, depth, spread, imbalance, quality score и payload для расширенного
контекста. Retention-политика для частоты snapshot будет уточняться после
подключения реальных потоков.

## Ограничения

- Реальная T-Bank stream схема подключена в `infra/tbank/sdk_clients.py`, но выше
  `infra/tbank` по-прежнему проходят только SDK-neutral payloads из `StreamEvent`.
- Для candle stream SDK wrapper выставляет `waiting_close=True`; closed candles остаются
  primary input, а формирующиеся свечи не запускают strategy candidates без явного флага.
- Deprecated user trade stream не используется как источник истины по собственным
  исполнениям; для этого остается broker order/fill reconciliation.
- Market quality score не является сигналом на сделку сам по себе.

## Historical replay from stored candles

`market_candle` теперь является входом не только для backfill, но и для
DB-backed historical replay. Контур `HistoricalDbReplayService` читает
закрытые `5m/10m/15m` bars, созданные через `BarEngine`, и передаёт их в тот
же strategy/risk/execution/persistence путь, что live runtime. Для `1m` raw
candles применяется только quality control и построение derived bars.

Historical session context строится детерминированно: synthetic
`micro_session_id` имеет формат
`historical:{trading_date}:{session_type}:{HH}`. `weekday_morning`,
`weekday_main`, `weekday_evening` и `weekend` не смешиваются; свечи вне
fallback trading windows получают `session_phase=closed`.

Replay-generated rows обязаны иметь `payload.source=historical_db_replay`.
Этот признак используется для идемпотентности и для безопасного
`--reset-derived-events`, который не удаляет live/shadow/sandbox факты.
## Historical Special-Day Awareness

Historical `market_candle` data is used by replay and calibration only after
corporate-action / special-day classification. Derived bars keep their session fields,
and replay payloads include `special_day_type`, `dividend_gap_day`,
`corporate_action_flag`, `abnormal_gap_day` and `eligible_for_live_calibration`.

Primary calibration excludes dividend/corporate-action days by default; special days are
reviewed in a separate calibration scope.

## Data-only Live Microstructure

When `TRADING_DATA_ONLY_SHADOW=true`, `trade-core` still runs session management, market streams,
`MarketEventBus`, `BarEngine` and market persistence, but closed bars do not enter strategy
evaluation. `LiveMarketDataCollector` subscribes to market events and writes
`market_microstructure_snapshot` with top-of-book, spread, depth, imbalance, freshness and market
quality.

This mode is for data collection only:

- no `signal_candidate`;
- no `order_intent`;
- no `broker_order` or pseudo-order;
- no `PostOrder`;
- no `CancelOrder`.

Use `scripts/run_data_shadow_summary_report.py` for spread/depth/quality summaries.
## Venue And Quality Semantics

Market data rows carry `venue_type`, `official_exchange_open`, and
`include_in_calibration`. Official MOEX exchange samples may be used for calibration.
Broker OTC/indicative and stale local fallback rows are display-only unless a future
operator workflow explicitly opts into separate OTC analysis.

Spread units are separate: `spread_abs`/`spread_abs_rub` are RUB and `spread_bps` is
`spread_abs / mid_price * 10000`. Market quality is component-based and stores
`spread_score`, `depth_score`, `touch_depth_score`, `depth_concentration_score`,
`imbalance_score`, `freshness_score`, `venue_score`, `trade_tape_score`,
`final_display_score`, and `final_calibration_score`.

Display quality describes the current visible book. Calibration quality is zero/not
applicable when the official exchange is closed or the venue is not `official_exchange`.
The initial model is heuristic and must be calibrated after 10-20 official exchange
trading days of microstructure collection.

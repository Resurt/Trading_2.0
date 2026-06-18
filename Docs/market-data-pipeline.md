# Market Data Pipeline и Bar Engine

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

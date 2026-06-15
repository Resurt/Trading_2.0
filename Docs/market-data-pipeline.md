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

`GapRecoveryCoordinator` после reconnect:

1. публикует `recovery_requested`;
2. вызывает `GetCandles` для backfill по инструментам и timeframe;
3. публикует восстановленные `candle` events;
4. вызывает `reconcile_open_orders`;
5. вызывает явный `refresh_positions_hook`, если он передан;
6. публикует `recovery_completed`.

Отдельный hook для позиций выбран потому, что в текущем `BrokerGateway` еще нет
SDK-neutral метода позиций. Добавлять его нужно отдельным архитектурным шагом,
вместе с reconciliation/portfolio model.

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

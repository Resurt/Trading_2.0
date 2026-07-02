# Market Data Pipeline Рё Bar Engine

## Broker Display Refresh

The BFF separates fast quote refresh from selected-instrument detail refresh.
Universe refresh is read-model first and must stay fast. Full order-book and
recent trade tape refresh are limited to the selected instrument to avoid
overloading the broker SDK/threadpool and to keep `/market/overview` responsive.
Selected details are split: `include_order_book=true&include_trades=false`
loads the selected ladder from persisted read-model rows before considering a
readonly broker fallback, while `include_order_book=false&include_trades=true`
may update the tape without waiting for the order-book refresh.

Broker OTC/indicative quote and trade rows are tagged as display-only and are
excluded from calibration by default.
If live `GetLastTrades` is transiently empty or stale, the dashboard may display
recent persisted data-only rows from `market_trade_sample` with
`trade_tape_source=persisted_data_only_trade_tape`. That fallback is readonly UI
data and never writes DB rows.

Р­С‚РѕС‚ РґРѕРєСѓРјРµРЅС‚ С„РёРєСЃРёСЂСѓРµС‚ СЂРµР°Р»РёР·Р°С†РёСЋ С€Р°РіР° 06. РћРЅ РґРѕРїРѕР»РЅСЏРµС‚ `Docs/architecture.md`,
`Docs/broker-gateway.md`, `Docs/session-manager.md` Рё `Docs/logging-analytics-spec.md`.

## Р¦РµР»СЊ

Market data СЃР»РѕР№ РґР°РµС‚ `trade-core` РЅРѕСЂРјР°Р»РёР·РѕРІР°РЅРЅС‹Р№ РїРѕС‚РѕРє СЂС‹РЅРѕС‡РЅС‹С… СЃРѕР±С‹С‚РёР№,
Р·Р°РєСЂС‹С‚С‹Рµ Р±Р°СЂС‹ 5m/10m/15m, lightweight market state Рё read models РґР»СЏ live dashboard.

РЎС‚СЂР°С‚РµРіРёСЏ РЅР° СЌС‚РѕРј С€Р°РіРµ РЅРµ СЂРµР°Р»РёР·СѓРµС‚СЃСЏ. РЎРёРіРЅР°Р»СЊРЅС‹Р№ РєРѕРЅС‚РµРєСЃС‚ РіРѕС‚РѕРІРёС‚СЃСЏ С‚РѕР»СЊРєРѕ РєР°Рє
СЂС‹РЅРѕС‡РЅС‹Р№ read model, Р±РµР· РїСЂР°РІРёР» РІС…РѕРґР°/РІС‹С…РѕРґР°.

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
               market_trade_sample
```

## Historical Candle Backfill

Р”Р»СЏ РЅР°РєРѕРїР»РµРЅРёСЏ Р±Р°Р·С‹ РїРµСЂРµРґ replay/calibration РґРѕР±Р°РІР»РµРЅ РѕС‚РґРµР»СЊРЅС‹Р№ РєРѕРЅС‚СѓСЂ
`HistoricalCandleBackfillService` РІ `trade_core.market_data.historical_backfill`.
РћРЅ РІС‹Р·С‹РІР°РµС‚ `BrokerGateway.get_candles()` РґР»СЏ raw `1m` candles, СЃРѕС…СЂР°РЅСЏРµС‚ РёС… РІ
`market_candle` Рё СЃС‚СЂРѕРёС‚ derived `5m/10m/15m` bars С‡РµСЂРµР· С‚РѕС‚ Р¶Рµ `BarEngine`.

Backfill РёСЃРїРѕР»СЊР·СѓРµС‚ `instrument_registry` РёР»Рё `InstrumentResolverService`, РїРѕСЌС‚РѕРјСѓ
SBER/GAZP/LKOH РґРѕР»Р¶РЅС‹ СЂР°Р±РѕС‚Р°С‚СЊ С‡РµСЂРµР· canonical `instrument_id` Р±РµР· placeholder UID.
РџРѕРІС‚РѕСЂРЅС‹Р№ Р·Р°РїСѓСЃРє РёРґРµРјРїРѕС‚РµРЅС‚РµРЅ Р±Р»Р°РіРѕРґР°СЂСЏ upsert РїРѕ
`instrument_id + timeframe + open_ts_utc + trading_date`.

CLI:

```powershell
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --lookback-days 90 --dry-run
```

РџРѕР»РЅС‹Р№ runbook: `Docs/historical-candle-backfill.md`.

## РџРѕРґРїРёСЃРєРё

`MarketDataSubscriptionService` РЅРѕСЂРјР°Р»РёР·СѓРµС‚ broker `StreamEvent` РІ typed events:

- `candles`;
- `order_book`;
- `last_prices`;
- `trading_status`;
- `info`;
- `market_trades`;
- `user_order_state`.

РЎРµСЂРІРёСЃ РЅРµ РїРѕРґРјРµРЅСЏРµС‚ gRPC trading methods. РћРЅ РёСЃРїРѕР»СЊР·СѓРµС‚ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РёР№
`BrokerGateway.stream_market_data()` Рё `BrokerGateway.stream_orders()`.

## РџСѓР±Р»РёРєСѓРµРјС‹Рµ РЎРѕР±С‹С‚РёСЏ

Р’РЅСѓС‚СЂРµРЅРЅРёР№ `MarketEventBus` РїСѓР±Р»РёРєСѓРµС‚:

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

`BarEngine` СЃС‚СЂРѕРёС‚ closed bars:

- `5m`;
- `10m`;
- `15m`.

РџСЂР°РІРёР»Р°:

- primary signal input - С‚РѕР»СЊРєРѕ `bar_closed`;
- С„РѕСЂРјРёСЂСѓСЋС‰РёРµСЃСЏ СЃРІРµС‡Рё РёРіРЅРѕСЂРёСЂСѓСЋС‚СЃСЏ, РµСЃР»Рё СЏРІРЅРѕ РЅРµ РІРєР»СЋС‡РµРЅ `include_forming`;
- timestamps С…СЂР°РЅСЏС‚СЃСЏ Рё РІ UTC, Рё РІ exchange timezone;
- bucket СЃС‡РёС‚Р°РµС‚СЃСЏ РїРѕ exchange timezone, Р·Р°С‚РµРј СЃРѕС…СЂР°РЅСЏРµС‚СЃСЏ UTC-РіСЂР°РЅРёС†Р°;
- РµСЃР»Рё СЂРѕР±РѕС‚ РїРѕР»СѓС‡Р°РµС‚ backfill РїРѕСЃР»Рµ reconnect, С‚Р°РєРёРµ СЃРІРµС‡Рё РїСЂРѕС…РѕРґСЏС‚ С‡РµСЂРµР· С‚РѕС‚ Р¶Рµ pipeline.

## Market State Calculators

`MarketStateCalculator` СЃС‡РёС‚Р°РµС‚:

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

`market_quality_score` - С‚РµС…РЅРёС‡РµСЃРєРёР№ quality indicator, Р° РЅРµ С‚РѕСЂРіРѕРІР°СЏ СЃС‚СЂР°С‚РµРіРёСЏ.
РћРЅ РЅСѓР¶РµРЅ РґР»СЏ dashboard, blocker analytics Рё Р±СѓРґСѓС‰РµР№ РєР°Р»РёР±СЂРѕРІРєРё.

## Read Models РґР»СЏ API/UI

Р“РѕС‚РѕРІС‹ in-memory read models:

- `live_order_book(instrument_id)` - СЃС‚Р°РєР°РЅ + derived market state;
- `recent_trades(instrument_id)` - РїРѕСЃР»РµРґРЅРёРµ anonymous market trades;
- `current_signal_context(instrument_id)` - latest closed bars, last price,
  trading status Рё market state.

Р­С‚Рё РјРѕРґРµР»Рё СЏРІР»СЏСЋС‚СЃСЏ С„СѓРЅРґР°РјРµРЅС‚РѕРј РґР»СЏ Р±СѓРґСѓС‰РёС… REST/WebSocket endpoints BFF.

## Gap Recovery

`StreamGapRecoveryService` СЏРІР»СЏРµС‚СЃСЏ С‚РµРєСѓС‰РµР№ СЂРµР°Р»РёР·Р°С†РёРµР№ recovery-РєРѕРЅС‚СѓСЂР° РїРѕСЃР»Рµ reconnect/gap.
`GapRecoveryCoordinator` РѕСЃС‚Р°РІР»РµРЅ РєР°Рє backward-compatible alias РґР»СЏ СЃС‚Р°СЂС‹С… РёРјРїРѕСЂС‚РѕРІ.

Flow:

1. С„РёРєСЃРёСЂСѓРµС‚ `stream_gap_recovery_requested` РІ audit/domain РєРѕРЅС‚СѓСЂРµ Рё РїСѓР±Р»РёРєСѓРµС‚ `recovery_requested`;
2. РѕРїСЂРµРґРµР»СЏРµС‚ `last_good_event_ts` РїРѕ РєР»СЋС‡Сѓ `stream_name + instrument_id + timeframe`;
3. РІС‹Р·С‹РІР°РµС‚ `BrokerGateway.get_candles()` С‚РѕР»СЊРєРѕ РґР»СЏ РїСЂРѕРїСѓС‰РµРЅРЅС‹С… closed candles;
4. РѕС‚Р±СЂР°СЃС‹РІР°РµС‚ duplicate/replayed candles, Сѓ РєРѕС‚РѕСЂС‹С… `close_ts_utc <= recovery_cursor`;
5. РїСѓР±Р»РёРєСѓРµС‚ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРЅС‹Рµ `candle` events РІ С‚РѕС‚ Р¶Рµ `MarketEventBus`, РїРѕСЌС‚РѕРјСѓ `BarEngine`,
   `MarketDataPipeline`, read models Рё DB store РїРѕР»СѓС‡Р°СЋС‚ backfill Р±РµР· РѕС‚РґРµР»СЊРЅРѕР№ РІРµС‚РєРё Р»РѕРіРёРєРё;
6. РїРёС€РµС‚ `stream_gap_backfill_started` Рё `stream_gap_backfill_completed`;
7. РІС‹Р·С‹РІР°РµС‚ `BrokerGateway.reconcile_open_orders()`;
8. РІС‹Р·С‹РІР°РµС‚ `BrokerGateway.reconcile_order_state()` РґР»СЏ РІСЃРµС… known working orders;
9. РІС‹Р·С‹РІР°РµС‚ `PositionService.refresh_positions()` С‡РµСЂРµР· runtime hook;
10. РїРёС€РµС‚ `order_reconciliation_completed` Рё `position_reconciliation_completed`;
11. РїСѓР±Р»РёРєСѓРµС‚ `recovery_completed`;
12. РїСЂРё РѕС€РёР±РєРµ РїРёС€РµС‚ `stream_gap_recovery_failed`, РјРµС‚СЂРёРєСѓ failed duration Рё РїРµСЂРµРІРѕРґРёС‚ runtime РІ
    degraded state С‡РµСЂРµР· failure hook.

РњРµС‚СЂРёРєРё recovery:

- `stream_reconnect_total{stream_type,result}`;
- `gap_recovery_duration_seconds{stream_type,status}`;
- `recovered_candles_total{instrument,timeframe,status}`;
- `reconciliation_mismatch_total{result}`.

Р”РµРґСѓРїР»РёРєР°С†РёСЏ РІС‹РїРѕР»РЅСЏРµС‚СЃСЏ РґРѕ РїСѓР±Р»РёРєР°С†РёРё РІ event bus. Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕ `market_candle` С…СЂР°РЅРёС‚СЃСЏ С‡РµСЂРµР·
repository-level upsert РїРѕ `instrument_id + timeframe + open_ts_utc + trading_date`, РїРѕСЌС‚РѕРјСѓ РїРѕРІС‚РѕСЂРЅС‹Р№
backfill РЅРµ РґРѕР»Р¶РµРЅ РїР»РѕРґРёС‚СЊ С„Р°РєС‚С‹ РґР»СЏ Р°РЅР°Р»РёС‚РёРєРё.

## РҐСЂР°РЅРµРЅРёРµ

Р”РѕР±Р°РІР»РµРЅС‹ С‚Р°Р±Р»РёС†С‹:

- `market_candle` - Р·Р°РєСЂС‹С‚С‹Рµ СЃРІРµС‡Рё Рё Р±Р°СЂС‹ СЃ UTC/exchange timestamps;
- `market_status_snapshot` - РЅРѕСЂРјР°Р»РёР·РѕРІР°РЅРЅС‹Р№ status/info snapshot;
- `order_book_summary` - lightweight book summary.
- `market_trade_sample` - persisted data-only trade tape samples from
  `market_trades` stream events or bounded readonly `GetLastTrades` polling.

РџРѕР»РЅС‹Р№ СЃС‚Р°РєР°РЅ РЅР° РєР°Р¶РґС‹Р№ С‚РёРє РЅРµ С…СЂР°РЅРёС‚СЃСЏ РІ PostgreSQL. Р’ Р‘Р” РїРѕРїР°РґР°СЋС‚ Р°РіСЂРµРіР°С‚С‹:
best bid/ask, depth, spread, imbalance, quality score Рё payload РґР»СЏ СЂР°СЃС€РёСЂРµРЅРЅРѕРіРѕ
РєРѕРЅС‚РµРєСЃС‚Р°. Retention-РїРѕР»РёС‚РёРєР° РґР»СЏ С‡Р°СЃС‚РѕС‚С‹ snapshot Р±СѓРґРµС‚ СѓС‚РѕС‡РЅСЏС‚СЊСЃСЏ РїРѕСЃР»Рµ
РїРѕРґРєР»СЋС‡РµРЅРёСЏ СЂРµР°Р»СЊРЅС‹С… РїРѕС‚РѕРєРѕРІ.

## РћРіСЂР°РЅРёС‡РµРЅРёСЏ

- Р РµР°Р»СЊРЅР°СЏ T-Bank stream СЃС…РµРјР° РїРѕРґРєР»СЋС‡РµРЅР° РІ `infra/tbank/sdk_clients.py`, РЅРѕ РІС‹С€Рµ
  `infra/tbank` РїРѕ-РїСЂРµР¶РЅРµРјСѓ РїСЂРѕС…РѕРґСЏС‚ С‚РѕР»СЊРєРѕ SDK-neutral payloads РёР· `StreamEvent`.
- Р”Р»СЏ candle stream SDK wrapper РІС‹СЃС‚Р°РІР»СЏРµС‚ `waiting_close=True`; closed candles РѕСЃС‚Р°СЋС‚СЃСЏ
  primary input, Р° С„РѕСЂРјРёСЂСѓСЋС‰РёРµСЃСЏ СЃРІРµС‡Рё РЅРµ Р·Р°РїСѓСЃРєР°СЋС‚ strategy candidates Р±РµР· СЏРІРЅРѕРіРѕ С„Р»Р°РіР°.
- Deprecated user trade stream РЅРµ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РєР°Рє РёСЃС‚РѕС‡РЅРёРє РёСЃС‚РёРЅС‹ РїРѕ СЃРѕР±СЃС‚РІРµРЅРЅС‹Рј
  РёСЃРїРѕР»РЅРµРЅРёСЏРј; РґР»СЏ СЌС‚РѕРіРѕ РѕСЃС‚Р°РµС‚СЃСЏ broker order/fill reconciliation.
- Market quality score РЅРµ СЏРІР»СЏРµС‚СЃСЏ СЃРёРіРЅР°Р»РѕРј РЅР° СЃРґРµР»РєСѓ СЃР°Рј РїРѕ СЃРµР±Рµ.

## Historical replay from stored candles

`market_candle` С‚РµРїРµСЂСЊ СЏРІР»СЏРµС‚СЃСЏ РІС…РѕРґРѕРј РЅРµ С‚РѕР»СЊРєРѕ РґР»СЏ backfill, РЅРѕ Рё РґР»СЏ
DB-backed historical replay. РљРѕРЅС‚СѓСЂ `HistoricalDbReplayService` С‡РёС‚Р°РµС‚
Р·Р°РєСЂС‹С‚С‹Рµ `5m/10m/15m` bars, СЃРѕР·РґР°РЅРЅС‹Рµ С‡РµСЂРµР· `BarEngine`, Рё РїРµСЂРµРґР°С‘С‚ РёС… РІ С‚РѕС‚
Р¶Рµ strategy/risk/execution/persistence РїСѓС‚СЊ, С‡С‚Рѕ live runtime. Р”Р»СЏ `1m` raw
candles РїСЂРёРјРµРЅСЏРµС‚СЃСЏ С‚РѕР»СЊРєРѕ quality control Рё РїРѕСЃС‚СЂРѕРµРЅРёРµ derived bars.

Historical session context СЃС‚СЂРѕРёС‚СЃСЏ РґРµС‚РµСЂРјРёРЅРёСЂРѕРІР°РЅРЅРѕ: synthetic
`micro_session_id` РёРјРµРµС‚ С„РѕСЂРјР°С‚
`historical:{trading_date}:{session_type}:{HH}`. `weekday_morning`,
`weekday_main`, `weekday_evening` Рё `weekend` РЅРµ СЃРјРµС€РёРІР°СЋС‚СЃСЏ; СЃРІРµС‡Рё РІРЅРµ
fallback trading windows РїРѕР»СѓС‡Р°СЋС‚ `session_phase=closed`.

Replay-generated rows РѕР±СЏР·Р°РЅС‹ РёРјРµС‚СЊ `payload.source=historical_db_replay`.
Р­С‚РѕС‚ РїСЂРёР·РЅР°Рє РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РґР»СЏ РёРґРµРјРїРѕС‚РµРЅС‚РЅРѕСЃС‚Рё Рё РґР»СЏ Р±РµР·РѕРїР°СЃРЅРѕРіРѕ
`--reset-derived-events`, РєРѕС‚РѕСЂС‹Р№ РЅРµ СѓРґР°Р»СЏРµС‚ live/shadow/sandbox С„Р°РєС‚С‹.
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
quality. Market trade events are persisted as real broker samples in
`market_trade_sample`; if stream samples are absent and the collection window is
open, bounded readonly `GetLastTrades` polling may persist real rows. Empty trade
responses produce status/reason only and never fake rows.

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

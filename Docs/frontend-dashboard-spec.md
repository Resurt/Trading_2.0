# Frontend Dashboard Spec

Status: current source of truth, updated 2026-06-30.

Legacy historical content was moved to
`Docs/archive/2026-06-30/frontend-dashboard-spec-legacy.md`. Do not use archived
material as acceptance criteria unless this current spec explicitly references it.

## Purpose

Live Dashboard is the operator surface for two separate concerns:

- market display: quotes, selected-instrument details, order book status, trade tape
  status, session state and freshness;
- data-only logging control: Start/Stop for persistent calibration/logging rows.

Market display must work without pressing Start. Start controls only persistent
data-only logging.

## Primary Live Feed

The primary market display channel is WebSocket `/ws/market-feed`.
`/ws/market` is a compatibility alias that sends the same `market.snapshot`
DashboardMarketFeed payload.

REST endpoints remain fallback and diagnostic paths:

- `GET /dashboard/market-feed/status`
- `GET /dashboard/market-feed/snapshot`
- `GET /market/overview?include_details=false`
- `GET /market/instruments/{instrument_id}/details`

The dashboard opens `/ws/market-feed` on page mount. It must not wait for Start.
The first WebSocket snapshot must include:

- quote rows for the core universe;
- selected instrument id;
- selected instrument details;
- selected order-book summary when available;
- recent market trades or explicit `trade_tape_status` / `trade_tape_reason`;
- session/preflight display metadata;
- freshness/source metadata.

If WebSocket fails, REST polling continues. If REST also fails, the UI keeps the
last good data and shows a degraded/stale warning instead of clearing the board.

## Session Ribbon

The top session ribbon should show operator-ready state, not raw diagnostics. In
normal open-market state it displays the session name and a single `рынок открыт`
detail. It must not render raw trading dates or duplicate equivalent reason codes
such as `рынок открыт · рынок открыт`. Dates are reserved for fallback states where
the market/session state is not known.

## Core Universe

The quote board expects the core universe:

- `SBER`
- `GAZP`
- `LKOH`
- `YDEX`
- `TATN`
- `GMKN`
- `OZON`
- `VTBR`

An empty or partial snapshot must merge by `instrument_id` and must not delete
missing universe rows from the existing board.

## Selected Instrument

Default selected instrument is `MOEX:SBER` only before the user makes a selection.
After the user selects another instrument, backend snapshots and delayed REST
responses must not reset the selection back to SBER.

The frontend sends selected-instrument changes over WebSocket when connected:

```json
{"type":"market.select","selected_instrument":"MOEX:GAZP"}
```

Rules:

- selected details requests use latest-wins semantics;
- each response is applied only if it belongs to the current selected instrument;
- a late SBER response after the user selected GAZP may update only the SBER quote
  row, not `selectedInstrumentId` and not the GAZP details panel;
- selected order book and trade tape must always match the selected instrument
  heading.

## Freshness

Dashboard freshness is dual:

- `received_ts`: when the BFF/API received the broker/read-model response;
- `exchange_ts`: when the exchange data was produced;
- `received_age_ms`;
- `exchange_age_ms`;
- `stale_by_received_time`;
- `stale_by_exchange_time`;
- `freshness_status`;
- `freshness_reason`.

For live order-book snapshots, receipt time is authoritative for operator display:
`exchange_ts` may represent the last exchange-side book change and can remain older
while a fresh broker/read-model snapshot is still current. Last-price-only,
candle, previous-close, broker OTC and broker indicative data remain
exchange-time gated and must not be shown as live calibration data.

Default freshness thresholds are backend-configurable:

- `DASHBOARD_LAST_PRICE_MAX_EXCHANGE_AGE_SECONDS=30`
- `DASHBOARD_SELECTED_BOOK_REFRESH_SECONDS=3`
- `DASHBOARD_ORDER_BOOK_MAX_EXCHANGE_AGE_SECONDS=30`
- `DASHBOARD_TRADES_MAX_EXCHANGE_AGE_SECONDS=15`

## Order Book

The dashboard performs live `GetOrderBook` calls only for the selected instrument.
The quote board must not request full depth for all eight instruments. Quote cards
may still show bid/ask, spread, depth and freshness from the stored
`order_book_summary` read-model produced by data-only collection.
The selected order-book refresh interval must stay below the order-book freshness
budget so an open-market selected ladder does not oscillate between fresh and
stale while the readonly broker/API is responsive.

`order_book_summary.instrument_id` may be stored as the broker `instrument_uid`
or another resolved broker alias. The BFF must resolve `MOEX:*`, ticker,
`instrument_uid`, and `figi` aliases before deciding that a quote card has no
fresh book. A stale `GetLastPrices` response must not downgrade an already fresh
order-book mid/read-model quote.

If no order book is available, the selected panel shows explicit status/reason,
for example:

- `no_order_book_samples`
- `market_closed`
- `get_order_book_timeout`
- `stale`
- `unavailable`

Missing order book must not block the quote board.

## Trade Tape

Selected details must include either recent trades or explicit trade tape status.
Supported status values include:

- `live`
- `stream_connected_no_samples`
- `no_market_trades_samples`
- `get_last_trades_timeout`
- `feed_not_implemented`
- `market_closed`
- `stale`
- `unavailable`

Absence of trades must not hide quotes or order-book status. The UI must show the
status/reason plainly.

If readonly last trades are present but their exchange timestamp is older than the
configured threshold, the UI must show the tape as delayed/stale, for example
`trade_tape_status=stale` and `trade_tape_reason=trade_exchange_ts_too_old`. It
must not label those rows as a live market stream and must not render them as
live tape rows. The table is reserved for fresh market-trades stream samples (or
fresh readonly snapshots when explicitly marked as such); stale diagnostic
`GetLastTrades` rows are represented only by status/reason text.

The frontend must not preserve old trade rows across refreshes when the current
selected details mark the tape stale or unknown.

## Connection Indicator

The top `Broker/API` chip is an operator display signal, not a single websocket
health check. It should stay online when the API is healthy and there is recent
dashboard feed data, fresh quote rows, or an active data-only stream. A degraded
secondary socket or a temporary portfolio/balance failure must not by itself turn
the whole market display into `нет связи`.

## Data-Only Logging Status

Start/Stop command UI must display data-only lifecycle states from
`/runtime/data-shadow/status` and `/robot/status` without collapsing them into a
generic running/stopped label:

- `collecting`: persistent data-only logging is active in the current session
  window;
- `paused_until_next_window`: one daily Start intent is still active and streams
  are paused between same-day windows;
- `stopped_day_complete`: the last collection window for the trading date finished;
- `stopped_by_operator`: operator Stop cancelled the daily intent and auto-resume is
  forbidden;
- `preflight_blocked`: Start was blocked before streams were started.

The UI must keep market display and data-only logging status visually separate.
When market display is online and logging is stopped, use copy like:

```text
Рынок отображается. Запись логов остановлена.
```

## Start/Stop Command UX

Start may call a fast advisory `/session/preflight` for the core universe, but the
operator click must not depend on that request finishing. If the advisory preflight
times out, the frontend still calls `/robot/start`. `/robot/start` is the
authoritative async command endpoint and returns `command_id`, `status=preflight_pending`,
`queued=true`, `next_poll_after_seconds`, and `effective_logging_state=start_pending`.
The backend/trade-core fresh preflight decides whether collector startup is allowed.

`/robot/start` payload must use:

- `mode=data_shadow`;
- the core universe;
- `real_orders_disabled=true`;
- `strategy_trading_disabled=true`.

Command progress is read from `/robot/status` and `/runtime/data-shadow/status`:

- `preflight_pending`: `Запуск сбора логов запрошен. Проверяю сессию...`;
- `preflight_retrying`: `Брокер временно не ответил, повторяю проверку...`;
- `collecting`: `Сбор логов запущен.`;
- `preflight_blocked`: `Сбор логов не запущен: <reason>. Следующая сессия: <time>.`.

Command messages must be short, dismissible and auto-dismiss after 10-15 seconds:

- already running: `Сбор логов уже запущен.`
- started: `Сбор логов запущен.`
- stopped: `Сбор логов остановлен.`
- preflight blocked: `Сбор логов не запущен: <reason>. Следующая сессия: <time>.`
- advisory preflight timeout after command queueing:
  `Брокер временно не ответил, повторяю проверку...`
- start endpoint timeout before command id:
  `Не удалось отправить команду Start. Проверьте API.`

Do not show long technical command payloads in the operator banner.

## Safety Invariants

Dashboard feed is readonly display. It must not:

- write `market_microstructure_snapshot`;
- write primary `order_book_summary` calibration rows;
- create `signal_candidate`;
- create `order_intent`;
- create `broker_order`;
- create `order_state_event`;
- call `PostOrder`;
- call `CancelOrder`.

Dashboard feed may display broker OTC/indicative/stale/local data, but those samples
remain display-only and are not primary calibration evidence.

## Acceptance

Run:

```powershell
python scripts/run_dashboard_live_feed_acceptance.py --selected-instrument MOEX:SBER --switch-instrument MOEX:GAZP --json-output
```

The acceptance must verify:

- API health;
- WebSocket primary connection;
- first snapshot within 3 seconds;
- at least 8 quote rows;
- SBER default selection;
- GAZP selected switch;
- selected details present;
- explicit trade tape status or trades;
- stale candle fallback not labeled live;
- dashboard feed works without Start;
- dashboard feed DB deltas are zero;
- `PostOrder=0`;
- `CancelOrder=0`.

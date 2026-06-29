# Спецификация frontend dashboard

> Current status contract, 2026-06-30: this top block is current.
> Later sections may still contain legacy/historical fragments; keep them for archival history unless a dedicated archival pass moves them.
> Current behavior in this block overrides older legacy wording below.

## Current Data-Only Lifecycle Status

Start/Stop command UI must display data-only lifecycle states from
`/runtime/data-shadow/status` and `/robot/status` without collapsing them into a
generic running/stopped label:

- `collecting`: persistent data-only logging is active in the current session window;
- `paused_until_next_window`: one daily Start intent is still active, streams are
  stopped between same-day windows, and UI should show `next_collection_window_at`;
- `stopped_day_complete`: the last collection window of the trading date finished;
- `stopped_by_operator`: operator Stop cancelled the daily intent and auto-resume is
  forbidden;
- `preflight_blocked`: Start was blocked before streams were started.

The dashboard quote/feed display remains independent from Start. Start controls only
persistent data-only logging and must not be required for quote board, selected
instrument details, order book status, or trade tape status.

Market display primary path, 2026-06-30:

- frontend opens `/ws/market-feed` on dashboard mount; `/ws/market` is a
  compatibility alias for the same DashboardMarketFeed payload;
- REST `/dashboard/market-feed/snapshot` is fallback/diagnostic and must not be
  the only live source;
- the first WS snapshot must arrive without pressing Start and include 8 quote
  rows, selected instrument details, session/preflight display metadata,
  selected order-book summary when available, and either recent trades or an
  explicit trade tape status/reason;
- empty or partial snapshots merge by `instrument_id` and never clear the quote
  board;
- selected-instrument responses are latest-wins. A late SBER selected-details
  response after the user selected GAZP can update the SBER row only; it must not
  change `selectedInstrumentId` or show the SBER book/tape under GAZP;
- freshness is dual: `received_ts` is BFF receipt time, `exchange_ts` is exchange
  data time. Old exchange data must be shown as stale/display-only even if it was
  received now.

Start/Stop command feedback must be short and dismissible. Already-running Start
shows "Сбор логов уже запущен.", a blocked Start shows "Сбор логов не запущен:
<reason>. Следующая сессия: <time>.", and Stop shows "Сбор логов остановлен."
The banner auto-dismisses after 10-15 seconds and can be manually dismissed.

## Live Market Refresh Model

Live Dashboard keeps the last good market snapshot on screen. A failed or slow
refresh must not erase existing quotes, order book, balance, or trade tape.

The dashboard uses two refresh levels:

- fast universe refresh: `/market/quotes/refresh?details=false` updates readonly
  prices for the core universe without loading order book/trade tape for every
  instrument;
- selected instrument refresh:
  `/market/quotes/refresh?instruments=<ticker>&details=true` loads order book
  and recent broker trades only for the selected instrument.

Broker OTC/indicative trades are shown only as operator display data. They must
not be treated as official MOEX tape or calibration samples.

Frontend - СЌС‚Рѕ Vue 3 dark-theme РёРЅС‚РµСЂС„РµР№СЃ РѕРїРµСЂР°С‚РѕСЂР° Рё Р°РЅР°Р»РёС‚РёРєР°. Р­С‚Рѕ РЅРµ landing page Рё РЅРµ РґРµРєРѕСЂР°С‚РёРІРЅР°СЏ РІРёС‚СЂРёРЅР°.

## РўРµС…РЅРѕР»РѕРіРёРё

- Vue 3
- Vite
- Vue Router
- Pinia
- REST client
- WebSocket client
- dark theme design tokens

## BFF РёСЃС‚РѕС‡РЅРёРєРё РґР°РЅРЅС‹С…

Frontend РґРѕР»Р¶РµРЅ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ FastAPI BFF РёР· `Docs/api-contract.md`.

REST endpoints:

- `/robot/status`
- `/session/current`
- `/session/preflight`
- `/portfolio/summary`
- `/portfolio/refresh`
- `/positions`
- `/orders/open`
- `/signals/current`
- `/market/overview`
- `/reports/hourly`
- `/reports/daily`
- `/reports/counterfactual`
- `/config/strategy`

WebSocket channels:

- `/ws/dashboard`
- `/ws/orders`
- `/ws/market`
- `/ws/reports`

РљРѕРјР°РЅРґС‹ СѓРїСЂР°РІР»РµРЅРёСЏ Рё СЂСѓС‡РЅРѕР№ Р·Р°РїСѓСЃРє РѕС‚С‡РµС‚РѕРІ РїСЂРѕС…РѕРґСЏС‚ С‡РµСЂРµР· auth abstraction BFF.
Р’ local-dev frontend РјРѕР¶РµС‚ РѕС‚РїСЂР°РІР»СЏС‚СЊ dev header `X-API-Role: operator`, РЅРѕ
РІ `production` СЌС‚РѕС‚ provider Р·Р°РїСЂРµС‰РµРЅ: РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ bearer-token provider,
Р° СЃР°РјРё РєРѕРјР°РЅРґС‹ СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ РІ `robot_command` Рё `audit_event`.

## РћР±С‰РёР№ layout

РћР±СЏР·Р°С‚РµР»СЊРЅС‹Рµ Р·РѕРЅС‹:

- РІРµСЂС…РЅСЏСЏ status bar;
- Р»РµРІР°СЏ РЅР°РІРёРіР°С†РёСЏ;
- РѕСЃРЅРѕРІРЅР°СЏ СЂР°Р±РѕС‡Р°СЏ РѕР±Р»Р°СЃС‚СЊ;
- РєРѕРјРїР°РєС‚РЅР°СЏ С‚РµРјРЅР°СЏ UI-СЃРёСЃС‚РµРјР° РґР»СЏ РґР»РёС‚РµР»СЊРЅРѕРіРѕ РјРѕРЅРёС‚РѕСЂРёРЅРіР°.

РћСЃРЅРѕРІРЅС‹Рµ СЃС‚СЂР°РЅРёС†С‹:

- `Live Dashboard`
- `Reports`
- `Settings`
- `Logs/Diagnostics`

## Live Dashboard

Live dashboard РґРѕР»Р¶РµРЅ РѕС‚РІРµС‡Р°С‚СЊ РЅР° РґРІР° РІРѕРїСЂРѕСЃР°:

1. Р§С‚Рѕ СЃРµР№С‡Р°СЃ РґРµР»Р°РµС‚ СЂС‹РЅРѕРє?
2. Р§С‚Рѕ СЃРµР№С‡Р°СЃ РґРµР»Р°РµС‚ СЂРѕР±РѕС‚?

РћР±СЏР·Р°С‚РµР»СЊРЅС‹Рµ РїР°РЅРµР»Рё:

- Р±Р°Р»Р°РЅСЃ;
- Р°РєС‚РёРІРЅС‹Рµ РёРЅСЃС‚СЂСѓРјРµРЅС‚С‹;
- Р°РєС‚РёРІРЅС‹Рµ С‚Р°Р№РјС„СЂРµР№РјС‹;
- `session_type`;
- `session_phase`;
- `broker_trading_status`;
- С‚РµРєСѓС‰РёР№ `micro_session_id`;
- countdown РґРѕ rollover;
- `strategy_state`;
- С‚РµРєСѓС‰РёР№ `signal_candidate`;
- С‚РµРєСѓС‰РёР№ `blocker_event`;
- spread;
- mid price;
- market quality score;
- top of book;
- order book widget;
- recent market trades tape;
- positions;
- active orders;
- recent risk events;
- market stream health;
- last closed candle age;
- latest hourly report status.

## Reports

РЎС‚СЂР°РЅРёС†Р° РѕС‚С‡РµС‚РѕРІ РЅСѓР¶РЅР° РґР»СЏ Р°РЅР°Р»РёР·Р° РґРЅСЏ, С‡Р°СЃР°, СЃРµСЃСЃРёРё, РёРЅСЃС‚СЂСѓРјРµРЅС‚Р° Рё blockers.

Р¤РёР»СЊС‚СЂС‹:

- `calendar_date`;
- `trading_date`;
- `session_type`;
- `micro_session_id`;
- instrument;
- timeframe;
- strategy id;
- blocker code;
- order status;
- cancel reason;
- reject reason.

РџР°РЅРµР»Рё:

- day trend / market regime;
- session-wise PnL;
- hourly micro-session comparison;
- candidate funnel;
- blocker ranking;
- execution quality;
- counterfactual outcomes 5/10/15 РјРёРЅСѓС‚;
- infra health;
- risk events list;
- cancelled/rejected orders drill-down.

РљР°Р¶РґС‹Р№ blocker РґРѕР»Р¶РµРЅ РѕС‚РєСЂС‹РІР°С‚СЊСЃСЏ РІ drill-down СЃ:

- `blocker_code`;
- `gate_name`;
- `gate_rank`;
- `reason_payload`;
- market context;
- session context;
- counterfactual result, РµСЃР»Рё РѕРЅ СѓР¶Рµ РїРѕСЃС‡РёС‚Р°РЅ.

## Settings

РќР°С‡Р°Р»СЊРЅС‹Рµ Р±Р»РѕРєРё:

- РІРєР»СЋС‡РµРЅРЅС‹Рµ instruments;
- РІРєР»СЋС‡РµРЅРЅС‹Рµ timeframes;
- strategy config РїРѕ session template;
- risk limits;
- freeze window РїРµСЂРµРґ РіСЂР°РЅРёС†РµР№ micro-session;
- СЂРµР¶РёРј Р·Р°РїСѓСЃРєР°: `historical_replay`, `sandbox`, `shadow`, `production`;
- secret status Р±РµР· РѕС‚РѕР±СЂР°Р¶РµРЅРёСЏ Р·РЅР°С‡РµРЅРёР№ СЃРµРєСЂРµС‚РѕРІ.

## Logs/Diagnostics

Р­С‚Р° СЃС‚СЂР°РЅРёС†Р° РїРѕРєР°Р·С‹РІР°РµС‚ РѕРїРµСЂР°С†РёРѕРЅРЅСѓСЋ РґРёР°РіРЅРѕСЃС‚РёРєСѓ. РћРЅР° РЅРµ Р·Р°РјРµРЅСЏРµС‚ Р°РЅР°Р»РёС‚РёС‡РµСЃРєРёРµ РѕС‚С‡РµС‚С‹ РёР· PostgreSQL.

РџР°РЅРµР»Рё:

- service health;
- reconnects;
- stale data;
- broker/API errors;
- rate limit pressure;
- recent technical errors;
- correlation search РїРѕ `run_id`, `micro_session_id`, `candidate_id`, `order_intent_id`, `request_order_id`, `exchange_order_id`.

## Dark theme tokens

UI РґРѕР»Р¶РµРЅ РѕРїСЂРµРґРµР»РёС‚СЊ tokens:

- background;
- surface;
- elevated surface;
- text primary;
- text secondary;
- text muted;
- border;
- success;
- warning;
- danger;
- info;
- long;
- short;
- flat;
- active;
- inactive;
- disabled.

РРЅС‚РµСЂС„РµР№СЃ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїР»РѕС‚РЅС‹Рј, С‡РёС‚Р°РµРјС‹Рј Рё СѓРґРѕР±РЅС‹Рј РґР»СЏ РїРѕРІС‚РѕСЂСЏСЋС‰РµР№СЃСЏ РѕРїРµСЂР°С‚РѕСЂСЃРєРѕР№ СЂР°Р±РѕС‚С‹.

## Р РµР°Р»РёР·Р°С†РёСЏ С€Р°РіР° 11

Р¤Р°РєС‚РёС‡РµСЃРєР°СЏ СЂРµР°Р»РёР·Р°С†РёСЏ РЅР°С…РѕРґРёС‚СЃСЏ РІ `apps/frontend/src`.

РљР°СЂС‚Р° СЃС‚СЂР°РЅРёС†:

- `LiveDashboardView` - live СЃРѕСЃС‚РѕСЏРЅРёРµ СЂРѕР±РѕС‚Р°, СЃРµСЃСЃРёРё, СЂС‹РЅРєР°, СЃС‚Р°РєР°РЅР°, РїРѕР·РёС†РёР№, Р·Р°СЏРІРѕРє Рё risk events.
- `ReportsView` - С„РёР»СЊС‚СЂС‹, rebuild daily report, daily/hourly reports, blocker ranking, counterfactual missed opportunities Рё summary charts.
- `SettingsView` - strategy config РїРѕ session template, risk limits, active instruments/timeframes Рё secret status Р±РµР· Р·РЅР°С‡РµРЅРёР№ СЃРµРєСЂРµС‚РѕРІ.
- `DiagnosticsView` - WebSocket/API degraded states, correlation search Рё cancelled/rejected order reason codes.

РљР»СЋС‡РµРІС‹Рµ РєРѕРјРїРѕРЅРµРЅС‚С‹:

- `DataPanel` - Р±Р°Р·РѕРІР°СЏ СЂР°Р±РѕС‡Р°СЏ РїР°РЅРµР»СЊ.
- `MetricTile` - РєРѕРјРїР°РєС‚РЅР°СЏ РјРµС‚СЂРёРєР°.
- `StatusPill` - readable label + machine-readable code.
- `EmptyState` - РїСѓСЃС‚РѕРµ РёР»Рё degraded СЃРѕСЃС‚РѕСЏРЅРёРµ.
- `MiniBars` - РїСЂРѕСЃС‚С‹Рµ summary charts Р±РµР· С‚СЏР¶РµР»РѕР№ chart-Р±РёР±Р»РёРѕС‚РµРєРё.
- `OrderBookWidget` - top-of-book Рё lightweight depth summary.
- `SignalReasonCard` - С‚РµРєСѓС‰РёР№ candidate/blocker СЃ reason code.
- `RiskEventsList` - РїРѕСЃР»РµРґРЅРёРµ candidate/blocker СЃРѕР±С‹С‚РёСЏ.

Pinia stores:

- Live Dashboard bootstrap - one aggregated `GET /dashboard/state` snapshot.
- `robot` - dashboard snapshot, `/session/preflight`, `/portfolio/refresh`,
  `/ws/dashboard`, start/stop commands and last command result.
- `market` - dashboard snapshot, `/dashboard/market-feed/snapshot`,
  `/dashboard/market-feed/status`, `/market/overview`, `/ws/market`, selected
  instrument details endpoint and top-of-book read model.
- `portfolio` - dashboard snapshot, `/positions`, `/orders/open`, `/ws/orders`.
- `reports` - loaded on the Reports page only: `/reports/hourly`, `/reports/daily`,
  `/reports/counterfactual`, `/reports/daily/run`, `/ws/reports`.

Live widgets:

- balance;
- session type / phase / broker trading status;
- current micro-session Рё countdown РґРѕ rollover;
- strategy state;
- current signal/candidate/blocker;
- spread / mid price / market quality;
- top-of-book Рё order book summary;
- recent market trades tape;
- positions;
- active orders;
- recent risk events;
- degraded flags;
- latest hourly report;
- freshness timestamps.

REST РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РґР»СЏ initial snapshot/history. WebSocket РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РґР»СЏ snapshot/live
РѕР±РЅРѕРІР»РµРЅРёР№. BFF WebSocket РґРµСЂР¶РёС‚ СЃРѕРµРґРёРЅРµРЅРёРµ РѕС‚РєСЂС‹С‚С‹Рј, РѕС‚РїСЂР°РІР»СЏРµС‚ snapshot РїСЂРё
РїРѕРґРєР»СЋС‡РµРЅРёРё, Р·Р°С‚РµРј push-РѕР±РЅРѕРІР»РµРЅРёСЏ РїРѕ interval Рё heartbeat.

## Current pages and blocks

Current pages:

- Live Dashboard
- Historical Data
- Reports
- Intraday Analytics
- Calibration / Calibration Center
- Settings
- Logs / Diagnostics

Current Live Dashboard blocks:

- Balance card
- session type, phase, broker status and micro-session
- one cockpit-style broker/API connection chip with a human label, not repeated
  `panel/quotes/portfolio` chips in several places
- compact quote cards for the core universe: SBER, GAZP, LKOH, YDEX, TATN, GMKN, OZON, VTBR
- Data-only Shadow Status
- selected instrument panel with price, bid/ask, mid, spread, depth, imbalance and book quality
- selected instrument market depth block: order-book ladder on the left and market trades tape on the right
- current signal and blocker reason
- recent risk events
- stream health

Balance card:

- shows portfolio value, available cash and blocked cash in the primary card;
- does not show full account id; the main dashboard card also avoids account id,
  expected yield and freshness clutter;
- auto-refreshes broker balance through readonly `POST /portfolio/refresh` while the
  dashboard is open;
- shows human-readable degraded reasons when broker balance is missing; raw technical
  codes are not the primary UI text;
- remains readonly account state in data-only shadow mode and never implies trading permission.

Start/Stop command feedback:

- Start first calls `GET /session/preflight` with the core data-only universe.
- If preflight fails, Start shows `preflight_unavailable` and does not call
  `POST /robot/start`.
- If `market_open=false` or `data_only_collection_allowed=false`, Start shows
  blocked state, human `reason_code` and `next_session_at` when present. The frontend
  does not call `POST /robot/start`; no command is sent to control plane and no stream
  can start from a closed-market click.
- If `market_open=true`, Start submits the data-only start command and shows the
  returned `RobotCommandResponse`.
- During Start, the button shows an animated progress state and a compact command strip
  appears only while a command is running or after a real command result.
- Stop always submits controlled stop and shows requested/stopped/error result in
  the command status panel.
- The latest command state is visible in human-readable form with reason and next
  session when applicable; the dashboard must not show an empty technical
  `last command` block before any command exists.

Quotes:

- Dashboard Live Feed is independent from the data-only Start button. It starts on
  page mount through `/ws/market-feed` (`/ws/market` remains an alias) and may run
  while `collector_state=stopped`. REST
  `/dashboard/market-feed/snapshot` is fallback/diagnostic.
- Start controls only persistent data-only logging. If dashboard feed is online and
  data-only is stopped, the UI says: "Рынок отображается. Запись логов остановлена."
- `GET /market/overview?include_details=false` is cheap and must return all eight
  core universe rows without heavy all-instrument order-book level payloads.
- `GET /market/instruments/{instrument_id}/details` and
  `/dashboard/market-feed/snapshot?selected_instrument=...` load bid/ask ladder,
  market trades and detailed source/freshness only for the selected instrument.
  SBER is the default selection.
- Quote rows show `last_price`, `last_price_at`, `last_price_source`,
  `quote_status`, `received_ts`, `exchange_ts`, `received_age_ms`,
  `exchange_age_ms`, `freshness_status`, `freshness_reason`, spread bps, bid/ask,
  depth, imbalance and book quality where available.
- Source priority is fresh Dashboard Live Feed order-book mid, readonly T-Bank quote,
  latest known candle close, previous close, then unavailable.
- `POST /market/quotes/refresh` is an explicit operator/diagnostic readonly broker
  refresh path for `GetLastPrices`/`GetOrderBook`; dashboard mount and polling do not
  call it for all instruments.
- A successful readonly `GetOrderBook` response is not fresh solely because the
  broker responded now. The UI must compare both BFF receipt time and exchange
  timestamp; old or missing `exchange_ts` is stale/display-only.
- Successful readonly quote refresh rows are cached briefly by the API, so a later
  `/market/overview` or dashboard snapshot cannot immediately replace live broker
  quotes with older candle fallback rows.
- Update model while the dashboard is open: `/ws/market-feed` sends the first
  snapshot immediately and then bounded refresh snapshots. REST polling is used
  only as fallback/diagnostic; `/runtime/data-shadow/status` refreshes every
  2-5 seconds; broker `/portfolio/refresh` every 60 seconds.
- If a refresh request times out, the dashboard keeps the last good quote instead of
  clearing the quote grid.
- `/ws/market-feed` and `/ws/market` updates merge by `instrument_id`. Empty
  snapshots add `empty_market_ws_snapshot` warning and must never clear the quote
  board; partial snapshots update present rows without deleting missing core
  instruments.
- The quote grid is card-based and must not require horizontal scrolling for the
  eight core instruments.
- Stale candles/order books remain visible with timestamp and stale badge, never as
  current live prices.
- Balance, session state, current/last prices, selected order book and explicit trade
  tape status must be visible without starting strategy shadow, live trading, or
  data-only collection.

Selected instrument depth:

- When the operator clicks a core universe row, the selected instrument block must
  render an order-book ladder with bid price, bid volume, ask price and ask volume.
- The selected instrument depth/tape layout must stay inside the selected
  instrument panel. It may stack ladder and tape vertically on narrower screens to
  avoid overlap with the side column.
- Ladder volume bars use only real `order_book_summary.bids`/`asks` levels from
  live data-only storage or explicit readonly `GetOrderBook` refresh. The UI must
  not invent synthetic depth levels.
- If there are no bid/ask levels, the block stays visible and shows the reason
  (`no_order_book_samples`, market closed, stream unavailable, stale book, etc.).
- The right side shows recent market trades when the market trades stream exists.
  If not available, it shows `no_market_trades_samples` instead of an empty panel.
- Display quality and calibration quality are separate. Without a real, fresh order
  book the UI shows `нет стакана`, `display-only` and `not_for_calibration` instead
  of rendering a synthetic percentage such as 35%.

Analytics pages:

- Historical Data: candle quality, instrument registry, dividend/special-day state and replay links.
- Intraday Analytics: session tabs for morning/main/evening/weekend, per-session summary, hour/micro-session rows and instrument x timeframe x side table.
- Calibration Center: Run Diagnostics, diagnosis, rolling performance cube, filters, market regime summary, top/dead contours, warnings and candidate config proposals.
## Market Source Display

The Live Dashboard must not show broker OTC/indicative quotes as MOEX live. Top-level
status separates official MOEX session, broker quote availability, data-only collection,
and trading-disabled state. Quote cards show source, venue, freshness, spread in
`bps / RUB`, and whether the row is eligible for calibration.

When MOEX is officially closed and broker quotes are available, the UI displays a
broker/indicative badge and a closed-exchange reason. Stale candle fallback remains
visible with timestamp and stale badge, but is never labelled live.

The selected instrument panel shows venue type, quote source, bid/ask/mid, spread,
depth, imbalance, display quality, calibration quality, order-book age, and trade tape
status. `/ws/market-feed` is the primary live path, `/ws/market` is a compatibility
alias, and REST polling is fallback/diagnostic that must not clear last good data on
timeout.

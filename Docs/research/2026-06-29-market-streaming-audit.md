# 2026-06-29 Market Streaming Audit

## Summary

Audit scope: data-only lifecycle after the 2026-06-29 morning collection and the
operator Live Dashboard market display. The goal was to make Start a persistent
full-day logging intent while keeping dashboard market display independent from
Start.

## Scope

- Backend data-only lifecycle: morning, main, evening window rollover, manual Stop,
  end-of-day completion, no calibration rows in gaps.
- Dashboard market display: quote board, selected instrument details, order book,
  trade tape status, WebSocket primary path, REST fallback, staleness handling.
- Safety invariants: no `PostOrder`, no `CancelOrder`, no production mode, no
  strategy shadow, no pseudo-orders, no trading entities in data-only mode.

## Findings From Research

Согласно исследованию было выявлено, что `/ws/market` не был фактическим primary
live broker feed: он отдавал DB/read-model market overview с cache overlay, тогда
как live readonly broker calls выполнялись через REST DashboardMarketFeedService.

Согласно исследованию было выявлено, что selected order book и trade tape
обновлялись REST polling’ом, а не market WebSocket stream.

Согласно исследованию было выявлено, что out-of-order selected details responses
могли откатывать выбранный инструмент и показывать старый стакан/ленту.

Согласно исследованию было выявлено, что freshness могла считаться по broker
response receipt time без достаточной защиты от старого exchange_ts.

The 2026-06-29 data-only audit also found that the collector completed
`weekday_morning` and did not resume for `weekday_main`/`weekday_evening`.

## Fixed Issues

- `/ws/market-feed` is now the primary Dashboard Live Feed WebSocket. `/ws/market`
  remains a compatibility alias and sends the same `market.snapshot` payload.
- The first dashboard market snapshot is sent without pressing Start.
- The WebSocket snapshot includes quote rows, selected instrument details,
  selected order-book summary when available, explicit trade tape status/reason,
  and session/freshness metadata.
- Frontend starts dashboard feed on mount without Start and keeps REST snapshot as
  fallback.
- Selected instrument changes are sent with `market.select`; late responses update
  only their own row and do not overwrite the current selected instrument.
- Freshness is dual: `received_ts`/`received_age_ms` and
  `exchange_ts`/`exchange_age_ms` are separate. Old exchange data is stale even if
  received now.
- Trade tape absence is explicit through `trade_tape_status` and
  `trade_tape_reason`.
- Start/Stop command notifications are short, auto-dismissed and manually
  dismissible.
- Backend full-day lifecycle supports daily Start intent, pause/resume between
  same-day windows, manual Stop cancellation, and end-of-day completion.

## Remaining Risks

- Trade tape can legitimately return `no_market_trades_samples` when broker
  readonly last trades provide no samples or time out. This is not a blocker for
  order-book microstructure display.
- Market candles can remain absent in data-only microstructure mode unless a
  separate candle backfill/import task is run.
- Historical documents may contain legacy wording or mojibake. They are preserved
  for audit history and should be archived only by a dedicated documentation
  archival pass.

## Acceptance Checks

- `scripts/run_data_only_full_day_lifecycle_check.py` verifies morning/main/evening
  lifecycle, pause/resume, Stop semantics, no rows in gaps, no trading entities,
  and status/audit fields.
- `scripts/run_dashboard_live_feed_acceptance.py` verifies WS primary connection,
  first snapshot without Start, at least 8 quote rows, selected switch to GAZP,
  explicit trade tape status, stale data not labeled live, DB deltas zero for
  calibration/trading tables, and zero PostOrder/CancelOrder calls.

## Operator-Facing Behavior

- Start/Stop не нужны для отображения рынка; Start управляет только persistent
  data-only logging.
- Dashboard feed не пишет market_microstructure_snapshot/calibration/trading
  entities и не вызывает trading methods.
- Quote board, selected order book, and trade tape status are display-only unless
  data-only collection has been explicitly started and fresh preflight allows
  persistent calibration logging.

# Trading 2.0

Проект торгового робота для Московской биржи через T-Invest API.

Целевая архитектура:

- backend полностью на Python;
- frontend на Vue 3 в dark theme;
- `trade-core` как долгоживущий критический контейнер;
- T-Bank gRPC как primary broker transport;
- FastAPI BFF + WebSocket для live dashboard;
- PostgreSQL как source of truth по состоянию, ордерам, событиям, отчетам и аудиту;
- Redis для Celery и coordination/cache;
- Prometheus + Grafana для метрик;
- Loki + Fluent Bit для technical logs.

## Обязательное чтение перед разработкой

Перед любой задачей нужно прочитать:

- `Docs/architecture.md`
- `Docs/implementation-plan.md`
- `Docs/logging-analytics-spec.md`
- `Docs/logging_analytics_architecture.md`
- `Docs/logging_analytics_event_taxonomy.md`
- `Docs/logging_analytics_rollout_plan.md`
- `Docs/database-schema.md`
- `Docs/broker-gateway.md`
- `Docs/session-manager.md`
- `Docs/market-data-pipeline.md`
- `Docs/historical-candle-backfill.md`
- `Docs/runbooks/historical-replay.md`
- `Docs/runbooks/calibration.md`
- `Docs/runbooks/analytics-and-calibration-center.md`
- `Docs/runbooks/data-retention-policy.md`
- `Docs/runbooks/corporate-actions.md`
- `Docs/runbooks/final-historical-calibration.md`
- `Docs/strategy-risk-execution.md`
- `Docs/observability_runbook.md`
- `Docs/live-analytics-bff.md`
- `Docs/logging_analytics_acceptance.md`
- все ADR из `Docs/adr/`

Если в ходе задачи меняется архитектурное решение, нужно обновить `Docs/` и соответствующий ADR в том же шаге.

## Текущее состояние

На этом этапе зафиксирована документация проекта, создан monorepo-каркас, добавлены
инфраструктурный compose-стек, схема PostgreSQL, BrokerGateway для T-Bank,
сессионная модель с hourly micro-sessions, market data pipeline с bar engine и
каркас strategy/risk/execution/reconciliation без прибыльной бизнес-логики.
Также добавлены structured JSON logging, Prometheus metrics registry и Grafana
dashboards provisioning для production-like observability. `report-worker`
содержит Celery task pipeline, hourly/daily reports, counterfactual analytics и
ручные CLI-скрипты для запуска отчетов вне FastAPI. `api` содержит FastAPI BFF
с REST endpoints для управления, live read models, отчетов, strategy config и
live WebSocket channels для dashboard/orders/market/reports. В production-like
режимах WebSocket в браузере авторизуется через короткоживущий ticket из
`POST /auth/ws-ticket`, а REST использует bearer auth. `frontend`
содержит Vue 3 dark-theme UI для live dashboard, reports, settings и diagnostics
с Pinia stores, REST snapshots и live WebSocket updates.

## Каркас репозитория

- `apps/trade-core` - долгоживущий Python runtime для session/market/strategy/risk/execution orchestration.
- `apps/api` - FastAPI BFF для управления, read models, отчетов и live WebSocket feeds.
- `apps/report-worker` - Celery/report worker для hourly/daily/counterfactual analytics.
- `apps/frontend` - Vue 3 + Vite dark-theme операторский UI.
- `packages/common` - общие enums и dataclasses.
- `tests` - backend unit/smoke/acceptance tests для runtime, API, SDK wrapper, analytics и launch gates.
- `scripts` - вспомогательные скрипты совместимости.
- `tools/reports` - CLI для hourly/daily/counterfactual отчетов вне FastAPI.

## Локальные проверки

```bash
python -m pytest
python -m ruff check .
python -m mypy
python scripts/run_frontend_text_encoding_check.py
cd apps/frontend && npm run build
cd apps/frontend && npm run typecheck
cd apps/frontend && npm run test:unit
```

На Windows, если PowerShell блокирует `npm.ps1`, используйте `npm.cmd`.

Единая локальная проверка без зависимости от `make`:

```bash
python scripts/check.py
```

Полный local controlled-launch acceptance без реальных broker orders:

```bash
python scripts/run_controlled_launch_acceptance.py
```

Быстрый вариант, если `scripts/check.py` уже запускался отдельно:

```bash
python scripts/run_controlled_launch_acceptance.py --skip-full-check
```

Этот gate проверяет analytics-smoke, report rebuild, replay-day,
`docker compose config`, SQLite migration upgrade/downgrade/upgrade,
sandbox dry-run, production safety guards и secret scan.

Реальный T-Bank SDK wrapper подключается optional extra, чтобы обычный CI не зависел от
T-Bank package index:

```powershell
python -m pip install -e ".[tbank]" --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
$env:SSL_TBANK_VERIFY = "true"
$env:TBANK_UNARY_TIMEOUT_FLOOR_SECONDS = "5.0"
python scripts/run_sandbox_smoke.py --dry-run
```

Для реального readonly smoke без `--dry-run` токены должны лежать в ignored
`secrets/tbank_full_access_token` / `secrets/tbank_readonly_token`, а переменные
`TBANK_*_TOKEN_FILE` должны указывать на эти файлы. `SSL_TBANK_VERIFY=true`
включает bundled Russian Trusted Root CA в официальном T-Bank SDK; TLS verification
не отключается.

## Trade-core runtime

`python -m trade_core.service` запускает HTTP `/health` и `/metrics`, а также
фоновый `TradeCoreRuntime`. Безопасный режим по умолчанию - `historical_replay`:
он открывает logical micro-sessions, пишет domain events в БД, строит closed bars,
создаёт `signal_candidate`, прогоняет risk gates и создаёт pseudo-orders без
реальных broker calls.

Минимальный локальный запуск без T-Bank токенов:

```powershell
$env:TRADING_RUNTIME_MODE = "historical_replay"
python -m trade_core.service
```

Production не стартует без явного `TRADING_PRODUCTION_CONFIRM=I_UNDERSTAND_LIVE_ORDERS`.

### Launch blocker fixes

- В Docker Compose `trade-core`, `api` и `report-worker` используют один PostgreSQL через `POSTGRES_HOST=postgres`, `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD_FILE`.
- Runtime больше не уходит в SQLite молча. SQLite fallback разрешён только при явном `TRADING_RUNTIME_LOCAL_SQLITE=1` для локальных одно-процессных экспериментов.
- `trade-core` пишет в startup log/audit `database_backend` и `database_url_redacted`.
- Sandbox/shadow/production проверяют наличие T-Bank SDK extra; контейнерный build ставит `.[tbank]` через официальный T-Bank package index.
- Инструменты `SBER,GAZP` резолвятся через T-Bank instruments API в реальные `instrument_uid`/canonical `instrument_id`; placeholder UID запрещён для sandbox/shadow/production.
- API production использует `TRADING_AUTH_MODE=static_bearer`; браузерные WebSocket соединения получают короткоживущий ticket через `POST /auth/ws-ticket`.
- Для расширенной приёмки используйте `python scripts/run_launch_readiness.py --mode local|compose|sandbox|shadow|production-preflight`.

Приёмка logging/analytics слоя для калибровки:

```bash
make analytics-smoke
make report-rebuild
make replay-day
```

Историческая загрузка свечей перед replay/calibration:

```bash
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP --lookback-days 90 --dry-run
```

Реальная загрузка использует только readonly T-Bank methods и пишет raw `1m`
candles плюс derived `5m/10m/15m` bars в `market_candle`. Подробности:
`Docs/historical-candle-backfill.md`.

После загрузки свечей исторический контур проверяет качество `market_candle`,
запускает DB-backed replay, строит counterfactual `+5m/+10m/+15m`,
пересобирает historical hourly/daily reports и формирует calibration report без
реальных `PostOrder`/`CancelOrder`:

```bash
make historical-quality LOOKBACK_DAYS=90
make historical-replay LOOKBACK_DAYS=90
make historical-counterfactual LOOKBACK_DAYS=90
make historical-report-rebuild LOOKBACK_DAYS=90
make calibration-report LOOKBACK_DAYS=90
make dividend-sync-730d
make market-special-days-future
make corporate-actions-import LOOKBACK_DAYS=90
make market-special-days LOOKBACK_DAYS=90
make calibration-primary LOOKBACK_DAYS=90
```

Операционный порядок: `Docs/runbooks/historical-replay.md` и
`Docs/runbooks/calibration.md`. Перед final calibration обязательно выполните
`Docs/runbooks/corporate-actions.md` и `Docs/runbooks/final-historical-calibration.md`:
dividend/corporate-action дни исключаются из primary calibration по умолчанию.
Primary corporate-action path is T-Bank `GetDividends` via `run_tbank_dividend_sync.py`;
manual CSV/JSON import is fallback/override only and does not make final calibration clean
unless the operator explicitly allows manual corporate actions.
Partial dividend sync is not clean: `completed_with_errors`, `failed`,
`failed_instruments > 0`, or `error_count > 0` blocks final calibration,
shadow readiness and production preflight. The latest status is persisted in
`dividend_sync_run`.

## Intraday Analytics and Calibration Center

Two diagnostic analytics surfaces are available:

- `Intraday Analytics`: current trading-day summaries by session, hour/micro-session,
  instrument, timeframe and side. It explains market bias/activity, spread/depth/imbalance,
  blockers, near misses and no-trade reasons. It is diagnostic only and does not enable trading.
- `Calibration Center`: rolling performance cube, robot-health diagnostics, no-trade diagnosis,
  market regime/drift snapshots and draft strategy config candidate proposals.

CLI:

```bash
python scripts/run_intraday_analytics.py --date YYYY-MM-DD --json-output
python scripts/run_calibration_observatory.py --universe SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --lookback-days 20 --json-output
```

Outputs are written under `.local/collection_reports/intraday/` and
`.local/collection_reports/calibration_observatory/`.

Safety invariants:

- no real `PostOrder` or `CancelOrder` is performed by analytics;
- `strategy_config_candidate` stores proposals only, initially `draft`;
- approving a candidate changes candidate status only and does not mutate active runtime config;
- 10-20 trading days are early evidence, not final truth, and must not hard-disable a contour;
- any actual runtime config application remains a separate future operator/admin workflow.

## Локальный Docker Compose

Создайте локальные Docker secrets в папке `secrets/` и не коммитьте их:

```bash
mkdir -p secrets
printf "local_postgres_password" > secrets/postgres_password
printf "local_grafana_password" > secrets/grafana_admin_password
printf "paste_full_access_token_here" > secrets/tbank_full_access_token
printf "paste_readonly_token_here" > secrets/tbank_readonly_token
```

Запуск:

```bash
docker compose up -d --build
docker compose ps
python -m alembic upgrade head
docker compose logs -f --tail=200
```

Локальные адреса:

- frontend: `http://localhost:5173`
- api health: `http://localhost:8000/health`
- trade-core health: `http://localhost:8001/health`
- report-worker health: `http://localhost:8002/health`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`
- Loki: `http://localhost:3100/ready`

## Instrument Resolution

Before real readonly dividend sync, historical candle backfill, shadow or
production, resolve internal canonical instruments to T-Bank `instrument_uid` /
`figi`:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
```

`MOEX:SBER` and `MOEX:GAZP` remain internal canonical ids for analytics and
reports. They are not broker ids and must not be sent to `GetDividends`,
`GetCandles`, streams or order placement in sandbox/shadow/production.

## Data-only Shadow

Data-only shadow is the next step after negative candle-only historical research. It collects
readonly live microstructure for spread, depth, imbalance, latency and stream-health calibration.
It is not trading shadow and not strategy shadow.

```powershell
set TRADING_DATA_ONLY_SHADOW=true
python scripts/run_data_only_shadow_smoke.py --instruments SBER,GAZP --minutes 10 --require-dividend-sync --json-output
python scripts/run_data_shadow_summary_report.py --lookback-hours 6 --json-output
python scripts/run_launch_readiness.py --mode data-shadow --instruments SBER,GAZP --shadow-minutes 10
```

Data-only shadow writes `market_microstructure_snapshot` and exposes
`/market/microstructure/latest`, `/market/microstructure/summary`, and
`/runtime/data-shadow/status`. It does not create `signal_candidate`, `order_intent`,
`broker_order`, pseudo-orders, `PostOrder`, or `CancelOrder`.

Before using the dashboard Start button or a live data-only smoke, run session preflight.
Closed market is reported as `market_closed_expected` with `next_session_at` and is not
a strategy failure:

```powershell
python scripts/run_data_only_shadow_smoke.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR --minutes 0 --preflight-only --require-dividend-sync --json-output
```

The dashboard Start action may call `/session/preflight` first, but that check is
advisory. A broker preflight timeout must not dead-end the operator click. The
button calls `POST /robot/start`, which quickly creates a durable command with
`status=preflight_pending`, `command_id`, `queued=true` and
`effective_logging_state=start_pending`. `trade-core` performs the authoritative
fresh preflight/retry in the background and then either starts data-only
collection or marks the command blocked with `reason_code` and `next_session_at`
when available.
Dashboard readonly quote/order-book refresh uses a bounded broker executor and is
briefly paused during Start so it does not starve the command preflight. API
`/health` is service liveness; broker connectivity degradation belongs in status
payloads, not in the container health endpoint.
For incident triage, compare the CLI result with
`GET /session/preflight?...&cache=false`. If T-Bank `TradingSchedules` omits the
current local MOEX fallback window but broker `GetTradingStatus` reports exchange
trading or readonly `GetLastPrices`/`GetOrderBook` probe calls work, preflight uses
`source=broker_status_fallback_time_rules` and only opens data-only collection for
working instruments. If status and probe are both unavailable, Start stays blocked
with `reason_code=broker_status_and_market_data_unavailable`. In data-only mode
`trading_allowed=false` even when collection is allowed.
`/session/current` and `/robot/status` use the same fresh preflight decision for
operator-facing session state; stale runtime `session_run` rows are marked with
`session_stale`/`stale_reason` and must not make a closed market look active.

After Start is accepted, trade-core uses the minimal data-only stream set
(`order_book`, `last_prices`, `trading_status`). If stream order books are silent
but readonly `GetOrderBook` remains available, a bounded polling fallback writes
`market_microstructure_snapshot` through the same calculation pipeline. The fallback
is disabled by Stop and never calls `PostOrder`, `CancelOrder`, or creates trading
entities.

One operator Start is a daily data-only collection intent, not a single-session
toggle. On weekdays the runtime must collect `weekday_morning`, pause at the
morning cutoff as `paused_until_next_window`, auto-resume for `weekday_main`, pause
again before `weekday_evening`, and finish as `stopped_day_complete` after the last
window. Manual Stop cancels the daily intent and prevents auto-resume. Runtime and
API status distinguish `robot_control_state`, `data_shadow_collector_state`,
`daily_collection_active`, and `effective_logging_state` so a stopped or paused
collector is not reported as simply running.

Data-only Start is market-data-only. Runtime micro-session position snapshots are
skipped, so account-level `GetPositions`/`GetPortfolio` calls happen only through
explicit balance diagnostics such as `/portfolio/refresh`.

Known-invalid primary data-only rows are not retained as rejected calibration
samples. If a bug writes `market_microstructure_snapshot` or `order_book_summary`
after the session cutoff, during official closure, in OTC/indicative mode, from
stale local history, or with wrong session context, maintenance must write a
purge manifest, preserve the incident in `audit_event`, and remove those rows
from primary calibration/logging tables. The protected CLI is
`scripts/run_purge_invalid_data_shadow_rows.py`.

The Start button must show an animated preflight/start progress state, not a silent
disabled button. The command strip shows the phase, message, reason code and next
session time when available. Success and already-running messages auto-dismiss
after 10-15 seconds; blocked/failed messages remain dismissible.

Broker balance can be refreshed independently of market hours. This is readonly account
state for operator visibility and never enables trading:

```powershell
python scripts/run_broker_balance_refresh.py --json-output
```

The Live Dashboard bootstraps from one aggregated `/dashboard/state` snapshot and then
uses app-level WebSockets (`/ws/dashboard`, `/ws/market`, `/ws/orders`) plus bounded
fallback polling. Frontend containers should use same-origin `/api` and `/ws` so the
browser does not fan out directly to `localhost:8000`.

The Live Dashboard auto-refreshes broker balance through readonly `/portfolio/refresh`
while the page is open. The API container must have the readonly T-Bank token mounted;
if `GetAccounts` is unavailable but `TRADING_ACCOUNT_ID` is set, refresh can still use
that account id internally. The main dashboard card shows portfolio value, available
cash and blocked cash only; full account ids are never rendered.

The Live Dashboard also shows quotes for the core universe through a readonly
Dashboard Live Feed. This feed is independent from the Start button: it can display
last prices, selected-instrument order book and trade-tape status while the
data-only collector is stopped. Start controls only persistent data-only log writing.

The feed is exposed primarily through WebSocket `/ws/market-feed`; `/ws/market`
is kept as a compatible alias for the same DashboardMarketFeed snapshot. REST
`/dashboard/market-feed/status` and `/dashboard/market-feed/snapshot` are fallback
and diagnostic endpoints. `GET /market/overview` is the cheap quote-board read-model
backed by the feed cache first, then stored `order_book_summary`, `market_candle`
and previous-close fallbacks. It always returns one row per core instrument and
avoids heavy all-instrument order-book calls. Selected-instrument bid/ask ladder
and trade tape come through the selected snapshot/details fields; the frontend
sends `market.select` over the WebSocket when the operator switches instruments.

Explicit readonly broker quote refresh remains `POST /market/quotes/refresh`.
Temporary request failures must not clear already displayed quotes; if the readonly
gateway is unavailable, refresh falls back to the local overview instead of blocking
the dashboard. Dashboard feed calls are readonly and must not write calibration logs,
create trading entities, or call `PostOrder`/`CancelOrder`.

Dashboard polling is intentionally split: quote board every 2 seconds, selected
instrument every 1 second while the feed sees the market open and every 5 seconds
when closed/stale, selected broker trading status every 5 seconds for the session
ribbon, data-only status every 2-5 seconds, and broker balance refresh every
60 seconds. Polling is silent and must not clear the last good balance or quote rows
on timeout. Empty or partial `/ws/market` snapshots are merged into the existing board
and never delete missing core-universe rows.

Dashboard freshness uses both BFF receipt time and exchange data time:
`received_ts`/`received_age_ms` are not enough to mark data live. Old
`exchange_ts` data must show as stale/display-only, and stale candles must not be
labeled live. Trade tape either contains recent readonly trades or exposes an
explicit `trade_tape_status`/`trade_tape_reason`.

Runbook: `Docs/runbooks/data-only-shadow.md`.

## Documentation acceptance rule

When code changes affect runtime behavior, API contracts, database schema, frontend surfaces or operator workflows, docs must be updated in the same change. If code changes but docs do not, the final response must explain why docs were not affected.

Current documentation index: `Docs/README.md`.

## Market Source Semantics

Official MOEX calendar status is the top-level gate for data-only calibration collection.
Broker availability is not the same thing as an official exchange session: T-Invest may
return broker/OTC/indicative quotes while MOEX is officially closed. Those quotes may be
displayed on the Live Dashboard, but they are tagged as `broker_quote_exchange_closed`,
`broker_otc_order_book`, or `broker_indicative_quote` and are excluded from calibration by
default.
`/runtime/data-shadow/status` also exposes supervisor state and restart/stale counters so
an intentionally stopped collector is visible as stopped rather than silently restarted.

The local MOEX calendar includes the 2026-06-20 and 2026-06-21 DSV(D) cancellation for
stock and derivatives markets due to the planned trading/clearing platform update. This
override is a local fixture, not an internet dependency. Start is blocked with
`moex_dsvd_cancelled_platform_update` on those dates.

Spread units are explicit: `spread_abs`/`spread_abs_rub` are RUB, while
`spread_bps = (best_ask - best_bid) / mid_price * 10000`. Market quality is split into
display quality and calibration quality. Display quality describes the visible book;
calibration quality is zero/not applicable when the venue is not `official_exchange`.

The frontend uses `/ws/market-feed` as the primary live market update path
(`/ws/market` remains an alias) and REST polling as a fallback. Failed refreshes
must not clear the last good quotes; stale/local candle fallbacks must show source
and timestamp.

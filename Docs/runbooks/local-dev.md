# Local Development Runbook

## РќР°Р·РЅР°С‡РµРЅРёРµ

Р›РѕРєР°Р»СЊРЅС‹Р№ Р·Р°РїСѓСЃРє РЅСѓР¶РµРЅ РґР»СЏ РїСЂРѕРІРµСЂРєРё runtime wiring, health endpoints, Prometheus/Grafana/Loki Рё Docker Compose wiring. РџРѕ СѓРјРѕР»С‡Р°РЅРёСЋ `trade-core` СЃС‚Р°СЂС‚СѓРµС‚ РІ Р±РµР·РѕРїР°СЃРЅРѕРј `historical_replay`: РѕРЅ РІРµРґС‘С‚ session/micro-session loop, РїРёС€РµС‚ domain events, СЃС‚СЂРѕРёС‚ closed bars Рё pseudo-orders Р±РµР· СЂРµР°Р»СЊРЅС‹С… T-Bank broker calls.

## РћР±СЏР·Р°С‚РµР»СЊРЅС‹Рµ РѕРіСЂР°РЅРёС‡РµРЅРёСЏ

- РќРµ РєРѕРјРјРёС‚РёС‚СЊ СЂРµР°Р»СЊРЅС‹Рµ T-Bank С‚РѕРєРµРЅС‹.
- РќРµ С…СЂР°РЅРёС‚СЊ СЂРµР°Р»СЊРЅС‹Рµ СЃРµРєСЂРµС‚С‹ РІ `.env`.
- Production-like secrets С‡РёС‚Р°СЋС‚СЃСЏ С‚РѕР»СЊРєРѕ РёР· Docker Compose secrets.
- Р”Р»СЏ dev РґРѕРїСѓСЃРєР°СЋС‚СЃСЏ Р»РѕРєР°Р»СЊРЅС‹Рµ С„Р°Р№Р»С‹ РІ `secrets/`; РїР°РїРєР° `secrets/` РёРіРЅРѕСЂРёСЂСѓРµС‚СЃСЏ Git.

## РџРѕРґРіРѕС‚РѕРІРєР° РѕРєСЂСѓР¶РµРЅРёСЏ

РЎРєРѕРїРёСЂСѓР№С‚Рµ РЅРµСЃРµРєСЂРµС‚РЅС‹Рµ РїР°СЂР°РјРµС‚СЂС‹:

```powershell
Copy-Item .env.example .env
```

РЎРѕР·РґР°Р№С‚Рµ Р»РѕРєР°Р»СЊРЅС‹Рµ secret files:

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
Set-Content -NoNewline secrets/postgres_password "local_postgres_password"
Set-Content -NoNewline secrets/grafana_admin_password "local_grafana_password"
Set-Content -NoNewline secrets/tbank_full_access_token "paste_full_access_token_here"
Set-Content -NoNewline secrets/tbank_readonly_token "paste_readonly_token_here"
```

Р”Р»СЏ СЂРµР°Р»СЊРЅРѕРіРѕ С‚РѕРєРµРЅР° Р·Р°РјРµРЅРёС‚Рµ `paste_*_token_here` РІСЂСѓС‡РЅСѓСЋ. РќРµ РІСЃС‚Р°РІР»СЏР№С‚Рµ С‚РѕРєРµРЅС‹ РІ РґРѕРєСѓРјРµРЅС‚С‹, compose-С„Р°Р№Р»С‹ РёР»Рё `.env`.

T-Bank adapter РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ СЂР°Р±РѕС‚Р°РµС‚ РІ `sandbox` СЂРµР¶РёРјРµ:

```powershell
$env:TBANK_ENVIRONMENT = "sandbox"
$env:TBANK_APP_NAME = "Resurt.Trading_2_0"
$env:SSL_TBANK_VERIFY = "true"
$env:TBANK_UNARY_TIMEOUT_FLOOR_SECONDS = "5.0"
```

`SSL_TBANK_VERIFY=true` РІРєР»СЋС‡Р°РµС‚ РІСЃС‚СЂРѕРµРЅРЅС‹Р№ РІ РѕС„РёС†РёР°Р»СЊРЅС‹Р№ T-Bank SDK bundle
`RussianTrustedRootCA.pem`. Р­С‚Рѕ РЅСѓР¶РЅРѕ РґР»СЏ endpoints T-Invest СЃ С†РµРїРѕС‡РєРѕР№ РќРЈР¦
РњРёРЅС†РёС„СЂС‹ Р Р¤. Р•СЃР»Рё Windows/curl/Python РїРѕРєР°Р·С‹РІР°СЋС‚
`self signed certificate in certificate chain` РёР»Рё issuer
`CN=The original certificate provided by the server is untrusted`, РїСЂРѕРІРµСЂСЊС‚Рµ
ESET/HTTPS inspection Рё РґРѕРІРµСЂРёРµ Russian Trusted Root/Sub CA РІ РѕРєСЂСѓР¶РµРЅРёРё РїСЂРѕС†РµСЃСЃР°.

Р”Р»СЏ СЂРµР°Р»СЊРЅС‹С… sandbox/live readonly calls С‡РµСЂРµР· РѕС„РёС†РёР°Р»СЊРЅС‹Р№ SDK СѓСЃС‚Р°РЅРѕРІРёС‚Рµ optional extra:

```powershell
python -m pip install -e ".[tbank]" --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

## Р—Р°РїСѓСЃРє СЃС‚РµРєР°

```powershell
docker compose up -d --build
```

РђР»СЊС‚РµСЂРЅР°С‚РёРІР°, РµСЃР»Рё СѓСЃС‚Р°РЅРѕРІР»РµРЅ `make`:

```powershell
make up
```

## РџСЂРѕРІРµСЂРєР° health

```powershell
docker compose ps
Invoke-WebRequest http://localhost:8000/health
Invoke-WebRequest http://localhost:8001/health
Invoke-WebRequest http://localhost:8002/health
Invoke-WebRequest http://localhost:5173/health
Invoke-WebRequest http://localhost:9090/-/healthy
Invoke-WebRequest http://localhost:3000/api/health
Invoke-WebRequest http://localhost:3100/ready
```

## РџСЂРѕРІРµСЂРєР° trade-core runtime

Р›РѕРєР°Р»СЊРЅРѕ Р±РµР· С‚РѕРєРµРЅРѕРІ РёСЃРїРѕР»СЊР·СѓР№С‚Рµ С‚РѕР»СЊРєРѕ Р±РµР·РѕРїР°СЃРЅС‹Р№ СЂРµР¶РёРј:

```powershell
$env:TRADING_RUNTIME_MODE = "historical_replay"
$env:TRADE_CORE_TICK_INTERVAL_SECONDS = "1"
python -m trade_core.service
```

РћР¶РёРґР°РµРјС‹Рµ endpoints:

```powershell
Invoke-WebRequest http://localhost:8001/health
Invoke-WebRequest http://localhost:8001/metrics
```

Р’ `historical_replay` runtime:

- РЅРµ С‚СЂРµР±СѓРµС‚ T-Bank С‚РѕРєРµРЅС‹;
- РЅРµ РІС‹Р·С‹РІР°РµС‚ `BrokerGateway.post_order`;
- РѕС‚РєСЂС‹РІР°РµС‚ logical hourly micro-sessions Р±РµР· СЂРµСЃС‚Р°СЂС‚Р° РїСЂРѕС†РµСЃСЃР°;
- РїРёС€РµС‚ `signal_candidate`, `candidate_stage_result`, `order_intent`, `broker_order` РєР°Рє domain facts;
- РЅР° shutdown РїРёС€РµС‚ `audit_event`.

Р•СЃР»Рё `TRADING_DATABASE_URL`/`DATABASE_URL` РЅРµ Р·Р°РґР°РЅ, runtime РЅРµ РґРѕР»Р¶РµРЅ РјРѕР»С‡Р° СѓС…РѕРґРёС‚СЊ РІ SQLite. Р”Р»СЏ РѕРґРЅРѕ-РїСЂРѕС†РµСЃСЃРЅРѕРіРѕ Р»РѕРєР°Р»СЊРЅРѕРіРѕ СЌРєСЃРїРµСЂРёРјРµРЅС‚Р° Р±РµР· Postgres РІС‹СЃС‚Р°РІСЊС‚Рµ СЏРІРЅС‹Р№ С„Р»Р°Рі:

```powershell
$env:TRADING_RUNTIME_LOCAL_SQLITE = "1"
```

Р’ Docker Compose СЌС‚РѕС‚ С„Р»Р°Рі РЅРµ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ: `trade-core`, `api` Рё `report-worker` РґРѕР»Р¶РЅС‹ СЃРјРѕС‚СЂРµС‚СЊ РІ РѕРґРёРЅ PostgreSQL С‡РµСЂРµР· `POSTGRES_HOST=postgres`, `POSTGRES_DB`, `POSTGRES_USER` Рё `POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password`.

## FastAPI BFF

OpenAPI РґРѕСЃС‚СѓРїРµРЅ Р»РѕРєР°Р»СЊРЅРѕ:

```powershell
Invoke-WebRequest http://localhost:8000/openapi.json
```

Swagger UI:

```text
http://localhost:8000/docs
```

Р§С‚РµРЅРёРµ СЃРѕСЃС‚РѕСЏРЅРёСЏ РЅРµ С‚СЂРµР±СѓРµС‚ СЂРѕР»Рё РІС‹С€Рµ `observer`:

```powershell
Invoke-RestMethod http://localhost:8000/robot/status
Invoke-RestMethod http://localhost:8000/session/current
Invoke-RestMethod http://localhost:8000/market/overview
```

РљРѕРјР°РЅРґС‹ СѓРїСЂР°РІР»РµРЅРёСЏ Рё СЂСѓС‡РЅРѕР№ Р·Р°РїСѓСЃРє daily report РІ local-dev РёСЃРїРѕР»СЊР·СѓСЋС‚ dev auth
headers. Р’ production СЌС‚РѕС‚ РїСѓС‚СЊ Р·Р°РїСЂРµС‰РµРЅ, С‚Р°Рј РЅСѓР¶РµРЅ `TRADING_AUTH_MODE=static_bearer`.

```powershell
Invoke-RestMethod -Method Post -Headers @{ "X-API-Role" = "operator" } http://localhost:8000/robot/start
Invoke-RestMethod -Method Post -Headers @{ "X-API-Role" = "operator" } http://localhost:8000/robot/stop
Invoke-RestMethod -Method Post -Headers @{ "X-API-Role" = "operator" } `
  -ContentType "application/json" `
  -Body '{"trading_date":"2026-06-13","strategy_id":"baseline","include_counterfactual":true}' `
  http://localhost:8000/reports/daily/run
```

Р­С‚Рё РєРѕРјР°РЅРґС‹ СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ РІ `robot_command`, Р° `trade-core` РїСЂРёРјРµРЅСЏРµС‚ РёС… РІ runtime loop.
РџСЂРѕРІРµСЂРёС‚СЊ СЃС‚Р°С‚СѓСЃ durable РєРѕРјР°РЅРґС‹ РјРѕР¶РЅРѕ С‡РµСЂРµР· `/robot/status` Рё РїСЂСЏРјСѓСЋ РґРёР°РіРЅРѕСЃС‚РёРєСѓ Р‘Р”
РІ local-dev.

WebSocket РєР°РЅР°Р»С‹:

- `ws://localhost:8000/ws/dashboard`
- `ws://localhost:8000/ws/orders`
- `ws://localhost:8000/ws/market-feed` - primary Dashboard Live Feed
- `ws://localhost:8000/ws/market` - compatibility alias for the same market feed
- `ws://localhost:8000/ws/reports`

Р”Р»СЏ Vite frontend API РґРѕР»Р¶РµРЅ СЂР°Р·СЂРµС€Р°С‚СЊ Р»РѕРєР°Р»СЊРЅС‹Р№ origin:

```powershell
$env:CORS_ALLOW_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
```

## Frontend

Р›РѕРєР°Р»СЊРЅС‹Р№ Р·Р°РїСѓСЃРє:

```powershell
cd apps/frontend
npm.cmd run dev
```

РџСЂРѕРІРµСЂРєРё:

```powershell
cd apps/frontend
npm.cmd run typecheck
npm.cmd run test:unit
npm.cmd run build
```

## РњРёРіСЂР°С†РёРё PostgreSQL

РџРѕСЃР»Рµ Р·Р°РїСѓСЃРєР° `postgres` РїСЂРёРјРµРЅРёС‚Рµ СЃС…РµРјСѓ:

```powershell
python -m alembic upgrade head
python -m alembic current
```

РџСЂРѕРІРµСЂРєР° РѕР±СЂР°С‚РёРјРѕСЃС‚Рё РїРѕСЃР»РµРґРЅРµР№ РјРёРіСЂР°С†РёРё:

```powershell
python -m alembic downgrade -1
python -m alembic upgrade head
```

## Р›РѕРєР°Р»СЊРЅС‹Рµ Р°РґСЂРµСЃР°

- Frontend: `http://localhost:5173`
- API: `http://localhost:8000`
- trade-core: `http://localhost:8001`
- report-worker: `http://localhost:8002`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`
- Loki: `http://localhost:3100`
- Fluent Bit HTTP: `http://localhost:2020`
- Fluent Bit forward input: `localhost:24224`

## Р›РѕРіРё

```powershell
docker compose logs -f --tail=200
```

Docker services РёСЃРїРѕР»СЊР·СѓСЋС‚ `fluentd` logging driver СЃ async-РґРѕСЃС‚Р°РІРєРѕР№ РЅР° Fluent Bit `forward` input (`localhost:24224`). Fluent Bit РѕС‚РїСЂР°РІР»СЏРµС‚ stdout/stderr logs РІ Loki.

## РћСЃС‚Р°РЅРѕРІРєР°

```powershell
docker compose down
```

РЎ СѓРґР°Р»РµРЅРёРµРј volume-РґР°РЅРЅС‹С…:

```powershell
docker compose down -v
```

## Р›РѕРєР°Р»СЊРЅС‹Рµ РїСЂРѕРІРµСЂРєРё Р±РµР· Docker

```powershell
python scripts/check.py
python scripts/run_replay_harness.py
python scripts/run_sandbox_smoke.py --dry-run
python scripts/run_historical_candle_backfill.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --lookback-days 90 --dry-run
```

РџРѕР»РЅС‹Р№ Р»РѕРєР°Р»СЊРЅС‹Р№ controlled-launch gate Р±РµР· СЂРµР°Р»СЊРЅС‹С… broker orders:

```powershell
python scripts/run_controlled_launch_acceptance.py
```

Р•СЃР»Рё `python scripts/check.py` СѓР¶Рµ РІС‹РїРѕР»РЅРµРЅ РѕС‚РґРµР»СЊРЅРѕ, РјРѕР¶РЅРѕ СѓСЃРєРѕСЂРёС‚СЊ РїСЂРёРµРјРєСѓ:

```powershell
python scripts/run_controlled_launch_acceptance.py --skip-full-check
```

Р­С‚РѕС‚ СЃРєСЂРёРїС‚ Р·Р°РїСѓСЃРєР°РµС‚ analytics acceptance, report rebuild, replay-day, `docker compose config`,
SQLite migration `upgrade -> downgrade -1 -> upgrade`, sandbox dry-run, production safety guards
Рё secret scan. РџСЂСЏРјРѕР№ `python -m alembic upgrade head` РёСЃРїРѕР»СЊР·СѓРµС‚ PostgreSQL РёР· `alembic.ini`/env
Рё РґРѕР»Р¶РµРЅ Р·Р°РїСѓСЃРєР°С‚СЊСЃСЏ С‚РѕР»СЊРєРѕ РїРѕСЃР»Рµ РїРѕРґРЅСЏС‚РѕРіРѕ `postgres` РёР»Рё СЃ СЏРІРЅРѕ Р·Р°РґР°РЅРЅС‹Рј `DATABASE_URL`.

Р Р°СЃС€РёСЂРµРЅРЅС‹Р№ launch-readiness gate:

```powershell
python scripts/run_launch_readiness.py --mode local
python scripts/run_launch_readiness.py --mode compose
python scripts/run_launch_readiness.py --mode sandbox
python scripts/run_launch_readiness.py --mode shadow
python scripts/run_launch_readiness.py --mode production-preflight
```

`compose` mode РїСЂРѕРІРµСЂСЏРµС‚ РѕР±С‰РёР№ PostgreSQL config, health endpoints, Celery/report worker smoke Рё frontend build. `production-preflight` РїР°РґР°РµС‚ Р±РµР· `TRADING_PRODUCTION_CONFIRM`, production auth token, РѕС‚РєР»СЋС‡РµРЅРёСЏ dev auth, resolved instrument ids Рё shared Postgres.

РќР° Windows, РµСЃР»Рё PowerShell Р±Р»РѕРєРёСЂСѓРµС‚ `npm.ps1`, РёСЃРїРѕР»СЊР·СѓР№С‚Рµ `npm.cmd` РЅР°РїСЂСЏРјСѓСЋ РёР· `apps/frontend`.

## Controlled launch modes

Р‘РµР·РѕРїР°СЃРЅС‹Р№ local default:

```powershell
$env:TRADING_RUNTIME_MODE = "historical_replay"
```

Sandbox dry-run:

```powershell
$env:TRADING_RUNTIME_MODE = "sandbox"
python scripts/run_sandbox_smoke.py --dry-run
```

Shadow mode Р»РѕРєР°Р»СЊРЅРѕ РґРѕР»Р¶РµРЅ РїРёСЃР°С‚СЊ pseudo-orders Рё РЅРµ РІС‹Р·С‹РІР°С‚СЊ СЂРµР°Р»СЊРЅС‹Р№ `PostOrder`:

```powershell
$env:TRADING_RUNTIME_MODE = "shadow"
docker compose up -d --build trade-core api report-worker report-worker-health frontend
```

Production РЅРµ РІРєР»СЋС‡Р°С‚СЊ Р»РѕРєР°Р»СЊРЅРѕ Р±РµР· РѕС‚РґРµР»СЊРЅРѕРіРѕ checklist. Р”Р»СЏ production С‚СЂРµР±СѓРµС‚СЃСЏ `TRADING_PRODUCTION_CONFIRM=I_UNDERSTAND_LIVE_ORDERS`.

## Report Worker

Р’ Docker Compose СЂРѕР»Рё СЂР°Р·РґРµР»РµРЅС‹:

- `report-worker` Р·Р°РїСѓСЃРєР°РµС‚ Celery worker РѕС‡РµСЂРµРґРё `reports`;
- `report-worker-health` РѕС‚РґР°РµС‚ HTTP `/health` Рё `/metrics` РЅР° `http://localhost:8002`;
- С‚СЏР¶РµР»С‹Рµ hourly/daily/counterfactual РѕС‚С‡РµС‚С‹ РЅРµ РІС‹РїРѕР»РЅСЏСЋС‚СЃСЏ РІ FastAPI process.

Р—Р°РїСѓСЃРє С‚РѕР»СЊРєРѕ РєРѕРЅС‚СѓСЂР° РѕС‚С‡РµС‚РѕРІ:

```powershell
docker compose up -d --build redis postgres report-worker report-worker-health
Invoke-WebRequest http://localhost:8002/health
```

РџСЂРѕРІРµСЂРєР° СЃРїРѕСЃРѕР±РЅРѕСЃС‚Рё worker РїСЂРёРЅРёРјР°С‚СЊ Р·Р°РґР°С‡Рё:

```powershell
make celery-inspect
make report-worker-smoke
```

Р­РєРІРёРІР°Р»РµРЅС‚РЅР°СЏ Celery РєРѕРјР°РЅРґР° РІРЅСѓС‚СЂРё РєРѕРЅС‚РµР№РЅРµСЂР°:

```powershell
docker compose exec -T report-worker celery -A report_worker.celery_app.celery_app inspect ping
```

Р›РѕРєР°Р»СЊРЅС‹Р№ Р·Р°РїСѓСЃРє worker Р±РµР· Docker, РµСЃР»Рё Redis РґРѕСЃС‚СѓРїРµРЅ РЅР° `localhost:6379`:

```powershell
$env:CELERY_BROKER_URL = "redis://localhost:6379/0"
$env:CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
$env:CELERY_DEFAULT_QUEUE = "reports"
$env:CELERY_REPORTS_QUEUE = "reports"
celery -A report_worker.celery_app.celery_app worker --loglevel=INFO --queues=reports
```

Р СѓС‡РЅРѕР№ Р·Р°РїСѓСЃРє РѕС‚С‡РµС‚РѕРІ Р±РµР· FastAPI:

```powershell
python tools/reports/build_hourly_report.py --date 2026-06-12 --strategy-id baseline --force-rebuild
python tools/reports/build_daily_report.py --date 2026-06-12 --strategy-id baseline --force-rebuild
python tools/reports/run_counterfactual_analysis.py --date 2026-06-12 --strategy-id baseline --force-rebuild
```

Р¤РёР»СЊС‚СЂС‹ CLI: `--instrument`, `--timeframe`, `--session-type`,
`--strategy-version`, `--force-rebuild`. HTML preview РјРѕР¶РЅРѕ РїРѕР»СѓС‡РёС‚СЊ С‡РµСЂРµР·
`--output-format html` РёР»Рё РІРјРµСЃС‚Рµ СЃ JSON С‡РµСЂРµР· `--output-format both`.

## Historical Candle Backfill

РџРµСЂРµРґ shadow/prod РєР°Р»РёР±СЂРѕРІРєРѕР№ РјРѕР¶РЅРѕ РЅР°РєРѕРїРёС‚СЊ raw candles Рё derived bars:

```powershell
$env:TRADING_BACKFILL_RUNTIME_MODE = "shadow"
$env:TBANK_ENVIRONMENT = "live"
$env:SSL_TBANK_VERIFY = "true"
python scripts/run_tbank_sdk_import_check.py
python scripts/run_historical_candle_backfill.py `
  --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T `
  --from-date 2025-01-01 `
  --to-date 2026-06-18 `
  --raw-interval 1m `
  --derive 5m,10m,15m `
  --chunk-days 7 `
  --strategy-id baseline
```

РЎРєСЂРёРїС‚ РЅРµ РІС‹Р·С‹РІР°РµС‚ `PostOrder`/`CancelOrder`; РѕРЅ РёСЃРїРѕР»СЊР·СѓРµС‚ С‚РѕР»СЊРєРѕ readonly
`GetCandles` Рё РїРёС€РµС‚ `market_candle`. РџРѕРґСЂРѕР±РЅРѕСЃС‚Рё: `Docs/historical-candle-backfill.md`.

## Historical replay local workflow

РџРѕСЃР»Рµ backfill РјРѕР¶РЅРѕ РїСЂРѕРіРЅР°С‚СЊ РїРѕР»РЅС‹Р№ local historical РєРѕРЅС‚СѓСЂ Р±РµР· СЂРµР°Р»СЊРЅС‹С…
broker orders:

```powershell
make historical-quality LOOKBACK_DAYS=10
make historical-replay LOOKBACK_DAYS=10
make historical-counterfactual LOOKBACK_DAYS=10
make historical-report-rebuild LOOKBACK_DAYS=10
make calibration-report LOOKBACK_DAYS=10
python scripts/run_launch_readiness.py --mode historical-replay --dry-run
```

Р”Р»СЏ СЂРµР°Р»СЊРЅРѕР№ РїСЂРѕРІРµСЂРєРё РЅР° СѓР¶Рµ Р·Р°РіСЂСѓР¶РµРЅРЅС‹С… candles СѓР±РµСЂРёС‚Рµ `--dry-run` Сѓ
РєРѕРЅРєСЂРµС‚РЅС‹С… CLI-РєРѕРјР°РЅРґ. Р’СЃРµ СЂРµР·СѓР»СЊС‚Р°С‚С‹ РїРёС€СѓС‚СЃСЏ РІ PostgreSQL domain tables:
`historical_data_quality_report`, replay-generated candidate/order facts,
`counterfactual_result`, `hourly_report`, `daily_report` Рё
`calibration_report`.

# Local Development Runbook

## Назначение

Локальный запуск нужен для проверки каркаса сервисов, health endpoints, Prometheus/Grafana/Loki и Docker Compose wiring. На этом шаге реальная торговая бизнес-логика и T-Bank broker calls не включены.

## Обязательные ограничения

- Не коммитить реальные T-Bank токены.
- Не хранить реальные секреты в `.env`.
- Production-like secrets читаются только из Docker Compose secrets.
- Для dev допускаются локальные файлы в `secrets/`; папка `secrets/` игнорируется Git.

## Подготовка окружения

Скопируйте несекретные параметры:

```powershell
Copy-Item .env.example .env
```

Создайте локальные secret files:

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
Set-Content -NoNewline secrets/postgres_password "local_postgres_password"
Set-Content -NoNewline secrets/grafana_admin_password "local_grafana_password"
Set-Content -NoNewline secrets/tbank_full_access_token "paste_full_access_token_here"
Set-Content -NoNewline secrets/tbank_readonly_token "paste_readonly_token_here"
```

Для реального токена замените `paste_*_token_here` вручную. Не вставляйте токены в документы, compose-файлы или `.env`.

T-Bank adapter по умолчанию работает в `sandbox` режиме:

```powershell
$env:TBANK_ENVIRONMENT = "sandbox"
$env:TBANK_APP_NAME = "Resurt.Trading_2_0"
```

## Запуск стека

```powershell
docker compose up -d --build
```

Альтернатива, если установлен `make`:

```powershell
make up
```

## Проверка health

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

## FastAPI BFF

OpenAPI доступен локально:

```powershell
Invoke-WebRequest http://localhost:8000/openapi.json
```

Swagger UI:

```text
http://localhost:8000/docs
```

Чтение состояния не требует роли выше `observer`:

```powershell
Invoke-RestMethod http://localhost:8000/robot/status
Invoke-RestMethod http://localhost:8000/session/current
Invoke-RestMethod http://localhost:8000/market/overview
```

Команды управления и ручной запуск daily report требуют placeholder role header:

```powershell
Invoke-RestMethod -Method Post -Headers @{ "X-API-Role" = "operator" } http://localhost:8000/robot/start
Invoke-RestMethod -Method Post -Headers @{ "X-API-Role" = "operator" } http://localhost:8000/robot/stop
Invoke-RestMethod -Method Post -Headers @{ "X-API-Role" = "operator" } `
  -ContentType "application/json" `
  -Body '{"trading_date":"2026-06-13","strategy_id":"baseline","include_counterfactual":true}' `
  http://localhost:8000/reports/daily/run
```

WebSocket каналы:

- `ws://localhost:8000/ws/dashboard`
- `ws://localhost:8000/ws/orders`
- `ws://localhost:8000/ws/market`
- `ws://localhost:8000/ws/reports`

Для Vite frontend API должен разрешать локальный origin:

```powershell
$env:CORS_ALLOW_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
```

## Frontend

Локальный запуск:

```powershell
cd apps/frontend
npm.cmd run dev
```

Проверки:

```powershell
cd apps/frontend
npm.cmd run typecheck
npm.cmd run test:unit
npm.cmd run build
```

## Миграции PostgreSQL

После запуска `postgres` примените схему:

```powershell
python -m alembic upgrade head
python -m alembic current
```

Проверка обратимости последней миграции:

```powershell
python -m alembic downgrade -1
python -m alembic upgrade head
```

## Локальные адреса

- Frontend: `http://localhost:5173`
- API: `http://localhost:8000`
- trade-core: `http://localhost:8001`
- report-worker: `http://localhost:8002`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`
- Loki: `http://localhost:3100`
- Fluent Bit HTTP: `http://localhost:2020`
- Fluent Bit forward input: `localhost:24224`

## Логи

```powershell
docker compose logs -f --tail=200
```

Docker services используют `fluentd` logging driver с async-доставкой на Fluent Bit `forward` input (`localhost:24224`). Fluent Bit отправляет stdout/stderr logs в Loki.

## Остановка

```powershell
docker compose down
```

С удалением volume-данных:

```powershell
docker compose down -v
```

## Локальные проверки без Docker

```powershell
python scripts/check.py
python scripts/run_replay_harness.py
python scripts/run_sandbox_smoke.py --dry-run
```

На Windows, если PowerShell блокирует `npm.ps1`, используйте `npm.cmd` напрямую из `apps/frontend`.

## Controlled launch modes

Безопасный local default:

```powershell
$env:TRADING_RUNTIME_MODE = "historical_replay"
```

Sandbox dry-run:

```powershell
$env:TRADING_RUNTIME_MODE = "sandbox"
python scripts/run_sandbox_smoke.py --dry-run
```

Shadow mode локально должен писать pseudo-orders и не вызывать реальный `PostOrder`:

```powershell
$env:TRADING_RUNTIME_MODE = "shadow"
docker compose up -d --build trade-core api report-worker frontend
```

Production не включать локально без отдельного checklist. Для production требуется `TRADING_PRODUCTION_CONFIRM=I_UNDERSTAND_LIVE_ORDERS`.

## Report Worker

Celery tasks используют Redis:

```powershell
$env:CELERY_BROKER_URL = "redis://localhost:6379/0"
$env:CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
celery -A report_worker.tasks worker --loglevel=INFO
```

Ручной запуск отчетов без FastAPI:

```powershell
python scripts/run_hourly_report.py --micro-session-id 2026-06-12:weekday_main:1000 --strategy-id baseline
python scripts/run_daily_report.py --trading-date 2026-06-12 --strategy-id baseline
python scripts/run_counterfactual.py --trading-date 2026-06-12 --strategy-id baseline
```

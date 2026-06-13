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
```

На Windows, если PowerShell блокирует `npm.ps1`, используйте `npm.cmd` напрямую из `apps/frontend`.

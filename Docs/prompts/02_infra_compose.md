# Step 02 Prompt: Infrastructure Compose

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: собрать Docker Compose стек и базовые конфигурации инфраструктуры.

Сделай сервисы:

- postgres
- redis
- loki
- fluent-bit
- prometheus
- grafana
- trade-core
- api
- report-worker
- frontend

Требования:

- healthcheck для каждого сервиса;
- Docker Compose secrets для T-Bank токенов, Postgres password, Grafana admin password;
- Fluent Bit собирает stdout/stderr контейнеров и отправляет в Loki;
- Prometheus scrape для backend сервисов и инфраструктуры;
- Grafana provisioning для Prometheus и Loki;
- dev secrets только sample/fake, реальные ключи не коммитить;
- обновить `Docs/runbooks/local-dev.md`.

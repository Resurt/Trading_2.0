# Observability runbook

Этот runbook описывает локальный production-like контур:

```text
Python JSON logs -> stdout/stderr -> Docker fluentd logging driver -> Fluent Bit -> Loki
backend /metrics -> Prometheus -> Grafana dashboards + Prometheus alert rules
```

## Сервисы и конфиги

- `docker-compose.yml` поднимает `loki`, `fluent-bit`, `prometheus`, `grafana`,
  `trade-core`, `api`, `report-worker`.
- `deploy/fluent-bit/fluent-bit.conf` принимает stdout/stderr контейнеров через
  Fluent Forward на `:24224` и отправляет записи в Loki.
- `deploy/prometheus/prometheus.yml` scrape-ит:
  - `trade-core:8001/metrics`;
  - `api:8000/metrics`;
  - `report-worker:8002/metrics`.
- `deploy/prometheus/rules/trading-alerts.yml` содержит базовые alert rules.
- `deploy/grafana/provisioning/datasources/datasources.yml` добавляет Prometheus и Loki.
- `deploy/grafana/dashboards/observability-stack.json` содержит основной dashboard.

## Запуск

```bash
docker compose up -d --build
docker compose ps
```

Проверки:

```bash
curl http://localhost:8001/health
curl http://localhost:8001/metrics
curl http://localhost:8000/metrics
curl http://localhost:8002/metrics
curl http://localhost:9090/-/healthy
curl http://localhost:3100/ready
curl http://localhost:3000/api/health
```

Адреса:

- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`
- Loki ready: `http://localhost:3100/ready`
- Fluent Bit health: `http://localhost:2020/api/v1/health`

## Метрики

Обязательные latency/distribution histograms:

- `broker_post_order_latency_seconds`
- `order_state_convergence_seconds`
- `candle_close_delivery_lag_seconds`
- `session_rollover_duration_seconds`
- `report_generation_duration_seconds`

Counters:

- `stream_reconnect_total`
- `rejected_orders_total`
- `risk_events_total`
- `counterfactual_jobs_total`

Gauges:

- `market_stream_alive`
- `last_stream_message_age_seconds`
- `open_orders`
- `active_positions`
- `celery_queue_backlog`

Разрешенные Prometheus labels:

- `service`
- `instrument`
- `timeframe`
- `session_type`
- `stream_type`
- `status`
- `result`

Не добавлять в labels: `candidate_id`, `micro_session_id`, `order_intent_id`,
`request_order_id`, `exchange_order_id`, `tracking_id`, exception text и любые
произвольные free-text причины. Эти поля остаются в JSON logs и PostgreSQL.

## Loki labels

Fluent Bit выносит в Loki labels только низкокардинальные поля:

- `job`
- `environment`
- `container_name`
- `source`
- `service`
- `level`
- `event_type`
- `session_type`
- `exchange_phase`
- `instrument`
- `timeframe`

Correlation IDs остаются внутри JSON body и ищутся через parsing/query, а не через labels.

Примеры запросов в Grafana Explore:

```logql
{job="fluent-bit", service="trade-core"}
{job="fluent-bit", service="trade-core", level="ERROR"}
{job="fluent-bit"} | json | candidate_id="..."
{job="fluent-bit"} | json | tracking_id="..."
```

## Dashboard

`Trading 2.0 Observability Stack` должен показывать:

- health `trade-core`;
- broker/API latency;
- stream reconnects and lag;
- hourly rollover;
- report-worker queue and failures;
- blocker overview;
- rejected/canceled orders;
- top infrastructure incidents.

Если dashboard не появился:

1. Проверить volume `./deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro`.
2. Проверить provider `deploy/grafana/provisioning/dashboards/dashboards.yml`.
3. Перезапустить Grafana: `docker compose restart grafana`.

## Alert scenarios

`TradingServiceDown`  
Сервис сам сообщает non-ok health. Проверить `/health`, последние логи в Loki и
`docker compose ps`.

`TradingServiceMissingMetrics`  
Prometheus не видит один из backend metrics endpoints. Проверить target в Prometheus UI:
`Status -> Targets`, затем сетевую доступность `trade-core:8001/metrics`,
`api:8000/metrics` и `report-worker:8002/metrics`.

`BrokerPostOrderLatencyHigh`  
p95 `PostOrder` выше 2 секунд. Проверить broker transport, rate-limit headers,
сетевые ошибки и последние `broker_order_posted`/`broker_order_updated` события.

`MarketStreamDown` / `MarketStreamStale` / `StreamReconnectSpike`  
Проверить stream reconnect logs, gap recovery events, возраст последнего сообщения и
работу `TBankBrokerGateway` streaming layer.

`SessionRolloverSlow`  
Проверить snapshot на границе часа, запись `session_run`/`micro_session`,
публикацию `report_requested` и нагрузку Postgres.

`ReportGenerationFailures` / `CounterfactualJobFailures` / `CeleryQueueBacklogHigh`  
Проверить `report-worker`, Redis, DB migrations, блокировки Postgres и payload задач.
Тяжелые отчеты не запускать вручную через FastAPI process.

`RejectedOrdersSpike`  
Проверить `status` label, broker reject reasons, session/phase permissions,
rate-limit pressure и актуальность instrument trading status.

`RiskEventsSpike`  
Проверить `result` label, blocker taxonomy, market quality, spread, stale data и
session-aware order policy.

## Диагностика Fluent Bit -> Loki

```bash
docker compose logs -f fluent-bit
curl http://localhost:2020/api/v1/metrics/prometheus
```

В Loki:

```logql
{job="fluent-bit"}
```

Если логов нет:

1. Убедиться, что сервисы используют `logging: *fluent-bit-logging`.
2. Проверить, что Fluent Bit слушает `localhost:24224`.
3. Проверить ready endpoint Loki: `http://localhost:3100/ready`.
4. Проверить, что Python services пишут JSON logs в stdout.

## Диагностика Prometheus

Проверить targets:

```text
http://localhost:9090/targets
```

Быстрые PromQL:

```promql
trading_service_up
market_stream_alive
last_stream_message_age_seconds
celery_queue_backlog
histogram_quantile(0.95, sum(rate(broker_post_order_latency_seconds_bucket[5m])) by (le, service))
```

Если metric name отсутствует, проверить код `TradingMetrics` и наличие `/metrics` у сервиса.

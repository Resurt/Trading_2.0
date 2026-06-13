# Step 08 Prompt: Logging, Metrics, Correlation

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: построить production-like систему логирования, метрик и корреляции событий.

Сделай:

- structured JSON logging на стандартном Python logging;
- контекст через `contextvars`, `LoggerAdapter` или filters;
- canonical log schema в `Docs/logging-analytics-spec.md`;
- technical logs -> stdout -> Fluent Bit -> Loki;
- domain events -> PostgreSQL;
- metrics -> Prometheus;
- required histograms/counters/gauges;
- тесты JSON shape и context propagation.

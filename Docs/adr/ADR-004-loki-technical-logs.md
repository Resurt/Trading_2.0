# ADR-004: Loki для technical logs

Status: Accepted

## Контекст

Технические логи высокообъемные: reconnects, stream health, API errors, latency, tracking id, rate limits, diagnostic events. Хранить весь поток таких логов в PostgreSQL не нужно и вредно для роли базы как source of truth.

## Решение

Технические JSON logs пишутся в stdout/stderr контейнеров, собираются Fluent Bit и отправляются в Loki. Grafana используется для просмотра и расследования.

## Последствия

- PostgreSQL не используется как raw technical log sink.
- Loki не является источником истины для калибровки стратегии.
- Для связи Loki и PostgreSQL обязательны correlation ids: `run_id`, `micro_session_id`, `candidate_id`, `order_intent_id`, `request_order_id`, `exchange_order_id`.
- Технические логи должны быть structured JSON, а не произвольный текст.

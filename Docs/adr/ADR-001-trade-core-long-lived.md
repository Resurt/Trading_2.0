# ADR-001: long-lived `trade-core`

Status: Accepted

## Контекст

`trade-core` держит market streams, strategy state, risk state, order lifecycle и reconciliation state. Если физически перезапускать контейнер каждый час, система сама будет создавать разрывы потоков, race conditions и грязные аналитические данные.

## Решение

`trade-core` - долгоживущий Python asyncio процесс в долгоживущем контейнере. Часовые и биржевые границы обрабатываются как доменные события внутри процесса.

## Последствия

- Hourly rollover становится детерминированным и наблюдаемым.
- Market streams не обрываются намеренно каждый час.
- Snapshot и `session_run_closed` становятся обязательными.
- Операционный рестарт допустим только как controlled maintenance или incident action, но не как штатная hourly-механика.

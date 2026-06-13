# ADR-003: PostgreSQL как source of truth

Status: Accepted

## Контекст

Проекту нужна база, из которой можно восстановить состояние, ордера, session runs, strategy events, risk events, reports, audit и counterfactual analytics. Сырые технические логи для этого не подходят: они удобны для диагностики, но не являются надежной доменной моделью.

## Решение

PostgreSQL является source of truth для доменных данных:

- `instrument_registry`;
- `strategy_config`;
- `session_run`;
- `signal_candidate`;
- `blocker_event`;
- `order_intent`;
- `broker_order`;
- `fill_event`;
- `risk_event`;
- `position_snapshot`;
- `strategy_state_event`;
- `hourly_report`;
- `daily_report`;
- `counterfactual_result`;
- `audit_event`.

## Последствия

- Все важные торговые решения должны фиксироваться как структурированные domain events.
- Отчеты и калибровка строятся из PostgreSQL, а не из raw logs.
- Event-heavy таблицы проектируются с учетом partitioning.
- Каждое событие должно иметь session/date context для разделения morning/main/evening/weekend.

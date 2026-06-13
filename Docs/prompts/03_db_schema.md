# Step 03 Prompt: Database Schema

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: спроектировать и реализовать схему данных PostgreSQL, миграции и слой доступа.

Сделай:

- SQLAlchemy 2.x;
- Alembic;
- модели и миграции для таблиц из `Docs/architecture.md`;
- partitioning plan для event-heavy таблиц;
- поля `calendar_date`, `trading_date`, `session_type`, `session_phase`, `micro_session_id`, `broker_trading_status`;
- тесты миграций и базовых repository методов.

Не используй PostgreSQL как raw technical log sink.

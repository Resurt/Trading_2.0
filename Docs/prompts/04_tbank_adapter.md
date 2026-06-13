# Step 04 Prompt: T-Bank Broker Adapter

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: реализовать брокерский адаптер к T-Invest API и безопасную загрузку секретов.

Сделай:

- интерфейс `BrokerGateway`;
- реализацию `TBankBrokerGateway`;
- разделение unary methods, streaming methods, auth/secrets, retry/backoff, deadlines, reconciliation helpers;
- чтение токенов из `/run/secrets/*` с dev fallback;
- методы `TradingSchedules`, `GetTradingStatus`, `GetCandles`, `GetLastPrices`, `GetOrderBook`, `PostOrder`, `CancelOrder`, `GetOrderState`, `GetOrders`, `PostStopOrder`;
- idempotent `request_order_id`;
- тесты на secret loading, idempotency, retry/deadline behavior.

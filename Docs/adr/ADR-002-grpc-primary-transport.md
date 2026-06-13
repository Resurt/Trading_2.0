# ADR-002: T-Bank gRPC как primary transport

Status: Accepted

## Контекст

T-Invest/T-Bank API описывает gRPC как основной протокол для торговых методов и streaming-сервисов. JSON WebSocket существует отдельно и полезен для streaming сценариев, но он не должен размывать границу торгового брокерского адаптера.

## Решение

`TBankBrokerGateway` использует T-Bank gRPC как primary broker transport. WebSocket разрешен для live feed и BFF, а также для отдельных streaming сценариев, если они явно обоснованы.

## Последствия

- Unary trading methods и streaming methods разделяются в `BrokerGateway`.
- `PostOrder`, `CancelOrder`, `GetOrderState`, `GetOrders`, `TradingSchedules` и reconciliation остаются за broker adapter.
- Frontend WebSocket contracts остаются BFF-контрактами, а не прямыми broker contracts.

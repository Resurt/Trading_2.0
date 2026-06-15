# Broker Gateway и T-Bank адаптер

Документ фиксирует границу между `trade-core` и T-Invest API для шага 04. Стратегия, risk engine и execution engine не должны зависеть от SDK-специфичных типов.

## Public API

Публичный интерфейс находится в `apps/trade-core/src/trade_core/broker_gateway.py`.

`BrokerGateway` предоставляет SDK-neutral методы:

- `trading_schedules`
- `get_trading_status`
- `get_candles`
- `get_last_prices`
- `get_order_book`
- `post_order`
- `cancel_order`
- `get_order_state`
- `get_orders`
- `post_stop_order`
- `reconcile_order_state`
- `reconcile_open_orders`
- `stream_market_data`
- `stream_orders`
- `recover_after_stream_gap`

Все запросы используют `InstrumentRef` с `instrument_id` / `instrument_uid`. `figi` не должен расползаться по стратегии и верхним слоям.

## Структура `infra/tbank`

```text
apps/trade-core/src/trade_core/infra/tbank/
  __init__.py
  config.py
  deadlines.py
  errors.py
  gateway.py
  headers.py
  idempotency.py
  protocols.py
  retry.py
  secrets.py
  sdk_clients.py
  streams.py
```

## Секреты

Порядок загрузки:

1. Docker Compose secrets:
   - `/run/secrets/tbank_full_access_token`
   - `/run/secrets/tbank_readonly_token`
2. Dev fallback env:
   - `TBANK_FULL_ACCESS_TOKEN`
   - `TBANK_READONLY_TOKEN`
3. Legacy local fallback:
   - `TINVEST_TOKEN`

Токены нельзя логировать, писать в `.env` или коммитить.

## Live / sandbox

`TBankBrokerConfig` поддерживает:

- `live`: `invest-public-api.tbank.ru:443`
- `sandbox`: `sandbox-invest-public-api.tbank.ru:443`

Для dev по умолчанию используется `sandbox`.

## Deadlines

Per-method deadlines зафиксированы по официальной таблице T-Invest:

| Метод | Deadline |
| --- | --- |
| `TradingSchedules` | 300 ms |
| `GetTradingStatus` | 500 ms |
| `GetCandles` | 500 ms |
| `GetLastPrices` | 500 ms |
| `GetOrderBook` | 500 ms |
| `PostOrder` | 1500 ms |
| `CancelOrder` | 1500 ms |
| `GetOrderState` | 300 ms |
| `GetOrders` | 500 ms |
| `PostStopOrder` | 1500 ms |

Источник: `https://developer.tbank.ru/invest/intro/developer/deadlines`.

## Headers

Адаптер захватывает и логирует служебные заголовки:

- `x-tracking-id`
- `x-app-name`
- `x-ratelimit-limit`
- `x-ratelimit-remaining`
- `x-ratelimit-reset`
- `message`

Источник по gRPC headers: `https://developer.tbank.ru/invest/intro/developer/protocols/grpc`.

## Retry и errors

`retry_async` повторяет только retryable ошибки:

- `UNAVAILABLE`
- `DEADLINE_EXCEEDED`
- `INTERNAL`
- `RESOURCE_EXHAUSTED`

Ошибки мапятся в SDK-neutral `BrokerGatewayError` с `reason_code`, чтобы далее связать их с `reject_reason_code`, `cancel_reason_code` и audit/domain events.

## Idempotency

`post_order` и `post_stop_order` генерируют `request_order_id` как UUID до вызова брокера. Если передан `client_order_key`, adapter хранит mapping `client_order_key -> request_order_id`, чтобы retry или повторный вызов использовал тот же UUID.

На следующем шаге execution/order lifecycle этот mapping должен быть связан с PostgreSQL `order_intent.request_order_id`.

## Streams

Для stream-соединений заложены:

- `PingMonitor`;
- reconnect with exponential backoff;
- `recover_after_stream_gap` hook;
- recovery через unary helpers после reconnect.

Источник по stream рекомендациям: `https://developer.tbank.ru/invest/intro/developer/stream`.

## Official T-Bank SDK wrapper

Реальный транспорт реализован внутри `infra/tbank/sdk_clients.py` и не протекает выше
`infra/tbank`:

- `TBankSdkUnaryClient` вызывает официальный Python SDK `t_tech.invest` для unary methods;
- `TBankSdkStreamClient` открывает market data stream и `OrderStateStream`;
- `TBankBrokerGateway` по умолчанию создает эти clients, если тест или replay не передал fake client;
- наружу возвращаются только SDK-neutral `dict` payloads и `StreamEvent`, без protobuf/SDK типов.

SDK подключен как optional dependency `tbank`, потому что пакет распространяется через T-Bank
package index и не должен ломать обычный CI без доступа к этому index:

```powershell
python -m pip install -e ".[tbank]" --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

Sandbox/live endpoint выбирается только через `LaunchModePolicy` и
`TBankBrokerConfig.from_launch_policy()`. Strategy/risk/execution слои не должны выбирать target
самостоятельно.

Высокоуровневый SDK стабильно отдает `x-tracking-id` через `response_metadata`.
Rate-limit headers сохраняются, когда SDK/gRPC metadata предоставляет их в ответе или исключении.
Raw token и Authorization metadata не логируются.

Для candle stream wrapper выставляет `waiting_close=True`; closed candles остаются primary input
для `BarEngine` и strategy candidates. Anonymous market trades используются только как market tape
context. Собственные fills идут через `OrderStateStream` и reconciliation helpers, а не через
deprecated user trades stream.

После reconnect `StreamSupervisor` вызывает `TBankBrokerGateway.recover_after_stream_gap()`:

- для market streams выполняется recent `GetCandles` backfill по `TBANK_STREAM_INSTRUMENT_IDS`
  и `TBANK_GAP_RECOVERY_TIMEFRAMES`;
- для `OrderStateStream` выполняется `GetOrders` по account id;
- для известных idempotency mappings выполняется `GetOrderState` по `request_order_id`.

Recovery best-effort: ошибка backfill/refresh логируется как `stream_gap_recovery_failed`, но не
должна останавливать reconnect loop.

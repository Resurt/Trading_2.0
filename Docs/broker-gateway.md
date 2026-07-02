# Broker Gateway Рё T-Bank Р°РґР°РїС‚РµСЂ

Р”РѕРєСѓРјРµРЅС‚ С„РёРєСЃРёСЂСѓРµС‚ РіСЂР°РЅРёС†Сѓ РјРµР¶РґСѓ `trade-core` Рё T-Invest API РґР»СЏ С€Р°РіР° 04. РЎС‚СЂР°С‚РµРіРёСЏ, risk engine Рё execution engine РЅРµ РґРѕР»Р¶РЅС‹ Р·Р°РІРёСЃРµС‚СЊ РѕС‚ SDK-СЃРїРµС†РёС„РёС‡РЅС‹С… С‚РёРїРѕРІ.

## Public API

РџСѓР±Р»РёС‡РЅС‹Р№ РёРЅС‚РµСЂС„РµР№СЃ РЅР°С…РѕРґРёС‚СЃСЏ РІ `apps/trade-core/src/trade_core/broker_gateway.py`.

`BrokerGateway` РїСЂРµРґРѕСЃС‚Р°РІР»СЏРµС‚ SDK-neutral РјРµС‚РѕРґС‹:

- `trading_schedules`
- `get_trading_status`
- `get_candles`
- `get_last_prices`
- `get_order_book`
- `post_order`
- `cancel_order`
- `get_order_state`
- `get_orders`
- `get_portfolio`
- `get_positions`
- `get_accounts`
- `get_dividends`
- `resolve_instruments`
- `post_stop_order`
- `reconcile_order_state`
- `reconcile_open_orders`
- `stream_market_data`
- `stream_orders`
- `recover_after_stream_gap`

Р’СЃРµ Р·Р°РїСЂРѕСЃС‹ РёСЃРїРѕР»СЊР·СѓСЋС‚ `InstrumentRef` СЃ `instrument_id` / `instrument_uid`. `figi` РЅРµ РґРѕР»Р¶РµРЅ СЂР°СЃРїРѕР»Р·Р°С‚СЊСЃСЏ РїРѕ СЃС‚СЂР°С‚РµРіРёРё Рё РІРµСЂС…РЅРёРј СЃР»РѕСЏРј.

## РЎС‚СЂСѓРєС‚СѓСЂР° `infra/tbank`

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

## РЎРµРєСЂРµС‚С‹

РџРѕСЂСЏРґРѕРє Р·Р°РіСЂСѓР·РєРё:

1. Docker Compose secrets:
   - `/run/secrets/tbank_full_access_token`
   - `/run/secrets/tbank_readonly_token`
2. Dev fallback env:
   - `TBANK_FULL_ACCESS_TOKEN`
   - `TBANK_READONLY_TOKEN`
3. Legacy local fallback:
   - `TINVEST_TOKEN`

РўРѕРєРµРЅС‹ РЅРµР»СЊР·СЏ Р»РѕРіРёСЂРѕРІР°С‚СЊ, РїРёСЃР°С‚СЊ РІ `.env` РёР»Рё РєРѕРјРјРёС‚РёС‚СЊ.

## Live / sandbox

`TBankBrokerConfig` РїРѕРґРґРµСЂР¶РёРІР°РµС‚:

- `live`: `invest-public-api.tbank.ru:443`
- `sandbox`: `sandbox-invest-public-api.tbank.ru:443`

Р”Р»СЏ dev РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ `sandbox`.

## Deadlines

Per-method deadlines Р·Р°С„РёРєСЃРёСЂРѕРІР°РЅС‹ РїРѕ РѕС„РёС†РёР°Р»СЊРЅРѕР№ С‚Р°Р±Р»РёС†Рµ T-Invest:

| РњРµС‚РѕРґ | Deadline |
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
| `GetPortfolio` | 500 ms |
| `GetPositions` | 500 ms |
| `GetAccounts` | 500 ms |
| `GetDividends` | 500 ms |
| `ResolveInstruments` | 500 ms |
| `PostStopOrder` | 1500 ms |

РСЃС‚РѕС‡РЅРёРє: `https://developer.tbank.ru/invest/intro/developer/deadlines`.

## Headers

РђРґР°РїС‚РµСЂ Р·Р°С…РІР°С‚С‹РІР°РµС‚ Рё Р»РѕРіРёСЂСѓРµС‚ СЃР»СѓР¶РµР±РЅС‹Рµ Р·Р°РіРѕР»РѕРІРєРё:

- `x-tracking-id`
- `x-app-name`
- `x-ratelimit-limit`
- `x-ratelimit-remaining`
- `x-ratelimit-reset`
- `message`

РСЃС‚РѕС‡РЅРёРє РїРѕ gRPC headers: `https://developer.tbank.ru/invest/intro/developer/protocols/grpc`.

## Retry Рё errors

`retry_async` РїРѕРІС‚РѕСЂСЏРµС‚ С‚РѕР»СЊРєРѕ retryable РѕС€РёР±РєРё:

- `UNAVAILABLE`
- `DEADLINE_EXCEEDED`
- `INTERNAL`
- `RESOURCE_EXHAUSTED`

РћС€РёР±РєРё РјР°РїСЏС‚СЃСЏ РІ SDK-neutral `BrokerGatewayError` СЃ `reason_code`, С‡С‚РѕР±С‹ РґР°Р»РµРµ СЃРІСЏР·Р°С‚СЊ РёС… СЃ `reject_reason_code`, `cancel_reason_code` Рё audit/domain events.

## Idempotency

`post_order` Рё `post_stop_order` РіРµРЅРµСЂРёСЂСѓСЋС‚ `request_order_id` РєР°Рє UUID РґРѕ РІС‹Р·РѕРІР° Р±СЂРѕРєРµСЂР°. Р•СЃР»Рё РїРµСЂРµРґР°РЅ `client_order_key`, adapter С…СЂР°РЅРёС‚ mapping `client_order_key -> request_order_id`, С‡С‚РѕР±С‹ retry РёР»Рё РїРѕРІС‚РѕСЂРЅС‹Р№ РІС‹Р·РѕРІ РёСЃРїРѕР»СЊР·РѕРІР°Р» С‚РѕС‚ Р¶Рµ UUID.

РќР° СЃР»РµРґСѓСЋС‰РµРј С€Р°РіРµ execution/order lifecycle СЌС‚РѕС‚ mapping РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ СЃРІСЏР·Р°РЅ СЃ PostgreSQL `order_intent.request_order_id`.

## Streams

Р”Р»СЏ stream-СЃРѕРµРґРёРЅРµРЅРёР№ Р·Р°Р»РѕР¶РµРЅС‹:

- `PingMonitor`;
- reconnect with exponential backoff;
- `recover_after_stream_gap` hook;
- recovery С‡РµСЂРµР· unary helpers РїРѕСЃР»Рµ reconnect.

РСЃС‚РѕС‡РЅРёРє РїРѕ stream СЂРµРєРѕРјРµРЅРґР°С†РёСЏРј: `https://developer.tbank.ru/invest/intro/developer/stream`.

## Official T-Bank SDK wrapper

Р РµР°Р»СЊРЅС‹Р№ С‚СЂР°РЅСЃРїРѕСЂС‚ СЂРµР°Р»РёР·РѕРІР°РЅ РІРЅСѓС‚СЂРё `infra/tbank/sdk_clients.py` Рё РЅРµ РїСЂРѕС‚РµРєР°РµС‚ РІС‹С€Рµ
`infra/tbank`:

- `TBankSdkUnaryClient` РІС‹Р·С‹РІР°РµС‚ РѕС„РёС†РёР°Р»СЊРЅС‹Р№ Python SDK `t_tech.invest` РґР»СЏ unary methods;
- `TBankSdkStreamClient` РѕС‚РєСЂС‹РІР°РµС‚ market data stream Рё `OrderStateStream`;
- `TBankBrokerGateway` РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ СЃРѕР·РґР°РµС‚ СЌС‚Рё clients, РµСЃР»Рё С‚РµСЃС‚ РёР»Рё replay РЅРµ РїРµСЂРµРґР°Р» fake client;
- РЅР°СЂСѓР¶Сѓ РІРѕР·РІСЂР°С‰Р°СЋС‚СЃСЏ С‚РѕР»СЊРєРѕ SDK-neutral `dict` payloads Рё `StreamEvent`, Р±РµР· protobuf/SDK С‚РёРїРѕРІ.

SDK РїРѕРґРєР»СЋС‡РµРЅ РєР°Рє optional dependency `tbank`, РїРѕС‚РѕРјСѓ С‡С‚Рѕ РїР°РєРµС‚ СЂР°СЃРїСЂРѕСЃС‚СЂР°РЅСЏРµС‚СЃСЏ С‡РµСЂРµР· T-Bank
package index Рё РЅРµ РґРѕР»Р¶РµРЅ Р»РѕРјР°С‚СЊ РѕР±С‹С‡РЅС‹Р№ CI Р±РµР· РґРѕСЃС‚СѓРїР° Рє СЌС‚РѕРјСѓ index:

```powershell
python -m pip install -e ".[tbank]" --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

РџСЂРѕРІРµСЂРєР° РЅР°Р»РёС‡РёСЏ SDK extra:

```powershell
python scripts/run_tbank_sdk_import_check.py
```

Р’ sandbox/shadow/production startup `trade-core` РґРѕР»Р¶РµРЅ fail-fast, РµСЃР»Рё SDK extra РЅРµ СѓСЃС‚Р°РЅРѕРІР»РµРЅ.

Р”Р»СЏ T-Invest endpoints СЃ С†РµРїРѕС‡РєРѕР№ РќРЈР¦ РњРёРЅС†РёС„СЂС‹ Р Р¤ РѕС„РёС†РёР°Р»СЊРЅС‹Р№ SDK РїРѕРґРґРµСЂР¶РёРІР°РµС‚
РІСЃС‚СЂРѕРµРЅРЅС‹Р№ bundle `RussianTrustedRootCA.pem`. Р’ local/sandbox/shadow/production
РѕРєСЂСѓР¶РµРЅРёСЏС… РІРєР»СЋС‡Р°Р№С‚Рµ РїСЂРѕРІРµСЂРєСѓ С‚Р°Рє:

```powershell
$env:SSL_TBANK_VERIFY = "true"
```

Р’ Docker Compose СЌС‚Рѕ РІРєР»СЋС‡РµРЅРѕ РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ С‡РµСЂРµР· `SSL_TBANK_VERIFY=true`.
TLS verification РЅРµ РѕС‚РєР»СЋС‡Р°РµС‚СЃСЏ: РµСЃР»Рё РІ РѕРєСЂСѓР¶РµРЅРёРё СЃС‚РѕРёС‚ ESET/HTTPS inspection РёР»Рё
РґСЂСѓРіР°СЏ СЃРёСЃС‚РµРјР° РїРµСЂРµС…РІР°С‚Р°, issuer РІРёРґР°
`CN=The original certificate provided by the server is untrusted` РѕР·РЅР°С‡Р°РµС‚, С‡С‚Рѕ
РЅСѓР¶РЅРѕ РґРѕРІРµСЂРёС‚СЊ С†РµРїРѕС‡РєСѓ Russian Trusted Root/Sub CA РІ РѕРєСЂСѓР¶РµРЅРёРё РїСЂРѕС†РµСЃСЃР° РёР»Рё
РёСЃРєР»СЋС‡РёС‚СЊ T-Invest endpoints РёР· TLS-inspection policy.

РќР°С€ SDK factory РґРѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕ РІС‹СЃС‚Р°РІР»СЏРµС‚ `SSL_TBANK_VERIFY=true` РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РїРµСЂРµРґ СЃРѕР·РґР°РЅРёРµРј `t_tech.invest.Client`, С‡С‚РѕР±С‹ local sandbox smoke РёСЃРїРѕР»СЊР·РѕРІР°Р» bundled `RussianTrustedRootCA.pem` РґР°Р¶Рµ Р±РµР· `.env`. РЇРІРЅРѕРµ `SSL_TBANK_VERIFY=false` РѕСЃС‚Р°С‘С‚СЃСЏ РґРёР°РіРЅРѕСЃС‚РёС‡РµСЃРєРёРј override, РЅРѕ РЅРµ РґРѕР»Р¶РЅРѕ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊСЃСЏ РґР»СЏ sandbox/shadow/production readiness.

`TBankBrokerGateway` С…СЂР°РЅРёС‚ per-method deadline values, РЅРѕ concrete SDK client
СЃРѕР·РґР°РµС‚ gRPC channel РІРЅСѓС‚СЂРё unary call. Р§С‚РѕР±С‹ С…РѕР»РѕРґРЅС‹Р№ TLS/gRPC handshake РЅРµ
СѓР±РёРІР°Р» РїРµСЂРІС‹Р№ Р·Р°РїСЂРѕСЃ, С„Р°РєС‚РёС‡РµСЃРєРёР№ timeout РёРјРµРµС‚ floor:

```powershell
$env:TBANK_UNARY_TIMEOUT_FLOOR_SECONDS = "5.0"
```

Р—РЅР°С‡РµРЅРёРµ РјРѕР¶РЅРѕ СѓРјРµРЅСЊС€Р°С‚СЊ РїРѕСЃР»Рµ РїРµСЂРµС…РѕРґР° РЅР° persistent SDK channel РёР»Рё РїРѕСЃР»Рµ
РёР·РјРµСЂРµРЅРёР№ РІ СЃС‚Р°Р±РёР»СЊРЅРѕРј runtime.

Sandbox/live endpoint РІС‹Р±РёСЂР°РµС‚СЃСЏ С‚РѕР»СЊРєРѕ С‡РµСЂРµР· `LaunchModePolicy` Рё
`TBankBrokerConfig.from_launch_policy()`. Strategy/risk/execution СЃР»РѕРё РЅРµ РґРѕР»Р¶РЅС‹ РІС‹Р±РёСЂР°С‚СЊ target
СЃР°РјРѕСЃС‚РѕСЏС‚РµР»СЊРЅРѕ.

## Instrument resolver

`InstrumentResolverService` Р·Р°РіСЂСѓР¶Р°РµС‚ `TRADING_INSTRUMENTS` (`SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T` РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ), РІС‹Р·С‹РІР°РµС‚ `BrokerGateway.resolve_instruments`, РїРѕР»СѓС‡Р°РµС‚ СЂРµР°Р»СЊРЅС‹Рµ `instrument_uid`, `figi`, `class_code`, `ticker`, `lot_size`, `min_price_increment`, trade availability Рё short/weekend flags, Р·Р°С‚РµРј upsert-РёС‚ `instrument_registry`.

If broker resolution times out or is temporarily unavailable, shadow/runtime startup may use the already-resolved `instrument_registry` cache only when every requested ticker has enabled rows with real `instrument_uid` or `figi`. Incomplete cache remains fail-fast; placeholder ids and unresolved seed rows are still blocked.

РќР°С‡РёРЅР°СЏ СЃ sandbox/shadow/production, market streams, candles, order placement, positions Рё reports РґРѕР»Р¶РЅС‹ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РѕРґРёРЅ canonical `instrument_id`, СЂР°РІРЅС‹Р№ broker `instrument_uid`. Placeholder UID (`runtime-placeholder`) Р·Р°РїСЂРµС‰С‘РЅ launch-readiness gate.

Р’С‹СЃРѕРєРѕСѓСЂРѕРІРЅРµРІС‹Р№ SDK СЃС‚Р°Р±РёР»СЊРЅРѕ РѕС‚РґР°РµС‚ `x-tracking-id` С‡РµСЂРµР· `response_metadata`.
Rate-limit headers СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ, РєРѕРіРґР° SDK/gRPC metadata РїСЂРµРґРѕСЃС‚Р°РІР»СЏРµС‚ РёС… РІ РѕС‚РІРµС‚Рµ РёР»Рё РёСЃРєР»СЋС‡РµРЅРёРё.
Raw token Рё Authorization metadata РЅРµ Р»РѕРіРёСЂСѓСЋС‚СЃСЏ.

Р”Р»СЏ candle stream wrapper РІС‹СЃС‚Р°РІР»СЏРµС‚ `waiting_close=True`; closed candles РѕСЃС‚Р°СЋС‚СЃСЏ primary input
РґР»СЏ `BarEngine` Рё strategy candidates. Anonymous market trades РёСЃРїРѕР»СЊР·СѓСЋС‚СЃСЏ С‚РѕР»СЊРєРѕ РєР°Рє market tape
context. РЎРѕР±СЃС‚РІРµРЅРЅС‹Рµ fills РёРґСѓС‚ С‡РµСЂРµР· `OrderStateStream` Рё reconciliation helpers, Р° РЅРµ С‡РµСЂРµР·
deprecated user trades stream.

РџРѕСЃР»Рµ reconnect `StreamSupervisor` РІС‹Р·С‹РІР°РµС‚ `TBankBrokerGateway.recover_after_stream_gap()`:

- РґР»СЏ market streams РІС‹РїРѕР»РЅСЏРµС‚СЃСЏ recent `GetCandles` backfill РїРѕ `TBANK_STREAM_INSTRUMENT_IDS`
  Рё `TBANK_GAP_RECOVERY_TIMEFRAMES`;
- РґР»СЏ `OrderStateStream` РІС‹РїРѕР»РЅСЏРµС‚СЃСЏ `GetOrders` РїРѕ account id;
- РґР»СЏ РёР·РІРµСЃС‚РЅС‹С… idempotency mappings РІС‹РїРѕР»РЅСЏРµС‚СЃСЏ `GetOrderState` РїРѕ `request_order_id`.

Recovery best-effort: РѕС€РёР±РєР° backfill/refresh Р»РѕРіРёСЂСѓРµС‚СЃСЏ РєР°Рє `stream_gap_recovery_failed`, РЅРѕ РЅРµ
РґРѕР»Р¶РЅР° РѕСЃС‚Р°РЅР°РІР»РёРІР°С‚СЊ reconnect loop.

## Dividend calendar

`BrokerGateway.get_dividends(DividendsRequest)` СЏРІР»СЏРµС‚СЃСЏ primary source РґР»СЏ dividend
corporate actions. `TBankSdkUnaryClient` РІС‹Р·С‹РІР°РµС‚ T-Bank / T-Invest `GetDividends` С‡РµСЂРµР·
instruments service Рё РІРѕР·РІСЂР°С‰Р°РµС‚ SDK-neutral payload:

- `instrument_id`
- `declared_date`
- `record_date`
- `last_buy_date`
- `payment_date`
- `dividend_type`
- `amount_per_share`
- `currency`
- `close_price`
- `yield_value`
- `raw_payload`

SDK/protobuf-С‚РёРїС‹ РЅРµ РїРѕРґРЅРёРјР°СЋС‚СЃСЏ РІС‹С€Рµ `infra/tbank`. Р’СЃРµ РґР°С‚С‹ СЃРµСЂРёР°Р»РёР·СѓСЋС‚СЃСЏ РІ ISO, money /
quotation values СЃРµСЂРёР°Р»РёР·СѓСЋС‚СЃСЏ РєР°Рє Decimal-compatible strings, РѕС‚СЃСѓС‚СЃС‚РІСѓСЋС‰РёРµ РїРѕР»СЏ РѕСЃС‚Р°СЋС‚СЃСЏ
`null`. Raw token Рё Authorization metadata РЅРµ Р»РѕРіРёСЂСѓСЋС‚СЃСЏ.

`DividendSyncService` СЃРѕС…СЂР°РЅСЏРµС‚ СЃРѕР±С‹С‚РёСЏ РІ `corporate_action_event` СЃ
`source=api_import`, `confidence=confirmed`, `action_type=dividend`, Р·Р°С‚РµРј Р·Р°РїСѓСЃРєР°РµС‚
special-day classification. Manual CSV/JSON import РѕСЃС‚Р°С‘С‚СЃСЏ fallback/override Рё РЅРµ СЃС‡РёС‚Р°РµС‚СЃСЏ
clean primary calibration Р±РµР· СЏРІРЅРѕРіРѕ operator flag.

Contract coverage РґР»СЏ SDK wrapper РЅР°С…РѕРґРёС‚СЃСЏ РІ `tests/test_tbank_sdk_clients.py`: С‚РµСЃС‚С‹ РёСЃРїРѕР»СЊР·СѓСЋС‚
fake SDK Р±РµР· СЃРµС‚Рё Рё РїСЂРѕРІРµСЂСЏСЋС‚ unary payload shapes, stream subscription shapes, `waiting_close=True`,
UUID `request_order_id`, headers, order lifecycle Рё machine-readable error mapping.

## Portfolio / positions

Portfolio and position state is part of the same `BrokerGateway` boundary. `trade-core`
uses SDK-neutral `get_portfolio`, `get_positions` and `get_accounts` for account
validation, `position_snapshot` writes and pre-entry reconciliation.

The concrete SDK wrapper maps T-Bank `instrument_uid`, `figi` and ticker payloads
into plain dictionaries. Upper layers normalize those broker aliases back to the
project `instrument_id` through `InstrumentRef`; SDK/protobuf objects must not leak
into strategy, risk, execution or reporting code.

## Instrument identity guard

`instrument_id` is the internal canonical id, for example `MOEX:SBER`. It is used
for analytics, reports, session events and UI filters. It is not a T-Bank broker
identifier.

Real T-Bank calls must use `instrument_uid` or, when supported, `figi`. Seed rows
such as `MOEX:SBER` / `MOEX:GAZP` are local bootstrap records only. In
sandbox/shadow/production and real readonly scripts, `GetDividends`, `GetCandles`,
market streams and order placement fail fast if only an internal `MOEX:*` id is
available.

`instrument_registry` stores resolution state explicitly:

- `source=seed|tbank_resolved|manual|safe_noop`;
- `resolution_status=resolved|unresolved|failed`;
- `resolved_at`;
- `resolution_error_code` / `resolution_error_message`;
- `broker_payload`.

Before dividend sync, real historical backfill, shadow or production, run:

```powershell
python scripts/run_tbank_instrument_resolve.py --instruments SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T --strict --json-output
python scripts/run_launch_readiness.py --mode instrument-resolution
```

If T-Bank returns `NOT_FOUND` for `MOEX:SBER` / `MOEX:GAZP`, treat it as an
unresolved instrument-registry problem, not as a missing MOEX share.

## Session preflight and account balance

`TradingSessionPreflightService` uses BrokerGateway methods in readonly mode:

- `trading_schedules`
- `get_trading_status`

Broker `TradingSchedules` is authoritative when available. Saturday/Sunday trading days returned by the broker are classified as `session_type=weekend`. If broker schedules are unavailable, fallback time rules are used and marked as `source=fallback_time_rules` or `source=fallback_weekend_time_rules`.

Portfolio/account visibility uses readonly methods:

- `get_accounts`
- `get_portfolio`
- `get_positions`
- `get_last_prices` and `get_order_book` for explicit dashboard quote refresh

`scripts/run_broker_balance_refresh.py` and `POST /portfolio/refresh` use only those
readonly methods. They write a `broker_balance` payload into the portfolio read model
so `/portfolio/summary` and `/robot/status.balance` can show real account state even
when the market is closed.

Balance read models must mask account ids and must not log secrets or full account identifiers. If broker balance is unavailable, API returns `balance_degraded=true` with a reason code instead of hiding the Balance card.

Balance refresh must never call `PostOrder`, `CancelOrder`, `post_stop_order` or any
order-changing method. It is operator visibility only and does not imply permission
to trade in data-only mode.

The API container must mount the readonly T-Bank token when it serves dashboard
balance and quote refresh endpoints. `GET /market/overview` is local DB/read-model
only and must not call the broker. `POST /market/quotes/refresh` is the explicit
bounded readonly `GetLastPrices`/`GetOrderBook` path; if it fails, the frontend keeps
the last good quote and the local overview remains available.

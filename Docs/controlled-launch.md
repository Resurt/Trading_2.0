# Controlled Launch

Этот документ фиксирует правила доведения робота до controlled launch. Production не является режимом по умолчанию и не включается без явного подтверждения.

## Launch modes

| Mode | Market data | Execution | Broker target | Для чего |
| --- | --- | --- | --- | --- |
| `historical_replay` | исторические candles/events | pseudo-orders, без broker calls | `none` | детерминированная проверка rollovers, blockers, reports и counterfactual |
| `sandbox` | T-Bank sandbox | pseudo-orders по умолчанию; реальные sandbox orders только по явному подтверждению | `sandbox-invest-public-api.tbank.ru:443` | smoke broker adapter и инфраструктуры без реальных денег |
| `shadow` | live market data | pseudo-orders, без `PostOrder`/`CancelOrder` | live readonly stream/unary | проверка сигналов, risk gates, analytics spine и dashboard на реальном рынке |
| `production` | live market data | real broker orders | `invest-public-api.tbank.ru:443` | live trading после checklist |

`TRADING_RUNTIME_MODE` по умолчанию равен `historical_replay`.

Production требует:

```text
TRADING_RUNTIME_MODE=production
TRADING_PRODUCTION_CONFIRM=I_UNDERSTAND_LIVE_ORDERS
```

Без `TRADING_PRODUCTION_CONFIRM` сервис должен упасть на startup, а не стартовать в опасном полу-состоянии.

## Safety gates

- `LaunchModePolicy` находится в `trading_common.launch_modes`.
- `DefaultExecutionEngine` принимает `launch_policy`.
- В `historical_replay` и `shadow` создается `order_intent`, но `BrokerGateway.post_order()` не вызывается.
- Pseudo-order пишет `broker_order` со статусом `pseudo_posted`, `real_broker_call=false` и `reason_code`.
- Cancel в pseudo mode пишет `cancel_reason_code`, `cancel_payload` и не вызывает `BrokerGateway.cancel_order()`.
- `sandbox` разрешает broker calls только через sandbox config.
- `sandbox` не отправляет real sandbox orders по умолчанию:
  нужен `TRADING_SANDBOX_ORDERS_CONFIRM=I_UNDERSTAND_SANDBOX_ORDERS`.
- `production` разрешает broker calls только после явного подтверждения и production checklist.

## Control plane

Операторские команды проходят через durable control plane:

- API endpoint пишет `robot_command` со статусом `requested`;
- API пишет соответствующий `audit_event`;
- `trade-core` читает команды внутри long-lived runtime loop;
- команда переводится в `accepted`, затем `applied`, `rejected` или `failed`;
- `start`/`resume` разрешают runtime принимать новые entries;
- `pause`/`stop` запрещают новые entries без физического рестарта `trade-core`;
- `emergency_stop` переводит runtime в `emergency_stopped` и фиксирует
  `cancel_reason_code=manual_operator_emergency_stop`.

Auth policy:

- в local-dev допустим dev provider `X-API-Role`/`X-API-Actor`;
- в `production` dev auth запрещен на startup;
- production API должен стартовать только с `TRADING_AUTH_MODE=static_bearer`
  и токенами операторских ролей через env/secrets.

## Replay harness

`trade_core.replay.ReplayHarness` воспроизводит:

- closed candles через `BarEngine`;
- `SessionSnapshot` через `HourlyMicroSessionManager`;
- blocker events;
- cancelled-order events;
- counterfactual sources через подключаемый callback.

Локальная проверка:

```powershell
python scripts/run_replay_harness.py
```

Ожидаемые признаки:

- `session_rollover_verified=true`;
- `blocker_pipeline_verified=true`;
- `counterfactual_pipeline_verified=true`.

## Sandbox smoke

Sandbox smoke проверяет wiring, endpoint и secret policy. Dry-run не требует реального токена:

```powershell
python scripts/run_sandbox_smoke.py --dry-run
```

Если установлен optional SDK extra и в окружении есть sandbox token, тот же скрипт без
`--dry-run` делает только readonly `TradingSchedules` call. Sandbox `PostOrder` разрешен
только явным флагом и параметрами заявки:

```powershell
python -m pip install -e ".[tbank]" --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
python scripts/run_sandbox_smoke.py
python scripts/run_sandbox_smoke.py --allow-sandbox-orders --account-id "<sandbox-account>" --instrument-id "MOEX:SBER" --price 300.10
```

`--allow-sandbox-orders` создает `LaunchModePolicy` с
`sandbox_orders_confirmed=true`. Без этого даже sandbox execution layer обязан
писать `order_submission_mode=sandbox_pseudo_order` и не вызывать `PostOrder`.

Sandbox results нельзя использовать как прямую оценку real execution quality. Это проверка инфраструктуры и adapter lifecycle.

## Local acceptance gate

Единая локальная команда controlled launch:

```powershell
python scripts/run_controlled_launch_acceptance.py
```

Она последовательно проверяет:

- `python scripts/check.py`;
- analytics-smoke;
- report rebuild;
- replay-day determinism;
- `docker compose config --quiet`;
- Alembic `upgrade -> downgrade -1 -> upgrade` на временной SQLite БД;
- sandbox dry-run без токенов и без реальных orders;
- production guard tests: production падает без `TRADING_PRODUCTION_CONFIRM`, а API production
  не стартует с dev auth;
- отсутствие raw token/Bearer secrets в отслеживаемых текстовых файлах.

Для быстрого локального прохода после отдельного `python scripts/check.py`:

```powershell
python scripts/run_controlled_launch_acceptance.py --skip-full-check
```

## Shadow mode

Shadow mode использует live market data, но execution остается pseudo:

- strategy/risk работают штатно;
- `order_intent` и causal events сохраняются;
- reports/counterfactual строятся как в production;
- реальные `PostOrder`/`CancelOrder` запрещены policy.

## CI gates

GitHub Actions workflow `.github/workflows/ci.yml` содержит jobs:

- `backend-quality` - pytest, ruff, mypy;
- `frontend-quality` - Vue typecheck, unit tests, build;
- `migration-check` - Alembic upgrade/downgrade/upgrade на PostgreSQL;
- `smoke-build` - compose config, replay smoke, sandbox dry-run smoke.

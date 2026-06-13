# API Contract

`api` - это FastAPI BFF для frontend. Он предоставляет REST для команд, снимков состояния, конфигурации и отчетов, а WebSocket - для live feed.

Тяжелые отчеты не строятся внутри API handlers. API только ставит задачи в `report-worker` и возвращает статус.

## REST endpoints

| Method | Path | Назначение |
| --- | --- | --- |
| `POST` | `/robot/start` | Запросить запуск робота в настроенном режиме. |
| `POST` | `/robot/stop` | Запросить controlled stop. |
| `GET` | `/robot/status` | Получить текущее состояние робота. |
| `GET` | `/session/current` | Получить текущую биржевую сессию и micro-session. |
| `GET` | `/positions` | Получить текущие позиции. |
| `GET` | `/orders/open` | Получить открытые ордера. |
| `GET` | `/signals/current` | Получить текущие candidates и blockers. |
| `GET` | `/market/overview` | Получить market overview по включенным инструментам. |
| `GET` | `/reports/hourly` | Получить hourly reports по фильтрам. |
| `GET` | `/reports/daily` | Получить daily reports по фильтрам. |
| `POST` | `/reports/daily/run` | Поставить rebuild daily report в `report-worker`. |
| `GET` | `/reports/counterfactual` | Получить counterfactual analytics. |
| `GET` | `/config/strategy` | Прочитать strategy config. |
| `PUT` | `/config/strategy` | Обновить strategy config через audited change. |

## `/robot/status`

Минимальные поля:

- balance;
- active instruments;
- active timeframes;
- strategy state;
- `session_type`;
- `session_phase`;
- `broker_trading_status`;
- `micro_session_id`;
- countdown до rollover;
- current blocker/candidate;
- PnL;
- positions;
- active orders;
- market stream health;
- last report status;
- service health summary.

## WebSocket channels

| Path | Назначение |
| --- | --- |
| `/ws/dashboard` | Общий live feed для dashboard. |
| `/ws/orders` | Order lifecycle updates. |
| `/ws/market` | Market overview, top of book, candles, market quality. |
| `/ws/reports` | Статусы report tasks и новые reports. |

## WebSocket message envelope

```json
{
  "message_id": "uuid",
  "ts_utc": "2026-06-13T12:00:00Z",
  "type": "dashboard.snapshot",
  "run_id": "uuid",
  "micro_session_id": "2026-06-13T07",
  "payload": {}
}
```

`message_id` и timestamps обязательны для deduplication и traceability.

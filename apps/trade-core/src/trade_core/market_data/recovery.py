"""Gap recovery hooks after market stream reconnects."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trade_core.broker_gateway import (
    BrokerGateway,
    CandleRequest,
    InstrumentRef,
    OrdersRequest,
)
from trade_core.market_data.event_bus import MarketEventBus
from trade_core.market_data.events import MarketDataEvent, MarketEventType, Timeframe, ensure_utc
from trade_core.market_data.subscriptions import candle_from_mapping

RefreshPositionsHook = Callable[[str], Awaitable[object] | object]


@dataclass(frozen=True, slots=True)
class GapRecoveryRequest:
    instruments: tuple[InstrumentRef, ...]
    candle_timeframes: tuple[Timeframe, ...]
    from_ts_utc: datetime
    to_ts_utc: datetime
    account_id: str | None = None


class GapRecoveryCoordinator:
    """Backfill candles and refresh account state after stream gaps."""

    def __init__(
        self,
        *,
        broker_gateway: BrokerGateway,
        event_bus: MarketEventBus,
        refresh_positions_hook: RefreshPositionsHook | None = None,
    ) -> None:
        self._broker_gateway = broker_gateway
        self._event_bus = event_bus
        self._refresh_positions_hook = refresh_positions_hook

    async def recover_after_reconnect(self, request: GapRecoveryRequest) -> None:
        started_at = ensure_utc(datetime.now().astimezone())
        await self._event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.RECOVERY_REQUESTED,
                payload=request,
                ts_utc=started_at,
                instrument_id=None,
            )
        )

        recovered_candles = 0
        for instrument in request.instruments:
            for timeframe in request.candle_timeframes:
                response = await self._broker_gateway.get_candles(
                    CandleRequest(
                        instrument=instrument,
                        interval=timeframe.value,
                        from_=request.from_ts_utc,
                        to=request.to_ts_utc,
                    )
                )
                for candle_payload in _iter_candle_payloads(response.data):
                    candle = candle_from_mapping(candle_payload, received_at=request.to_ts_utc)
                    await self._event_bus.publish(
                        MarketDataEvent(
                            event_type=MarketEventType.CANDLE,
                            payload=candle,
                            ts_utc=request.to_ts_utc,
                            instrument_id=candle.instrument_id,
                        )
                    )
                    recovered_candles += 1

        open_orders_refreshed = False
        positions_refreshed = False
        if request.account_id is not None:
            await self._broker_gateway.reconcile_open_orders(OrdersRequest(request.account_id))
            open_orders_refreshed = True
            if self._refresh_positions_hook is not None:
                result = self._refresh_positions_hook(request.account_id)
                if inspect.isawaitable(result):
                    await result
                positions_refreshed = True

        await self._event_bus.publish(
            MarketDataEvent(
                event_type=MarketEventType.RECOVERY_COMPLETED,
                payload={
                    "recovered_candles": recovered_candles,
                    "open_orders_refreshed": open_orders_refreshed,
                    "positions_refreshed": positions_refreshed,
                },
                ts_utc=ensure_utc(datetime.now().astimezone()),
                instrument_id=None,
            )
        )


def _iter_candle_payloads(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    candles = data.get("candles", ())
    if not isinstance(candles, Iterable):
        return ()
    return (dict(candle) for candle in candles if isinstance(candle, dict))

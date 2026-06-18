"""Per-method T-Invest API deadlines based on official recommendations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Deadline:
    method_name: str
    milliseconds: int

    @property
    def seconds(self) -> float:
        return self.milliseconds / 1000


RECOMMENDED_DEADLINES_MS: Mapping[str, int] = MappingProxyType(
    {
        "TradingSchedules": 300,
        "GetTradingStatus": 500,
        "GetCandles": 500,
        "GetDividends": 500,
        "GetLastPrices": 500,
        "GetOrderBook": 500,
        "PostOrder": 1500,
        "CancelOrder": 1500,
        "GetOrderState": 300,
        "GetOrders": 500,
        "GetPortfolio": 500,
        "GetPositions": 500,
        "GetAccounts": 500,
        "ResolveInstruments": 500,
        "PostStopOrder": 1500,
    }
)


def deadline_for(method_name: str) -> Deadline:
    try:
        milliseconds = RECOMMENDED_DEADLINES_MS[method_name]
    except KeyError as exc:
        msg = f"No configured T-Bank deadline for method {method_name!r}"
        raise KeyError(msg) from exc
    return Deadline(method_name=method_name, milliseconds=milliseconds)

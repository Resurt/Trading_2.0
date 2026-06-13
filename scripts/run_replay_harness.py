"""Run a deterministic replay smoke scenario without broker or Docker."""

# ruff: noqa: E402

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sys import path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from report_worker.analytics import AnalyticsAssumptions, PricePathPoint, analyze_counterfactual
from report_worker.analytics.models import CounterfactualSource
from trade_core.market_data import Candle, Timeframe
from trade_core.replay import (
    ReplayCounterfactualCase,
    ReplayEvent,
    ReplayEventType,
    ReplayHarness,
)
from trade_core.session import BrokerTradingStatus, ScheduleWindow, SessionManager, TradingSchedule
from trading_common.enums import SessionPhase, SessionType

MSK = ZoneInfo("Europe/Moscow")


def main() -> None:
    harness = ReplayHarness(counterfactual_callback=counterfactual_callback)
    result = harness.run(build_events())
    print(json.dumps(result.as_payload(), ensure_ascii=False, indent=2))


def build_events() -> list[ReplayEvent]:
    manager = SessionManager()
    schedule = TradingSchedule(
        windows=(
            ScheduleWindow(
                session_type=SessionType.WEEKDAY_MAIN,
                session_phase=SessionPhase.CONTINUOUS_TRADING,
                start_at=msk(2026, 6, 12, 10),
                end_at=msk(2026, 6, 12, 12),
                calendar_date=date(2026, 6, 12),
                trading_date=date(2026, 6, 12),
            ),
        )
    )
    broker_status = BrokerTradingStatus(status="normal_trading", api_trade_available=True)
    events = [
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 15).astimezone(UTC),
            event_type=ReplayEventType.SESSION_SNAPSHOT,
            payload=manager.evaluate(
                now=msk(2026, 6, 12, 10, 15),
                schedule=schedule,
                broker_status=broker_status,
            ),
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 59).astimezone(UTC),
            event_type=ReplayEventType.SESSION_SNAPSHOT,
            payload=manager.evaluate(
                now=msk(2026, 6, 12, 10, 59),
                schedule=schedule,
                broker_status=broker_status,
            ),
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 11, 0).astimezone(UTC),
            event_type=ReplayEventType.SESSION_SNAPSHOT,
            payload=manager.evaluate(
                now=msk(2026, 6, 12, 11, 0),
                schedule=schedule,
                broker_status=broker_status,
            ),
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 56).astimezone(UTC),
            event_type=ReplayEventType.BLOCKER_TRIGGERED,
            payload={"candidate_id": "replay-candidate-1", "reason_code": "spread_too_wide"},
        ),
        ReplayEvent(
            ts_utc=msk(2026, 6, 12, 10, 56).astimezone(UTC),
            event_type=ReplayEventType.COUNTERFACTUAL_SOURCE,
            payload=ReplayCounterfactualCase(
                source_event_type="blocked_candidate",
                instrument_id="MOEX:SBER",
                strategy_id="baseline",
                side="buy",
                event_ts=msk(2026, 6, 12, 10, 56).astimezone(UTC),
                entry_price=Decimal("100.00"),
                lot_qty=1,
                blocker_code="spread_too_wide",
            ),
        ),
    ]
    events.extend(candle_events())
    return events


def candle_events() -> list[ReplayEvent]:
    events: list[ReplayEvent] = []
    open_time = msk(2026, 6, 12, 10, 56)
    closes = [Decimal("100.10"), Decimal("100.20"), Decimal("100.30"), Decimal("100.40")]
    closes.extend([Decimal("100.50"), Decimal("100.45"), Decimal("100.55"), Decimal("100.60")])
    for index, close_price in enumerate(closes):
        start = open_time + timedelta(minutes=index)
        end = start + timedelta(minutes=1)
        candle = Candle(
            instrument_id="MOEX:SBER",
            timeframe=Timeframe.M1,
            open_ts_utc=start.astimezone(UTC),
            close_ts_utc=end.astimezone(UTC),
            exchange_open_ts=start,
            exchange_close_ts=end,
            open_price=Decimal("100.00"),
            high_price=close_price + Decimal("0.05"),
            low_price=Decimal("99.95"),
            close_price=close_price,
            volume_lots=Decimal("10"),
            is_closed=True,
            source="replay_fixture",
        )
        events.append(
            ReplayEvent(
                ts_utc=end.astimezone(UTC),
                event_type=ReplayEventType.CANDLE,
                payload=candle,
            )
        )
    return events


def counterfactual_callback(
    cases: Sequence[ReplayCounterfactualCase],
    candles: Sequence[Candle],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    assumptions = AnalyticsAssumptions()
    for case in cases:
        path_points = [
            PricePathPoint(
                ts_utc=candle.close_ts_utc,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
            )
            for candle in candles
            if candle.instrument_id == case.instrument_id and candle.close_ts_utc > case.event_ts
        ]
        analysis = analyze_counterfactual(
            source=CounterfactualSource(
                candidate_id=case.candidate_id,
                order_intent_id=case.order_intent_id,
                source_event_type=case.source_event_type,
                instrument_id=case.instrument_id,
                strategy_id=case.strategy_id,
                side=case.side,
                event_ts=case.event_ts,
                entry_price=case.entry_price,
                lot_qty=case.lot_qty,
                blocker_code=case.blocker_code,
                cancel_reason_code=case.cancel_reason_code,
            ),
            price_path=path_points,
            assumptions=assumptions,
        )
        results.append({"source": case.as_payload(), "analysis": analysis.as_payload()})
    return results


def msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


if __name__ == "__main__":
    main()

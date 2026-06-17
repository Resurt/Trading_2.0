from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import (
    BrokerGateway,
    BrokerUnaryResponse,
    InstrumentRef,
    PortfolioRequest,
    PositionsRequest,
)
from trade_core.portfolio import PositionService
from trade_core.session import SessionEventContext
from trading_common.db.base import Base
from trading_common.db.models import PositionSnapshot
from trading_common.enums import SessionPhase, SessionType


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def instrument() -> InstrumentRef:
    return InstrumentRef(
        instrument_id="MOEX:SBER",
        instrument_uid="uid-sber",
        class_code="TQBR",
        ticker="SBER",
    )


def context_for(instrument_id: str) -> SessionEventContext:
    del instrument_id
    return SessionEventContext(
        calendar_date=date(2026, 6, 12),
        trading_date=date(2026, 6, 12),
        session_type=SessionType.WEEKDAY_MAIN,
        session_phase=SessionPhase.CONTINUOUS_TRADING,
        micro_session_id="2026-06-12:weekday_main:20260612T1000",
        broker_trading_status="normal_trading",
    )


class FakePositionGateway:
    def __init__(
        self,
        *,
        positions_payloads: tuple[Mapping[str, Any], ...],
        portfolio_payloads: tuple[Mapping[str, Any], ...] | None = None,
    ) -> None:
        self._positions_payloads = list(positions_payloads)
        self._portfolio_payloads = list(portfolio_payloads or ({"positions": []},))
        self.get_positions_calls: list[PositionsRequest] = []
        self.get_portfolio_calls: list[PortfolioRequest] = []

    async def get_positions(
        self,
        request: PositionsRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.get_positions_calls.append(request)
        return BrokerUnaryResponse(
            method_name="GetPositions",
            data=dict(_next_payload(self._positions_payloads)),
        )

    async def get_portfolio(
        self,
        request: PortfolioRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        self.get_portfolio_calls.append(request)
        return BrokerUnaryResponse(
            method_name="GetPortfolio",
            data=dict(_next_payload(self._portfolio_payloads)),
        )


def test_position_service_refresh_writes_snapshot_and_portfolio_totals() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    gateway = FakePositionGateway(
        positions_payloads=(
            {
                "positions": [
                    {
                        "instrument_uid": "uid-sber",
                        "qty_lots": "5",
                        "position_side": "long",
                        "avg_price": "290",
                        "market_price": "300",
                        "unrealized_pnl": "50",
                        "realised_pnl": "10",
                        "exposure": "1500",
                        "short_available": True,
                    }
                ]
            },
        ),
        portfolio_payloads=({"positions": [], "short_allowed_by_account": True},),
    )

    with Session(engine) as session:
        service = PositionService(
            broker_gateway=cast(BrokerGateway, gateway),
            session=session,
            session_context_provider=context_for,
            tracked_instruments=(instrument(),),
        )
        result = asyncio.run(
            service.refresh_positions(
                "account-1",
                reason="unit_test_refresh",
                now=utc(2026, 6, 12, 7),
            )
        )
        session.commit()

        snapshot = session.execute(select(PositionSnapshot)).scalar_one()
        assert result.portfolio.open_position_lots == 5
        assert result.portfolio.long_position_lots == 5
        assert result.portfolio.short_position_lots == 0
        assert result.portfolio.gross_exposure_rub == Decimal("1500")
        assert snapshot.instrument_id == "MOEX:SBER"
        assert snapshot.position_side == "long"
        assert snapshot.qty_lots == 5
        assert snapshot.snapshot_payload["instrument_uid"] == "uid-sber"

    engine.dispose()


def test_position_service_blocks_stale_snapshot_before_entry() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    gateway = FakePositionGateway(positions_payloads=({"positions": []},))

    with Session(engine) as session:
        service = PositionService(
            broker_gateway=cast(BrokerGateway, gateway),
            session=session,
            session_context_provider=context_for,
            tracked_instruments=(instrument(),),
            freshness_seconds=30,
        )
        validation = asyncio.run(
            service.validate_before_entry(
                account_id="account-1",
                instrument_id="MOEX:SBER",
                now=utc(2026, 6, 12, 7),
            )
        )

        assert not validation.allowed
        assert validation.reason_code == "position_state_stale"
        assert validation.portfolio.position_state_fresh is False
        assert validation.portfolio.position_reconciliation_matched is False
        assert validation.broker_position_lots == 0
        assert session.scalar(select(PositionSnapshot.instrument_id)) == "MOEX:SBER"

    engine.dispose()


def test_position_service_blocks_broker_mismatch_against_local_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    gateway = FakePositionGateway(
        positions_payloads=(
            {"positions": [_position_payload("1")]},
            {"positions": [_position_payload("2")]},
        )
    )

    with Session(engine) as session:
        service = PositionService(
            broker_gateway=cast(BrokerGateway, gateway),
            session=session,
            session_context_provider=context_for,
            tracked_instruments=(instrument(),),
            freshness_seconds=30,
        )
        asyncio.run(
            service.refresh_positions(
                "account-1",
                reason="initial_local_snapshot",
                now=utc(2026, 6, 12, 7),
            )
        )
        session.commit()

        validation = asyncio.run(
            service.validate_before_entry(
                account_id="account-1",
                instrument_id="MOEX:SBER",
                now=utc(2026, 6, 12, 7) + timedelta(seconds=5),
            )
        )

        assert not validation.allowed
        assert validation.reason_code == "position_reconciliation_mismatch"
        assert validation.local_position_lots == 1
        assert validation.broker_position_lots == 2
        assert validation.portfolio.position_state_fresh is True
        assert validation.portfolio.position_reconciliation_matched is False

    engine.dispose()


def _position_payload(qty_lots: str) -> dict[str, object]:
    return {
        "instrument_uid": "uid-sber",
        "qty_lots": qty_lots,
        "position_side": "long",
        "market_price": "300",
        "exposure": str(Decimal(qty_lots) * Decimal("300")),
        "short_available": True,
    }


def _next_payload(payloads: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    if len(payloads) == 1:
        return payloads[0]
    return payloads.pop(0)

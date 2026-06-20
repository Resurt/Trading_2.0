from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import scripts.run_broker_balance_refresh as balance_refresh_cli

from trade_core.broker_gateway import BrokerUnaryResponse
from trading_common.db.base import Base
from trading_common.db.models import InstrumentRegistry, PositionSnapshot
from trading_common.db.service import DatabaseService


class FakeReadonlyBalanceGateway:
    instances: list[FakeReadonlyBalanceGateway] = []

    def __init__(self) -> None:
        self.post_order_calls = 0
        self.cancel_order_calls = 0
        FakeReadonlyBalanceGateway.instances.append(self)

    async def get_accounts(self, request: object) -> BrokerUnaryResponse:
        del request
        return BrokerUnaryResponse(
            method_name="GetAccounts",
            data={
                "accounts": [
                    {
                        "account_id": "account-999999",
                        "type": "broker",
                        "status": "open",
                    }
                ]
            },
        )

    async def get_positions(self, request: object, metadata: object = None) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetPositions",
            data={
                "account_id": "account-999999",
                "money": [{"currency": "RUB", "units": 99000, "nano": 0}],
                "blocked": [{"currency": "RUB", "units": 250, "nano": 0}],
                "positions": [],
            },
        )

    async def get_portfolio(self, request: object, metadata: object = None) -> BrokerUnaryResponse:
        del request, metadata
        return BrokerUnaryResponse(
            method_name="GetPortfolio",
            data={
                "account_id": "account-999999",
                "positions": [],
                "total_amount_portfolio": "150000",
                "expected_yield": "1000",
                "available_margin": "50000",
            },
        )

    async def post_order(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.post_order_calls += 1

    async def cancel_order(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.cancel_order_calls += 1


def test_broker_balance_refresh_cli_writes_masked_balance_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'balance-refresh.db'}"
    database = DatabaseService(database_url)
    Base.metadata.create_all(database.engine)
    _seed_instrument(database)
    FakeReadonlyBalanceGateway.instances.clear()
    monkeypatch.setattr(
        balance_refresh_cli,
        "TBankBrokerGateway",
        FakeReadonlyBalanceGateway,
    )

    payload = asyncio.run(
        balance_refresh_cli.async_main(
            argparse.Namespace(
                account_id=None,
                database_url=database_url,
                json_output=True,
                dry_run=False,
            )
        )
    )

    assert payload["balance_refreshed"] is True
    assert payload["account_id_masked"] == "acc***999"
    assert "account-999999" not in str(payload)
    assert FakeReadonlyBalanceGateway.instances[0].post_order_calls == 0
    assert FakeReadonlyBalanceGateway.instances[0].cancel_order_calls == 0
    with database.session_scope() as session:
        snapshot = session.query(PositionSnapshot).order_by(
            PositionSnapshot.snapshot_ts.desc()
        ).first()
        assert snapshot is not None
        broker_balance = cast(dict[str, object], snapshot.snapshot_payload["broker_balance"])
        assert broker_balance["account_id_masked"] == "acc***999"
        assert broker_balance["account_status"] == "open"
        assert broker_balance["total_portfolio_value_rub"] == "150000"
        assert "account-999999" not in str(broker_balance)


def _seed_instrument(database: DatabaseService) -> None:
    with database.session_scope() as session:
        session.add(
            InstrumentRegistry(
                instrument_id="MOEX:SBER",
                ticker="SBER",
                class_code="TQBR",
                figi="figi-sber",
                instrument_uid="uid-sber",
                name="SBER",
                lot_size=10,
                min_price_increment=Decimal("0.01"),
                currency="RUB",
                is_enabled=True,
                supports_morning=True,
                supports_evening=True,
                supports_weekend=False,
                source="tbank_resolved",
                resolved_at=datetime(2026, 6, 20, tzinfo=UTC),
                resolution_status="resolved",
                broker_payload={},
                instrument_payload={},
            )
        )

"""Validate sandbox adapter wiring without committing or printing secrets."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.broker_gateway import (
    AccountsRequest,
    CandleRequest,
    InstrumentRef,
    InstrumentResolveRequest,
    OrderBookRequest,
    OrderPlacementRequest,
    TradingSchedulesRequest,
    TradingStatusRequest,
)
from trade_core.infra.tbank import (
    TBankBrokerConfig,
    TBankBrokerGateway,
    TBankTokenBundle,
    build_sandbox_smoke_plan,
    load_tbank_tokens,
)
from trading_common import LaunchModePolicy, RuntimeMode


async def _sandbox_readonly_status(
    *,
    config: TBankBrokerConfig,
    tokens: TBankTokenBundle,
) -> dict[str, object]:
    if not (tokens.readonly_token or tokens.full_access_token):
        return {"status": "skipped_no_token"}

    gateway = TBankBrokerGateway(config=config, tokens=tokens)
    now = datetime.now(tz=UTC)
    await gateway.trading_schedules(
        TradingSchedulesRequest(
            exchange="MOEX",
            from_=now,
            to=now + timedelta(days=1),
        )
    )
    accounts_response = await gateway.get_accounts(AccountsRequest())
    resolve_response = await gateway.resolve_instruments(
        InstrumentResolveRequest(tickers=("SBER",), class_code="TQBR")
    )
    instrument_payloads = resolve_response.data.get("instruments", [])
    if not isinstance(instrument_payloads, list) or not instrument_payloads:
        return {"status": "failed_no_resolved_instrument"}
    instrument_payload = instrument_payloads[0]
    if not isinstance(instrument_payload, dict):
        return {"status": "failed_bad_instrument_payload"}
    instrument = InstrumentRef(
        instrument_id=str(instrument_payload.get("instrument_id")),
        instrument_uid=str(instrument_payload.get("instrument_uid") or ""),
        class_code=str(instrument_payload.get("class_code") or "TQBR"),
        ticker=str(instrument_payload.get("ticker") or "SBER"),
    )
    await gateway.get_trading_status(TradingStatusRequest(instrument=instrument))
    await gateway.get_candles(
        CandleRequest(
            instrument=instrument,
            interval="1m",
            from_=now - timedelta(minutes=30),
            to=now,
        )
    )
    await gateway.get_order_book(OrderBookRequest(instrument=instrument, depth=10))
    accounts = accounts_response.data.get("accounts", [])
    return {
        "status": "ok",
        "accounts_count": len(accounts) if isinstance(accounts, list) else None,
        "resolved_ticker": instrument.ticker,
        "resolved_instrument_id_present": bool(instrument.instrument_id),
        "resolved_instrument_uid_present": bool(instrument.instrument_uid),
    }


async def _sandbox_order_status(
    *,
    config: TBankBrokerConfig,
    tokens: TBankTokenBundle,
    account_id: str,
    instrument_id: str,
    price: Decimal,
    quantity: int,
) -> str:
    gateway = TBankBrokerGateway(config=config, tokens=tokens)
    await gateway.post_order(
        OrderPlacementRequest(
            account_id=account_id,
            instrument=InstrumentRef(instrument_id=instrument_id),
            side="buy",
            order_type="limit",
            lot_qty=quantity,
            price=price,
            time_in_force="day",
            client_order_key=f"sandbox-smoke:{instrument_id}:{price}:{quantity}",
        )
    )
    return "posted"


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Run T-Bank sandbox smoke configuration check.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate endpoints and mode without requiring tokens.",
    )
    parser.add_argument(
        "--allow-sandbox-orders",
        action="store_true",
        help="Allow one explicit sandbox PostOrder smoke call.",
    )
    parser.add_argument("--account-id", help="Sandbox account id for --allow-sandbox-orders.")
    parser.add_argument("--instrument-id", help="Instrument id for sandbox PostOrder smoke.")
    parser.add_argument("--price", type=Decimal, help="Limit price for sandbox PostOrder smoke.")
    parser.add_argument("--quantity", type=int, default=1, help="Lots for sandbox PostOrder smoke.")
    args = parser.parse_args()

    policy = LaunchModePolicy.from_mode(
        RuntimeMode.SANDBOX,
        sandbox_orders_confirmed=args.allow_sandbox_orders,
    )
    config = TBankBrokerConfig.from_launch_policy(policy)
    tokens = TBankTokenBundle(full_access_token=None, readonly_token=None)
    if not args.dry_run:
        tokens = load_tbank_tokens()
    if args.allow_sandbox_orders and args.dry_run:
        parser.error("--allow-sandbox-orders cannot be combined with --dry-run")
    if args.allow_sandbox_orders and not tokens.full_access_token:
        parser.error("--allow-sandbox-orders requires configured full-access sandbox token")

    readonly_status: dict[str, object] | str = "dry_run"
    sandbox_order_status = "not_requested"
    if not args.dry_run:
        readonly_status = await _sandbox_readonly_status(config=config, tokens=tokens)
    if args.allow_sandbox_orders:
        missing_order_args = [
            name
            for name, value in (
                ("--account-id", args.account_id),
                ("--instrument-id", args.instrument_id),
                ("--price", args.price),
            )
            if value is None
        ]
        if missing_order_args:
            parser.error("--allow-sandbox-orders requires " + ", ".join(missing_order_args))
        sandbox_order_status = await _sandbox_order_status(
            config=config,
            tokens=tokens,
            account_id=str(args.account_id),
            instrument_id=str(args.instrument_id),
            price=args.price,
            quantity=args.quantity,
        )

    plan = build_sandbox_smoke_plan(
        policy=policy,
        config=config,
        tokens=tokens,
        dry_run=args.dry_run,
        allow_sandbox_orders=args.allow_sandbox_orders,
        readonly_call_status=readonly_status,
        sandbox_order_status=sandbox_order_status,
    )
    print(json.dumps(plan.as_payload(), ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

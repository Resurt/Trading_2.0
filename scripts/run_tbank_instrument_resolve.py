"""Resolve configured MOEX tickers to real T-Bank instrument IDs."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.broker_gateway import BrokerUnaryResponse, InstrumentRef, InstrumentResolveRequest
from trade_core.infra.tbank import TBankBrokerGateway
from trade_core.instruments import InstrumentResolverService, is_broker_resolved_instrument
from trade_core.runtime import SafeNoopBrokerGateway
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.config import build_database_url_from_env
from trading_common.db.models import InstrumentRegistry
from trading_common.db.service import DatabaseService


class DryRunInstrumentGateway(SafeNoopBrokerGateway):
    async def resolve_instruments(
        self,
        request: InstrumentResolveRequest,
        metadata: object | None = None,
    ) -> BrokerUnaryResponse:
        del metadata
        return BrokerUnaryResponse(
            method_name="ResolveInstruments",
            data={
                "instruments": [
                    {
                        "instrument_id": f"dry-run-uid-{ticker.lower()}",
                        "instrument_uid": f"dry-run-uid-{ticker.lower()}",
                        "figi": f"dry-run-figi-{ticker.lower()}",
                        "ticker": ticker,
                        "class_code": request.class_code,
                        "name": ticker,
                        "lot_size": 10,
                        "min_price_increment": "0.01",
                        "currency": "RUB",
                        "api_trade_available": True,
                        "short_available": True,
                        "supports_weekend": False,
                    }
                    for ticker in request.tickers
                ]
            },
            headers={},
        )


def main() -> None:
    args = parse_args()
    payload = asyncio.run(async_main(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    raise SystemExit(0 if payload["ready_for_broker_calls"] or not args.strict else 7)


async def async_main(args: argparse.Namespace) -> dict[str, object]:
    tickers = split_csv(args.instruments)
    requested = tuple(
        InstrumentRef(
            instrument_id=f"MOEX:{ticker}",
            ticker=ticker,
            class_code=args.class_code,
        )
        for ticker in tickers
    )
    if args.dry_run:
        gateway = DryRunInstrumentGateway()
        response = await gateway.resolve_instruments(
            InstrumentResolveRequest(tickers=tickers, class_code=args.class_code)
        )
        resolved_payloads = response.data.get("instruments", [])
        return {
            "source": "tbank_resolved",
            "dry_run": True,
            "instruments_requested": len(tickers),
            "instruments_resolved": (
                len(resolved_payloads) if isinstance(resolved_payloads, list) else 0
            ),
            "instruments_failed": 0,
            "updated_registry_rows": 0,
            "unresolved_enabled_instruments": [],
            "ready_for_broker_calls": True,
            "real_orders_disabled": True,
        }

    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            gateway = TBankBrokerGateway()
            resolver = InstrumentResolverService(
                broker_gateway=gateway,
                session=session,
                launch_policy=LaunchModePolicy.from_mode(RuntimeMode.SHADOW),
                exchange="MOEX",
            )
            try:
                resolved = await resolver.resolve_startup_instruments(requested)
            except Exception as exc:
                unresolved = _unresolved_rows(session)
                return {
                    "source": "tbank_resolved",
                    "dry_run": False,
                    "instruments_requested": len(tickers),
                    "instruments_resolved": 0,
                    "instruments_failed": len(tickers),
                    "updated_registry_rows": 0,
                    "unresolved_enabled_instruments": unresolved,
                    "ready_for_broker_calls": False,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                    "real_orders_disabled": True,
                }
            unresolved = _unresolved_rows(session)
            ready = not unresolved and all(is_broker_resolved_instrument(item) for item in resolved)
            return {
                "source": "tbank_resolved",
                "dry_run": False,
                "instruments_requested": len(tickers),
                "instruments_resolved": len(resolved),
                "instruments_failed": max(len(tickers) - len(resolved), 0),
                "updated_registry_rows": len(resolved),
                "unresolved_enabled_instruments": unresolved,
                "ready_for_broker_calls": ready,
                "real_orders_disabled": True,
            }
    finally:
        database.engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T")
    parser.add_argument("--class-code", default="TQBR")
    parser.add_argument("--database-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in raw.split(",") if item.strip())


def _unresolved_rows(session: object) -> list[dict[str, object]]:
    rows = session.query(InstrumentRegistry).filter(InstrumentRegistry.is_enabled.is_(True)).all()
    return [
        {
            "instrument_id": row.instrument_id,
            "ticker": row.ticker,
            "source": row.source,
            "resolution_status": row.resolution_status,
            "instrument_uid_present": bool(row.instrument_uid),
            "figi_present": bool(row.figi),
            "resolution_error_code": row.resolution_error_code,
            "resolution_error_message": row.resolution_error_message,
        }
        for row in rows
        if not is_broker_resolved_instrument(row)
    ]


if __name__ == "__main__":
    main()

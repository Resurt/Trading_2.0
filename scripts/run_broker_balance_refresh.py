"""Refresh readonly broker balance snapshot for dashboard cards."""

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

from trade_core.infra.tbank import TBankBrokerGateway
from trade_core.portfolio import BrokerBalanceRefreshService
from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService


def main() -> None:
    args = parse_args()
    payload = asyncio.run(async_main(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    success = bool(payload.get("balance_refreshed") or payload.get("balance_degraded"))
    raise SystemExit(0 if success else 1)


async def async_main(args: argparse.Namespace) -> dict[str, object]:
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        gateway = TBankBrokerGateway()
    except Exception as exc:
        return {
            "balance_refreshed": False,
            "balance_degraded": True,
            "balance_degraded_reason_code": _reason_from_exception(
                exc,
                default="broker_gateway_unavailable",
            ),
            "account_id_masked": _mask_account_id(args.account_id),
        }
    with database.session_scope() as session:
        service = BrokerBalanceRefreshService(
            broker_gateway=gateway,
            session=session,
        )
        result = await service.refresh(
            account_id=args.account_id,
            dry_run=args.dry_run,
        )
        return result.as_payload()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _mask_account_id(account_id: str | None) -> str | None:
    if not account_id:
        return None
    if len(account_id) <= 6:
        return f"{account_id[:2]}***"
    return f"{account_id[:3]}***{account_id[-3:]}"


def _reason_from_exception(exc: Exception, *, default: str) -> str:
    text = str(exc).strip()
    if text and " " not in text and len(text) <= 96:
        return text
    return default


if __name__ == "__main__":
    main()

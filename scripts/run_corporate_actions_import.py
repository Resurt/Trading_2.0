"""Import corporate action events from CSV/JSON or a single manual CLI row."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from datetime import date
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

from trade_core.corporate_actions import (  # noqa: E402
    CorporateActionEvent,
    CorporateActionImportConfig,
    CorporateActionService,
)
from trading_common.db.config import build_database_url_from_env  # noqa: E402
from trading_common.db.service import DatabaseService  # noqa: E402


def main() -> None:
    args = parse_args()
    config = CorporateActionImportConfig(source=args.source, confidence=args.confidence)
    database = DatabaseService(args.database_url or build_database_url_from_env())
    try:
        with database.session_scope() as session:
            service = CorporateActionService(session)
            if args.file:
                file_path = Path(args.file)
                if file_path.suffix.lower() == ".json":
                    rows = service.import_json(file_path, config=config)
                else:
                    rows = service.import_csv(file_path, config=config)
            else:
                rows = (
                    service.upsert_event(
                        CorporateActionEvent(
                            instrument_id=args.instrument_id or f"MOEX:{args.ticker.upper()}",
                            ticker=args.ticker.upper(),
                            action_type=args.action_type,
                            ex_date=args.ex_date,
                            registry_close_date=args.registry_close_date,
                            payment_date=args.payment_date,
                            amount_per_share=args.amount_per_share,
                            currency=args.currency,
                            source=args.source,
                            confidence=args.confidence,
                            action_payload={
                                "source": args.source,
                                "confidence": args.confidence,
                                "created_from": "manual_cli",
                            },
                        )
                    ),
                )
            payload = {
                "source": "corporate_actions_import",
                "rows_imported": len(rows),
                "corporate_action_ids": [str(row.corporate_action_id) for row in rows],
            }
    finally:
        database.engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file")
    parser.add_argument("--ticker", default="SBER")
    parser.add_argument("--instrument-id")
    parser.add_argument("--action-type", default="dividend")
    parser.add_argument("--ex-date", type=parse_date)
    parser.add_argument("--registry-close-date", type=parse_date)
    parser.add_argument("--payment-date", type=parse_date)
    parser.add_argument("--amount-per-share", type=Decimal)
    parser.add_argument("--currency", default="RUB")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--confidence", default="manual_unverified")
    parser.add_argument("--database-url")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


if __name__ == "__main__":
    main()

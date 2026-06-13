from __future__ import annotations

import argparse
import json
from datetime import date

from report_worker.analytics import ReportAnalyticsService
from trading_common.db.config import build_database_url_from_env
from trading_common.db.service import DatabaseService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run daily counterfactual analytics.")
    parser.add_argument("--trading-date", required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--database-url")
    args = parser.parse_args()

    database = DatabaseService(args.database_url or build_database_url_from_env())
    with database.session_scope() as session:
        service = ReportAnalyticsService(session)
        results = service.run_counterfactual_analysis_for_date(
            trading_date=date.fromisoformat(args.trading_date),
            strategy_id=args.strategy_id,
        )
        payload = {
            "trading_date": args.trading_date,
            "strategy_id": args.strategy_id,
            "result_count": len(results),
            "results": service.counterfactual_read_models(results),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

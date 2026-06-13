from __future__ import annotations

import argparse

from _cli import add_common_report_args, build_service, parsed_date, print_payload, run_with_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Run counterfactual analytics for a trading date.")
    add_common_report_args(parser)
    args = parser.parse_args()

    database, scope = run_with_service(args.database_url)
    with scope as session:
        service = build_service(session)
        results = service.run_counterfactual_analysis_for_date(
            trading_date=parsed_date(args.date),
            strategy_id=args.strategy_id,
            instrument_id=args.instrument_id,
            timeframe=args.timeframe,
            session_type=args.session_type,
            strategy_version=args.strategy_version,
            force_rebuild=args.force_rebuild,
        )
        payload = {
            "trading_date": args.date,
            "strategy_id": args.strategy_id,
            "instrument": args.instrument_id,
            "timeframe": args.timeframe,
            "session_type": args.session_type,
            "strategy_version": args.strategy_version,
            "result_count": len(results),
            "results": service.counterfactual_read_models(results),
        }
        print_payload(payload, output_format=args.output_format)
    database.engine.dispose()


if __name__ == "__main__":
    main()

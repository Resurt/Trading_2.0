from __future__ import annotations

import argparse

from _cli import add_common_report_args, build_service, parsed_date, print_payload, run_with_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Build hourly report rows for a trading date.")
    add_common_report_args(parser)
    parser.add_argument("--micro-session-id")
    args = parser.parse_args()

    database, scope = run_with_service(args.database_url)
    with scope as session:
        service = build_service(session)
        if args.micro_session_id:
            report = service.build_hourly_report(
                micro_session_id=args.micro_session_id,
                strategy_id=args.strategy_id,
                force_rebuild=args.force_rebuild,
            )
            payload: dict[str, object] = service.hourly_read_model(report)
        else:
            reports = service.build_hourly_reports_for_date(
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
                "result_count": len(reports),
                "reports": [service.hourly_read_model(report) for report in reports],
            }
        print_payload(payload, output_format=args.output_format)
    database.engine.dispose()


if __name__ == "__main__":
    main()

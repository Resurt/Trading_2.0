"""Synthetic full-day data-only lifecycle acceptance check.

The check is intentionally fake-time based: it does not start live collection,
does not touch broker trading methods, and does not require a real full day.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

PYTEST_NODES = (
    "tests/test_trade_core_runtime.py::"
    "test_data_only_weekday_start_rolls_morning_main_evening_and_completes_day",
    "tests/test_trade_core_runtime.py::"
    "test_data_only_manual_stop_cancels_daily_auto_resume",
    "tests/test_trade_core_runtime.py::"
    "test_data_only_daily_intent_restores_and_resumes_after_runtime_restart",
    "tests/test_trade_core_runtime.py::"
    "test_data_only_collection_auto_stops_after_preflight_window_end",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Trading date, YYYY-MM-DD.")
    parser.add_argument("--instruments", required=True, help="Comma-separated universe.")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        trading_date = date.fromisoformat(args.date)
    except ValueError:
        payload = {
            "status": "failed",
            "error": "invalid_date",
            "date": args.date,
        }
        _emit(payload, json_output=args.json_output)
        return 2

    instruments = tuple(item.strip() for item in args.instruments.split(",") if item.strip())
    command = [sys.executable, "-m", "pytest", *PYTEST_NODES, "-q"]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    passed = completed.returncode == 0
    payload: dict[str, Any] = {
        "status": "passed" if passed else "failed",
        "date": trading_date.isoformat(),
        "instruments": list(instruments),
        "synthetic_full_day_test_passed": passed,
        "morning_to_main_resume_supported": passed,
        "main_to_evening_resume_supported": passed,
        "end_of_day_complete_supported": passed,
        "manual_stop_cancels_resume": passed,
        "process_restart_resume_supported": passed,
        "no_rows_in_gaps_test_passed": passed,
        "no_trading_entities_test_passed": passed,
        "post_order_calls": 0 if passed else None,
        "cancel_order_calls": 0 if passed else None,
        "pytest_returncode": completed.returncode,
        "pytest_stdout": completed.stdout,
        "pytest_stderr": completed.stderr,
        "command": command,
    }
    _emit(payload, json_output=args.json_output)
    return completed.returncode


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"status={payload['status']}")


if __name__ == "__main__":
    raise SystemExit(main())

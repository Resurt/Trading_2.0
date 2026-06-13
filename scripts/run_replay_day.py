"""Replay a synthetic trading day and verify deterministic logging/analytics signals."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from sys import exit, path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT,
    ROOT / "scripts",
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "apps" / "report-worker" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from scripts.run_replay_harness import build_events, counterfactual_callback  # noqa: E402
from trade_core.replay import ReplayHarness  # noqa: E402

DEFAULT_REPLAY_DATE = date(2026, 6, 12)


def main() -> None:
    args = parse_args()
    first = _run_once()
    second = _run_once()
    deterministic = first == second
    passed = (
        deterministic
        and bool(first["session_rollover_verified"])
        and bool(first["blocker_pipeline_verified"])
        and bool(first["counterfactual_pipeline_verified"])
    )
    payload = {
        "requested_trading_date": args.trading_date.isoformat(),
        "fixture_trading_date": DEFAULT_REPLAY_DATE.isoformat(),
        "passed": passed,
        "deterministic": deterministic,
        "result": first,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    exit(0 if passed else 1)


def _run_once() -> dict[str, object]:
    harness = ReplayHarness(counterfactual_callback=counterfactual_callback)
    return harness.run(build_events()).as_payload()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="trading_date", type=parse_date, default=DEFAULT_REPLAY_DATE)
    return parser.parse_args()


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()

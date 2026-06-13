"""Validate sandbox adapter wiring without committing or printing secrets."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
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

from trade_core.infra.tbank import (
    TBankBrokerConfig,
    TBankTokenBundle,
    build_sandbox_smoke_plan,
    load_tbank_tokens_for_launch,
)
from trading_common import LaunchModePolicy, RuntimeMode


def main() -> None:
    parser = argparse.ArgumentParser(description="Run T-Bank sandbox smoke configuration check.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate endpoints and mode without requiring tokens.",
    )
    args = parser.parse_args()

    policy = LaunchModePolicy.from_mode(RuntimeMode.SANDBOX)
    config = TBankBrokerConfig.from_launch_policy(policy)
    tokens = TBankTokenBundle(full_access_token=None, readonly_token=None)
    if not args.dry_run:
        tokens = load_tbank_tokens_for_launch(policy)
    plan = build_sandbox_smoke_plan(
        policy=policy,
        config=config,
        tokens=tokens,
        dry_run=args.dry_run,
    )
    print(json.dumps(plan.as_payload(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

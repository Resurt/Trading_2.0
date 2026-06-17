"""Fail-fast check that the optional T-Bank SDK extra is installed."""

# ruff: noqa: E402

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from sys import path

ROOT = Path(__file__).resolve().parents[1]
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in path:
        path.insert(0, str(src))

from trade_core.infra.tbank.sdk_clients import SDK_PACKAGE_NAME, load_tbank_sdk


def main() -> None:
    try:
        sdk = load_tbank_sdk()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "sdk_package": SDK_PACKAGE_NAME,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(1) from exc

    print(
        json.dumps(
            {
                "ok": True,
                "sdk_package": SDK_PACKAGE_NAME,
                "module": getattr(sdk, "__name__", SDK_PACKAGE_NAME),
                "version": _sdk_distribution_version(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _sdk_distribution_version() -> str | None:
    for package_name in ("t-tech-investments", "t_tech-investments"):
        try:
            return metadata.version(package_name)
        except metadata.PackageNotFoundError:
            continue
    return None


if __name__ == "__main__":
    main()

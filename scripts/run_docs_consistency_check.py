"""Small guardrail for required documentation anchors."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS = (
    ("Docs/README.md", "runbooks"),
    ("Docs/prompts/README.md", "Historical implementation prompts"),
    ("Docs/runbooks/data-only-shadow.md", "market_closed_expected"),
    ("Docs/runbooks/data-only-shadow.md", "next_session_at"),
    ("Docs/frontend-dashboard-spec.md", "Balance card"),
    ("Docs/api-contract.md", "data-only shadow"),
    ("Docs/api-contract.md", "/analytics/intraday"),
    ("Docs/database-schema.md", "market_microstructure_snapshot"),
    ("Docs/database-schema.md", "rolling_performance_cube"),
    ("Docs/runbooks/analytics-and-calibration-center.md", "Calibration Center"),
)


def main() -> None:
    failures: list[dict[str, str]] = []
    for relative_path, needle in CHECKS:
        path = ROOT / relative_path
        if not path.exists():
            failures.append({"path": relative_path, "missing": "file"})
            continue
        content = path.read_text(encoding="utf-8")
        if needle not in content:
            failures.append({"path": relative_path, "missing": needle})
    payload = {
        "passed": not failures,
        "checks": [{"path": path, "contains": needle} for path, needle in CHECKS],
        "failures": failures,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()

"""Check that rebuilt API containers expose the current route contract."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

EXPECTED_ROUTES = (
    "/market/microstructure/latest",
    "/market/microstructure/summary",
    "/runtime/data-shadow/status",
    "/portfolio/summary",
    "/analytics/intraday/today",
    "/analytics/intraday",
    "/calibration/observatory/status",
    "/calibration/rolling-performance",
    "/calibration/regime",
    "/calibration/config-candidates",
)
REBUILD_HINT = "docker compose up -d --build api frontend"


def main() -> None:
    args = parse_args()
    payload = run_smoke(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    raise SystemExit(0 if payload["passed"] else 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default="http://localhost:8000")
    parser.add_argument("--frontend-url", default="http://localhost:5173")
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


def run_smoke(args: argparse.Namespace) -> dict[str, object]:
    compose = _run(["docker", "compose", "ps"])
    health = _get_json(f"{args.api_base_url.rstrip('/')}/health")
    openapi = _get_json(f"{args.api_base_url.rstrip('/')}/openapi.json")
    frontend = (
        {"ok": True, "skipped": True}
        if args.skip_frontend
        else _get_text(args.frontend_url.rstrip("/"))
    )
    paths = openapi.get("json", {}).get("paths", {})
    existing_paths = set(paths) if isinstance(paths, dict) else set()
    missing_routes = [route for route in EXPECTED_ROUTES if route not in existing_paths]
    passed = (
        compose["returncode"] == 0
        and health["ok"]
        and openapi["ok"]
        and frontend["ok"]
        and not missing_routes
    )
    return {
        "passed": passed,
        "docker_compose_ps": compose,
        "api_health_ok": health["ok"],
        "frontend_ok": frontend["ok"],
        "openapi_ok": openapi["ok"],
        "missing_routes": missing_routes,
        "expected_routes": list(EXPECTED_ROUTES),
        "rebuild_hint": REBUILD_HINT if missing_routes else None,
    }


def _run(argv: Sequence[str]) -> dict[str, object]:
    completed = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "command": " ".join(argv),
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


def _get_json(url: str) -> dict[str, object]:
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as response:
            body = response.read().decode("utf-8")
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "json": json.loads(body),
            }
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error_code": type(exc).__name__, "error_message": str(exc)}


def _get_text(url: str) -> dict[str, object]:
    try:
        with urlopen(Request(url), timeout=10) as response:
            response.read(512)
            return {"ok": 200 <= response.status < 500, "status": response.status}
    except (OSError, URLError) as exc:
        return {"ok": False, "error_code": type(exc).__name__, "error_message": str(exc)}


if __name__ == "__main__":
    main()

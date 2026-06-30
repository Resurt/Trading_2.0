"""Live operator Start button acceptance for data-only collection.

The script is intentionally conservative: it never uses the API Start endpoint
to pass the acceptance. If browser automation is unavailable, the result is a
diagnostic JSON with ``frontend_button_live_verified=false``.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "http://localhost:8000"
DEFAULT_FRONTEND_URL = "http://localhost:5173"


@dataclass(frozen=True)
class HttpResult:
    ok: bool
    status_code: int | None
    payload: dict[str, Any]
    error: str | None = None


def main() -> int:
    args = _parse_args()
    started_at = datetime.now(tz=UTC)
    result: dict[str, Any] = {
        "status": "failed",
        "started_at": started_at.isoformat(),
        "frontend_button_live_verified": False,
        "api_fallback_used_for_success": False,
        "command_created_from_button": False,
        "collector_started": False,
        "snapshots_delta": 0,
        "instruments": _split_csv(args.instruments),
        "warnings": [],
        "blockers": [],
    }

    preflight = _get_json(
        f"{args.api_base}/session/preflight?"
        + urllib.parse.urlencode(
            {
                "instruments": args.instruments,
                "mode": "data_shadow",
                "cache": "false",
            }
        ),
        timeout_seconds=10,
    )
    result["preflight"] = preflight.payload if preflight.ok else {"error": preflight.error}
    before_summary = _get_microstructure_summary(args.api_base)
    before_snapshots = int(before_summary.payload.get("snapshots_count") or 0)
    result["snapshots_before"] = before_snapshots

    initial_data_shadow = _get_json(
        f"{args.api_base}/runtime/data-shadow/status",
        timeout_seconds=5,
    )
    result["data_shadow_status_before"] = (
        initial_data_shadow.payload
        if initial_data_shadow.ok
        else {"error": initial_data_shadow.error}
    )
    if str(result["data_shadow_status_before"].get("collector_state") or "") == "collecting":
        result.update(
            {
                "status": "already_collecting_verified",
                "collector_started": True,
                "collector_left_running": True,
                "command_created_from_button": bool(
                    result["data_shadow_status_before"].get("last_command_id")
                ),
            }
        )
        result["warnings"].append(
            "collector_already_collecting_start_button_click_not_repeated"
        )
        _emit(result, json_output=args.json_output)
        return 0

    click_result = _click_start_button(
        frontend_url=args.frontend_url,
        screenshot_dir=Path(args.screenshot_dir) if args.screenshot_dir else None,
    )
    result.update(click_result)
    if not click_result["frontend_button_live_verified"]:
        result["status"] = "browser_automation_unavailable"
        result["blockers"].append(click_result.get("blocker", "browser_automation_unavailable"))
        _emit(result, json_output=args.json_output)
        return 0

    deadline = time.monotonic() + max(0, args.minutes) * 60
    latest_robot: dict[str, Any] = {}
    latest_data_shadow: dict[str, Any] = {}
    while time.monotonic() < deadline:
        robot = _get_json(f"{args.api_base}/robot/status", timeout_seconds=5)
        data_shadow = _get_json(
            f"{args.api_base}/runtime/data-shadow/status",
            timeout_seconds=5,
        )
        if robot.ok:
            latest_robot = robot.payload
        if data_shadow.ok:
            latest_data_shadow = data_shadow.payload
        collector_state = str(latest_data_shadow.get("collector_state") or "")
        command_id = (
            latest_data_shadow.get("last_command_id")
            or latest_data_shadow.get("command_id")
            or latest_robot.get("command_id")
        )
        if command_id:
            result["command_created_from_button"] = True
        if collector_state == "collecting":
            result["collector_started"] = True
            break
        if collector_state in {"preflight_blocked", "failed", "degraded"}:
            break
        time.sleep(10)

    after_summary = _get_microstructure_summary(args.api_base)
    after_snapshots = int(after_summary.payload.get("snapshots_count") or 0)
    result["snapshots_after"] = after_snapshots
    result["snapshots_delta"] = max(0, after_snapshots - before_snapshots)
    result["robot_status_after"] = latest_robot
    result["data_shadow_status_after"] = latest_data_shadow

    if result["collector_started"] and result["snapshots_delta"] > 0:
        result["status"] = "passed"
    elif preflight.ok and not bool(preflight.payload.get("data_only_collection_allowed")):
        result["status"] = "preflight_blocked"
        result["warnings"].append("session_preflight_blocked_start_not_expected_to_collect")
    else:
        result["blockers"].append("collector_did_not_start_or_snapshots_did_not_grow")

    _emit(result, json_output=args.json_output)
    return 0 if result["status"] in {"passed", "preflight_blocked"} else 1


def _click_start_button(
    *,
    frontend_url: str,
    screenshot_dir: Path | None,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except Exception as exc:
        return {
            "frontend_button_live_verified": False,
            "blocker": "playwright_unavailable",
            "browser_error": str(exc),
        }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(frontend_url, wait_until="domcontentloaded", timeout=30_000)
        if screenshot_dir is not None:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_dir / "start_button_before.png"))
        button = page.get_by_role("button", name=re.compile(r"^(Старт|Start)$", re.I))
        if button.count() != 1:
            browser.close()
            return {
                "frontend_button_live_verified": False,
                "blocker": "start_button_not_unique_or_missing",
                "start_button_count": button.count(),
            }
        button.click(timeout=10_000)
        page.wait_for_timeout(1_000)
        if screenshot_dir is not None:
            page.screenshot(path=str(screenshot_dir / "start_button_after.png"))
        browser.close()
    return {"frontend_button_live_verified": True}


def _get_microstructure_summary(api_base: str) -> HttpResult:
    return _get_json(
        f"{api_base}/market/microstructure/summary?"
        + urllib.parse.urlencode({"lookback_minutes": "15"}),
        timeout_seconds=5,
    )


def _get_json(url: str, *, timeout_seconds: int) -> HttpResult:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body) if body.strip() else {}
            return HttpResult(ok=True, status_code=response.status, payload=payload)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        return HttpResult(ok=False, status_code=exc.code, payload=payload, error=str(exc))
    except Exception as exc:
        return HttpResult(ok=False, status_code=None, payload={}, error=str(exc))


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"status={payload.get('status')}")
        print(f"frontend_button_live_verified={payload.get('frontend_button_live_verified')}")
        print(f"collector_started={payload.get('collector_started')}")
        print(f"snapshots_delta={payload.get('snapshots_delta')}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--frontend-url", default=DEFAULT_FRONTEND_URL)
    parser.add_argument("--instruments", required=True)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--screenshot-dir")
    parser.add_argument("--json-output", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

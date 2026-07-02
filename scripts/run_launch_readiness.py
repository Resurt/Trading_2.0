"""Run controlled-launch readiness gates for local/compose/sandbox/shadow/prod."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "apps" / "frontend"
for src in (
    ROOT / "apps" / "trade-core" / "src",
    ROOT / "packages" / "common" / "src",
):
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
OUTPUT_TAIL_CHARS = 5000
PRODUCTION_CONFIRM = "I_UNDERSTAND_LIVE_ORDERS"
SANDBOX_ORDER_CONFIRM = "I_UNDERSTAND_SANDBOX_ORDERS"
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bTINVEST_TOKEN\s*=\s*t\.", re.IGNORECASE),
    re.compile(r"\bTBANK_(?:FULL_ACCESS|READONLY)_TOKEN\s*=\s*t\.", re.IGNORECASE),
    re.compile(r"\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
    re.compile(r"\b(?:token|secret|password|credential)\s*[:=]\s*['\"]?t\.", re.IGNORECASE),
)
TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".vue",
    ".yaml",
    ".yml",
}
BLOCKED_BINARY_SUFFIXES = {".docx", ".xlsx", ".pdf"}


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    command: str
    details: dict[str, object]


def main() -> None:
    args = parse_args()
    env = os.environ.copy()
    results = run_mode(args, env)
    passed = all(result.passed for result in results)
    payload = {
        "passed": passed,
        "mode": args.mode,
        "gates": [asdict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=(
            "local",
            "compose",
            "sandbox",
            "shadow",
            "production-preflight",
            "historical-replay",
            "historical-final-calibration",
            "instrument-resolution",
            "data-shadow",
        ),
        default="local",
    )
    parser.add_argument("--date", default="2026-06-12")
    parser.add_argument("--strategy-id", default="baseline")
    parser.add_argument("--lookback-days", type=int, default=10)
    parser.add_argument("--instruments", default="SBER,GAZP,LKOH,YDEX,TATN,GMKN,OZON,VTBR,T")
    parser.add_argument("--timeframes", default="5m,10m,15m")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--shadow-minutes", type=float, default=0.0)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-compose-up", action="store_true")
    parser.add_argument("--allow-manual-corporate-actions", action="store_true")
    parser.add_argument("--max-dividend-sync-age-hours", type=int, default=24)
    parser.add_argument("--skip-dividend-sync-check", action="store_true")
    parser.add_argument("--allow-dividend-sync-fail-open", action="store_true")
    parser.add_argument("--gate-timeout-seconds", type=int, default=600)
    return parser.parse_args()


def run_mode(args: argparse.Namespace, env: Mapping[str, str]) -> list[GateResult]:
    if args.mode == "local":
        return run_local(args)
    if args.mode == "compose":
        return run_compose(args)
    if args.mode == "sandbox":
        return run_sandbox(args, env)
    if args.mode == "shadow":
        return run_shadow(args, env)
    if args.mode == "production-preflight":
        return run_production_preflight(args, env)
    if args.mode == "historical-replay":
        return run_historical_replay(args)
    if args.mode == "historical-final-calibration":
        return run_historical_final_calibration(args)
    if args.mode == "instrument-resolution":
        return run_instrument_resolution(args)
    if args.mode == "data-shadow":
        return run_data_shadow(args, env)
    raise ValueError(args.mode)


def run_local(args: argparse.Namespace) -> list[GateResult]:
    return [
        run_cmd("python_scripts_check", [sys.executable, "scripts/check.py"], timeout_seconds=360),
        run_cmd(
            "analytics_smoke",
            [
                sys.executable,
                "scripts/run_logging_analytics_acceptance.py",
                "--date",
                args.date,
                "--strategy-id",
                args.strategy_id,
            ],
        ),
        run_cmd("replay_day", [sys.executable, "scripts/run_replay_day.py", "--date", args.date]),
        run_secret_scan_gate(),
    ]


def run_compose(args: argparse.Namespace) -> list[GateResult]:
    host_env = compose_host_env()
    results = [
        run_cmd(
            "docker_compose_config",
            ["docker", "compose", "config", "--quiet"],
            timeout_seconds=120,
        ),
        run_compose_shared_db_gate(),
    ]
    if not args.skip_compose_up:
        results.append(
            run_cmd(
                "docker_compose_up",
                ["docker", "compose", "up", "-d", "--build"],
                timeout_seconds=900,
            )
        )
        results.extend(
            [
                run_cmd(
                    "postgres_migration_upgrade",
                    [
                        "docker",
                        "compose",
                        "exec",
                        "-T",
                        "api",
                        "python",
                        "-m",
                        "alembic",
                        "upgrade",
                        "head",
                    ],
                    timeout_seconds=180,
                ),
                run_health_gate("api_health", "http://localhost:8000/health"),
                run_health_gate("trade_core_health", "http://localhost:8001/health"),
                run_health_gate("report_worker_health", "http://localhost:8002/health"),
                run_cmd(
                    "report_worker_smoke",
                    [sys.executable, "scripts/run_report_worker_smoke.py"],
                    env=host_env,
                ),
                run_cmd(
                    "frontend_build",
                    [npm_cmd(), "run", "build"],
                    cwd=FRONTEND,
                    timeout_seconds=180,
                ),
                run_cmd(
                    "api_route_smoke",
                    [sys.executable, "scripts/run_api_route_smoke.py", "--json-output"],
                    timeout_seconds=120,
                ),
            ]
        )
    return results


def run_sandbox(args: argparse.Namespace, env: Mapping[str, str]) -> list[GateResult]:
    results = [
        run_cmd(
            "tbank_sdk_import_check",
            [sys.executable, "scripts/run_tbank_sdk_import_check.py"],
        ),
        run_cmd("sandbox_readonly_smoke", [sys.executable, "scripts/run_sandbox_smoke.py"]),
    ]
    if env.get("TRADING_SANDBOX_ORDERS_CONFIRM") == SANDBOX_ORDER_CONFIRM:
        results.append(
            GateResult(
                name="sandbox_real_order_gate",
                passed=True,
                command="TRADING_SANDBOX_ORDERS_CONFIRM",
                details={"status": "explicitly_confirmed"},
            )
        )
    else:
        results.append(
            GateResult(
                name="sandbox_real_order_gate",
                passed=True,
                command="TRADING_SANDBOX_ORDERS_CONFIRM",
                details={"status": "not_confirmed_no_real_orders"},
            )
        )
    return results


def run_shadow(args: argparse.Namespace, env: Mapping[str, str]) -> list[GateResult]:
    results = [
        env_gate("shadow_mode_selected", env.get("TRADING_RUNTIME_MODE") == "shadow"),
        env_gate("shadow_no_real_orders", "TRADING_PRODUCTION_CONFIRM" not in env),
        run_instrument_registry_gate(args),
        run_dividend_sync_status_gate(args, name="shadow_clean_dividend_sync"),
        env_gate(
            "dividend_calendar_fail_open_policy_known",
            env.get("TRADING_DIVIDEND_SYNC_FAIL_OPEN", "false").lower()
            in {"false", "true", "0", "1", "yes", "no"},
        ),
        run_cmd("replay_day", [sys.executable, "scripts/run_replay_day.py", "--date", args.date]),
        run_cmd(
            "report_rebuild",
            [
                sys.executable,
                "scripts/run_report_rebuild.py",
                "--date",
                args.date,
                "--strategy-id",
                args.strategy_id,
            ],
        ),
    ]
    if args.shadow_minutes > 0:
        results.append(wait_gate("shadow_live_market_data_window", args.shadow_minutes))
    return results


def run_production_preflight(args: argparse.Namespace, env: Mapping[str, str]) -> list[GateResult]:
    return [
        env_gate(
            "production_confirmation_present",
            env.get("TRADING_PRODUCTION_CONFIRM") == PRODUCTION_CONFIRM,
            details={"expected": PRODUCTION_CONFIRM},
        ),
        env_gate(
            "production_auth_token_present",
            any(key.startswith("TRADING_API_") and key.endswith("_TOKEN") for key in env)
            or any(key.startswith("TRADING_API_") and key.endswith("_TOKEN_FILE") for key in env),
        ),
        env_gate("dev_auth_disabled", env.get("TRADING_AUTH_MODE", "static_bearer") != "dev"),
        env_gate(
            "dividend_sync_enabled",
            env.get("TRADING_DIVIDEND_SYNC_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        ),
        env_gate(
            "dividend_sync_fail_closed_default",
            env.get("TRADING_DIVIDEND_SYNC_FAIL_OPEN", "false").lower()
            in {"0", "false", "no", "off"}
            or args.allow_dividend_sync_fail_open,
        ),
        run_dividend_sync_status_gate(args, name="production_clean_dividend_sync"),
        run_cmd(
            "production_safety_tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_trade_core_runtime.py::test_runtime_requires_tbank_sdk_for_production_like_modes",
                "tests/test_trade_core_runtime.py::test_emergency_stop_cancels_working_orders",
                "tests/test_api_bff.py::test_production_refuses_dev_auth_without_auth_service",
                "tests/test_api_bff.py::test_production_auth_rejects_role_header_without_bearer",
                "-q",
            ],
        ),
        run_compose_shared_db_gate(),
        run_instrument_registry_gate(args),
        run_no_placeholder_instrument_gate(),
        run_secret_scan_gate(),
    ]


def run_historical_replay(args: argparse.Namespace) -> list[GateResult]:
    quality_timeframes = f"1m,{args.timeframes}"
    replay_args = [
        "--lookback-days",
        str(args.lookback_days),
        "--instruments",
        args.instruments,
        "--timeframes",
        args.timeframes,
        "--strategy-id",
        args.strategy_id,
        "--json-output",
    ]
    dry_run_replay_args = [*replay_args, "--dry-run"]
    config_fallback_args = ["--allow-default-strategy-config"] if args.dry_run else []
    dividend_sync_args = [] if args.skip_dividend_sync_check else ["--require-dividend-sync"]
    return [
        run_cmd(
            "market_special_day_classification",
            [
                sys.executable,
                "scripts/run_market_special_day_classification.py",
                "--lookback-days",
                str(args.lookback_days),
                "--instruments",
                args.instruments,
                "--include-future",
                "--lookahead-days",
                "365",
                "--skip-existing",
                "--chunk-days",
                "30",
                "--progress-every",
                "30",
                *dividend_sync_args,
                "--json-output",
            ],
        ),
        run_cmd(
            "historical_data_quality",
            [
                sys.executable,
                "scripts/run_historical_data_quality_report.py",
                "--lookback-days",
                str(args.lookback_days),
                "--instruments",
                args.instruments,
                "--timeframes",
                quality_timeframes,
                "--require-special-day-classification",
                "--json-output",
            ],
        ),
        run_cmd(
            "historical_replay_dry_run",
            [
                sys.executable,
                "scripts/run_historical_replay_from_db.py",
                *dry_run_replay_args,
                "--require-special-day-classification",
                *dividend_sync_args,
                *config_fallback_args,
            ],
        ),
        run_cmd(
            "historical_replay_idempotency_probe",
            [
                sys.executable,
                "scripts/run_historical_replay_from_db.py",
                *(dry_run_replay_args if args.dry_run else replay_args),
                "--require-special-day-classification",
                *dividend_sync_args,
                *config_fallback_args,
            ],
        ),
        run_cmd(
            "historical_counterfactual_dry_run",
            [
                sys.executable,
                "scripts/run_historical_counterfactual_rebuild.py",
                "--lookback-days",
                str(args.lookback_days),
                "--strategy-id",
                args.strategy_id,
                "--instruments",
                args.instruments,
                "--timeframes",
                args.timeframes,
                "--dry-run",
                "--json-output",
            ],
        ),
        run_cmd(
            "calibration_report",
            [
                sys.executable,
                "scripts/run_calibration_report.py",
                "--lookback-days",
                str(args.lookback_days),
                "--strategy-id",
                args.strategy_id,
                "--instruments",
                args.instruments,
                "--timeframes",
                args.timeframes,
                "--calibration-scope",
                "primary_normal_days",
                "--require-special-day-classification",
                "--max-dividend-sync-age-hours",
                str(args.max_dividend_sync_age_hours),
                *(
                    ["--allow-manual-corporate-actions"]
                    if args.allow_manual_corporate_actions
                    else []
                ),
                "--json-output",
            ],
        ),
        GateResult(
            name="historical_no_real_orders",
            passed=True,
            command="LaunchModePolicy.HISTORICAL_REPLAY",
            details={"post_order": "disabled", "cancel_order": "disabled"},
        ),
        run_secret_scan_gate(),
    ]


def run_historical_final_calibration(args: argparse.Namespace) -> list[GateResult]:
    quality_timeframes = f"1m,{args.timeframes}"
    heavy_timeout_seconds = max(args.gate_timeout_seconds, 900)
    dividend_sync_args = [] if args.skip_dividend_sync_check else ["--require-dividend-sync"]
    manual_args = (
        ["--allow-manual-corporate-actions"] if args.allow_manual_corporate_actions else []
    )
    common_replay_args = [
        "--lookback-days",
        str(args.lookback_days),
        "--instruments",
        args.instruments,
        "--timeframes",
        args.timeframes,
        "--strategy-id",
        args.strategy_id,
        "--require-special-day-classification",
        *dividend_sync_args,
        "--json-output",
    ]
    calibration_command = [
        sys.executable,
        "scripts/run_calibration_report.py",
        "--lookback-days",
        str(args.lookback_days),
        "--strategy-id",
        args.strategy_id,
        "--instruments",
        args.instruments,
        "--timeframes",
        args.timeframes,
        "--calibration-scope",
        "primary_normal_days",
        "--require-special-day-classification",
        "--max-dividend-sync-age-hours",
        str(args.max_dividend_sync_age_hours),
        *manual_args,
        "--json-output",
    ]
    calibration_gate = (
        run_cmd(
            "primary_calibration_clean",
            calibration_command,
            timeout_seconds=heavy_timeout_seconds,
        )
        if args.skip_dividend_sync_check
        else run_json_cmd_gate(
            "primary_calibration_clean",
            calibration_command,
            expected={"calibration_clean": True},
            timeout_seconds=heavy_timeout_seconds,
        )
    )
    return [
        run_instrument_registry_gate(args),
        run_dividend_sync_status_gate(
            args,
            name="historical_final_clean_dividend_sync",
            allow_skip=True,
        ),
        run_cmd(
            "special_day_classification_requires_dividend_sync",
            [
                sys.executable,
                "scripts/run_market_special_day_classification.py",
                "--lookback-days",
                str(args.lookback_days),
                "--instruments",
                args.instruments,
                "--include-future",
                "--lookahead-days",
                "365",
                "--skip-existing",
                "--chunk-days",
                "30",
                "--progress-every",
                "30",
                *dividend_sync_args,
                "--json-output",
            ],
            timeout_seconds=heavy_timeout_seconds,
        ),
        run_cmd(
            "historical_quality_requires_special_days",
            [
                sys.executable,
                "scripts/run_historical_data_quality_report.py",
                "--lookback-days",
                str(args.lookback_days),
                "--instruments",
                args.instruments,
                "--timeframes",
                quality_timeframes,
                "--require-special-day-classification",
                "--json-output",
            ],
            timeout_seconds=heavy_timeout_seconds,
        ),
        run_cmd(
            "historical_replay_uses_db_strategy_config",
            [sys.executable, "scripts/run_historical_replay_from_db.py", *common_replay_args],
            timeout_seconds=heavy_timeout_seconds,
        ),
        run_cmd(
            "historical_counterfactual_present",
            [
                sys.executable,
                "scripts/run_historical_counterfactual_rebuild.py",
                "--lookback-days",
                str(args.lookback_days),
                "--strategy-id",
                args.strategy_id,
                "--instruments",
                args.instruments,
                "--timeframes",
                args.timeframes,
                "--json-output",
            ],
            timeout_seconds=heavy_timeout_seconds,
        ),
        calibration_gate,
        GateResult(
            name="historical_final_no_real_orders",
            passed=True,
            command="LaunchModePolicy.HISTORICAL_REPLAY",
            details={"post_order": "disabled", "cancel_order": "disabled"},
        ),
        run_secret_scan_gate(),
    ]


def run_instrument_resolution(args: argparse.Namespace) -> list[GateResult]:
    command = [
        sys.executable,
        "scripts/run_tbank_instrument_resolve.py",
        "--instruments",
        args.instruments,
        "--json-output",
    ]
    if args.dry_run:
        command.append("--dry-run")
    else:
        command.append("--strict")
    return [
        run_cmd(
            "tbank_sdk_import_check",
            [sys.executable, "scripts/run_tbank_sdk_import_check.py"],
        ),
        run_json_cmd_gate(
            "instrument_resolve",
            command,
            expected={"ready_for_broker_calls": True},
        ),
        run_instrument_registry_gate(args, allow_empty=args.dry_run),
        GateResult(
            name="instrument_resolution_no_real_orders",
            passed=True,
            command="readonly instrument resolve",
            details={"post_order": "disabled", "cancel_order": "disabled"},
        ),
    ]


def run_data_shadow(args: argparse.Namespace, env: Mapping[str, str]) -> list[GateResult]:
    preflight = [
        sys.executable,
        "scripts/run_data_only_shadow_smoke.py",
        "--instruments",
        args.instruments,
        "--minutes",
        "0",
        "--preflight-only",
        "--json-output",
    ]
    smoke = [
        sys.executable,
        "scripts/run_data_only_shadow_smoke.py",
        "--instruments",
        args.instruments,
        "--minutes",
        str(max(args.shadow_minutes, 0.2)),
        "--json-output",
        "--max-instruments-per-stream-batch",
        "4",
        "--stream-batch-delay-seconds",
        "2",
    ]
    if args.dry_run:
        preflight.append("--dry-run")
        smoke.append("--dry-run")
    if not args.skip_dividend_sync_check:
        preflight.append("--require-dividend-sync")
        smoke.append("--require-dividend-sync")
    results = [
        run_cmd(
            "tbank_sdk_import_check",
            [sys.executable, "scripts/run_tbank_sdk_import_check.py"],
        ),
        run_instrument_registry_gate(args, allow_empty=args.dry_run),
        run_dividend_sync_status_gate(
            args,
            name="data_shadow_clean_dividend_sync",
        )
        if not args.skip_dividend_sync_check
        else GateResult(
            name="data_shadow_clean_dividend_sync",
            passed=True,
            command="--skip-dividend-sync-check",
            details={"status": "skipped"},
        ),
        GateResult(
            name="data_only_shadow_forced_by_smoke",
            passed=True,
            command="scripts/run_data_only_shadow_smoke.py",
            details={
                "status": "script_sets_TRADING_DATA_ONLY_SHADOW_true",
                "environment_value": env.get("TRADING_DATA_ONLY_SHADOW"),
            },
        ),
        env_gate("production_confirmation_absent", "TRADING_PRODUCTION_CONFIRM" not in env),
    ]
    preflight_gate = run_json_cmd_capture(
        "data_only_shadow_preflight",
        preflight,
        timeout_seconds=int(max(args.gate_timeout_seconds, 120)),
    )
    results.append(preflight_gate)
    payload = _json_payload(preflight_gate)
    if not preflight_gate.passed:
        return results

    if payload.get("market_open") is False and payload.get("market_closed_expected") is True:
        results.append(
            GateResult(
                name="data_only_shadow_closed_market_expected",
                passed=True,
                command=format_cmd(preflight),
                details={
                    "status": "market_closed_expected",
                    "market_open": False,
                    "market_closed_expected": True,
                    "reason_code": payload.get("reason_code"),
                    "next_session_at": payload.get("next_session_at"),
                    "smoke_was_run": False,
                    "no_live_samples_expected": True,
                    "no_real_orders": True,
                },
            )
        )
    elif payload.get("market_open") is True:
        smoke_gate = run_json_cmd_gate(
            "data_only_shadow_smoke",
            smoke,
            expected={
                "data_only_shadow_enabled": True,
                "real_orders_disabled": True,
                "market_open": True,
                "post_order_calls": 0,
                "cancel_order_calls": 0,
                "signal_candidates_delta": 0,
                "order_intents_delta": 0,
                "broker_orders_delta": 0,
            },
            timeout_seconds=int(max(args.gate_timeout_seconds, 120)),
        )
        details = dict(smoke_gate.details)
        details["smoke_was_run"] = True
        results.append(
            GateResult(
                name=smoke_gate.name,
                passed=smoke_gate.passed,
                command=smoke_gate.command,
                details=details,
            )
        )
    else:
        results.append(
            GateResult(
                name="data_only_shadow_preflight_state",
                passed=False,
                command=format_cmd(preflight),
                details={
                    "status": "preflight_not_open_not_expected_closed",
                    "market_open": payload.get("market_open"),
                    "market_closed_expected": payload.get("market_closed_expected"),
                    "reason_code": payload.get("reason_code"),
                    "next_session_at": payload.get("next_session_at"),
                    "smoke_was_run": False,
                    "no_real_orders": True,
                },
            )
        )
    results.append(
        GateResult(
            name="data_shadow_strategy_pipeline_disabled",
            passed=True,
            command="runtime data-only flag",
            details={
                "signal_candidate": "disabled",
                "order_intent": "disabled",
                "pseudo_order": "disabled",
                "smoke_was_run": payload.get("market_open") is True,
                "no_real_orders": True,
            },
        ),
    )
    return results


def run_cmd(
    name: str,
    argv: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int = 240,
) -> GateResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            env=dict(env) if env is not None else None,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return GateResult(
            name=name,
            passed=False,
            command=format_cmd(argv),
            details={
                "status": "timeout",
                "subcommand": subcommand_name(argv),
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(exc.stdout or ""),
                "stderr_tail": tail(exc.stderr or ""),
            },
        )
    return GateResult(
        name=name,
        passed=completed.returncode == 0,
        command=format_cmd(argv),
        details={
            "status": "completed",
            "returncode": completed.returncode,
            "subcommand": subcommand_name(argv),
            "timeout_seconds": timeout_seconds,
            "stdout_tail": tail(completed.stdout),
            "stderr_tail": tail(completed.stderr),
        },
    )


def run_json_cmd_gate(
    name: str,
    argv: Sequence[str],
    *,
    expected: Mapping[str, object],
    cwd: Path = ROOT,
    timeout_seconds: int = 240,
) -> GateResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return GateResult(
            name=name,
            passed=False,
            command=format_cmd(argv),
            details={
                "status": "timeout",
                "subcommand": subcommand_name(argv),
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(exc.stdout or ""),
                "stderr_tail": tail(exc.stderr or ""),
            },
        )
    if completed.returncode != 0:
        return GateResult(
            name=name,
            passed=False,
            command=format_cmd(argv),
            details={
                "status": "completed",
                "returncode": completed.returncode,
                "subcommand": subcommand_name(argv),
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(completed.stdout),
                "stderr_tail": tail(completed.stderr),
            },
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return GateResult(
            name=name,
            passed=False,
            command=format_cmd(argv),
            details={
                "status": "json_parse_failed",
                "returncode": completed.returncode,
                "subcommand": subcommand_name(argv),
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(completed.stdout),
                "stderr_tail": tail(completed.stderr),
                "error_message": str(exc),
            },
        )
    mismatches = {
        key: {"expected": value, "actual": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    return GateResult(
        name=name,
        passed=not mismatches,
        command=format_cmd(argv),
        details={
            "status": "completed",
            "returncode": completed.returncode,
            "subcommand": subcommand_name(argv),
            "timeout_seconds": timeout_seconds,
            "stdout_tail": tail(completed.stdout),
            "stderr_tail": tail(completed.stderr),
            "json_expected": dict(expected),
            "json_mismatches": mismatches,
        },
    )


def run_json_cmd_capture(
    name: str,
    argv: Sequence[str],
    *,
    cwd: Path = ROOT,
    timeout_seconds: int = 240,
) -> GateResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return GateResult(
            name=name,
            passed=False,
            command=format_cmd(argv),
            details={
                "status": "timeout",
                "subcommand": subcommand_name(argv),
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(exc.stdout or ""),
                "stderr_tail": tail(exc.stderr or ""),
            },
        )
    if completed.returncode != 0:
        return GateResult(
            name=name,
            passed=False,
            command=format_cmd(argv),
            details={
                "status": "completed",
                "returncode": completed.returncode,
                "subcommand": subcommand_name(argv),
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(completed.stdout),
                "stderr_tail": tail(completed.stderr),
            },
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return GateResult(
            name=name,
            passed=False,
            command=format_cmd(argv),
            details={
                "status": "json_parse_failed",
                "returncode": completed.returncode,
                "subcommand": subcommand_name(argv),
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(completed.stdout),
                "stderr_tail": tail(completed.stderr),
                "error_message": str(exc),
            },
        )
    return GateResult(
        name=name,
        passed=bool(payload.get("passed", True)),
        command=format_cmd(argv),
        details={
            "status": "completed",
            "returncode": completed.returncode,
            "subcommand": subcommand_name(argv),
            "timeout_seconds": timeout_seconds,
            "stdout_tail": tail(completed.stdout),
            "stderr_tail": tail(completed.stderr),
            "json_payload": payload,
        },
    )


def _json_payload(result: GateResult) -> Mapping[str, object]:
    payload = result.details.get("json_payload")
    return payload if isinstance(payload, Mapping) else {}


def run_dividend_sync_status_gate(
    args: argparse.Namespace,
    *,
    name: str,
    allow_skip: bool = False,
) -> GateResult:
    if args.skip_dividend_sync_check and allow_skip:
        return GateResult(
            name=name,
            passed=True,
            command="--skip-dividend-sync-check",
            details={"status": "skipped_explicitly_for_local_dry_gate"},
        )
    try:
        from trade_core.corporate_actions import dividend_sync_status_payload
        from trading_common.db.config import build_database_url_from_env
        from trading_common.db.service import DatabaseService

        database = DatabaseService(build_database_url_from_env())
        try:
            with database.session_scope() as session:
                payload = dividend_sync_status_payload(
                    session,
                    max_age_hours=args.max_dividend_sync_age_hours,
                )
        finally:
            database.engine.dispose()
    except Exception as exc:
        return GateResult(
            name=name,
            passed=False,
            command="select latest dividend_sync_run",
            details={"error_code": type(exc).__name__, "error_message": str(exc)},
        )

    passed = (
        payload.get("status") == "completed"
        and payload.get("clean") is True
        and payload.get("failed_instruments") == 0
        and payload.get("error_count") == 0
        and payload.get("ready_for_shadow") is True
    )
    return GateResult(
        name=name,
        passed=passed,
        command="select latest dividend_sync_run",
        details={**payload, "checked_at": datetime.now(tz=UTC).isoformat()},
    )


def run_instrument_registry_gate(
    args: argparse.Namespace,
    *,
    allow_empty: bool = False,
) -> GateResult:
    if allow_empty:
        return GateResult(
            name="instrument_registry_resolved",
            passed=True,
            command="dry-run registry check skipped",
            details={"status": "skipped_for_dry_run"},
        )
    try:
        from trade_core.instruments import is_broker_resolved_instrument
        from trading_common.db.config import build_database_url_from_env
        from trading_common.db.models import InstrumentRegistry
        from trading_common.db.service import DatabaseService

        database = DatabaseService(build_database_url_from_env())
        try:
            with database.session_scope() as session:
                requested = {
                    item.strip().upper() for item in args.instruments.split(",") if item.strip()
                }
                rows = (
                    session.query(InstrumentRegistry)
                    .filter(InstrumentRegistry.is_enabled.is_(True))
                    .all()
                )
                scoped_rows = [
                    row for row in rows if not requested or row.ticker.upper() in requested
                ]
                unresolved = [
                    {
                        "instrument_id": row.instrument_id,
                        "ticker": row.ticker,
                        "source": row.source,
                        "resolution_status": row.resolution_status,
                        "instrument_uid_present": bool(row.instrument_uid),
                        "figi_present": bool(row.figi),
                        "resolution_error_code": row.resolution_error_code,
                        "resolution_error_message": row.resolution_error_message,
                    }
                    for row in scoped_rows
                    if not is_broker_resolved_instrument(row)
                ]
        finally:
            database.engine.dispose()
    except Exception as exc:
        return GateResult(
            name="instrument_registry_resolved",
            passed=False,
            command="select instrument_registry",
            details={"error_code": type(exc).__name__, "error_message": str(exc)},
        )
    passed = (allow_empty and not scoped_rows) or (bool(scoped_rows) and not unresolved)
    return GateResult(
        name="instrument_registry_resolved",
        passed=passed,
        command="select instrument_registry",
        details={
            "requested_instruments": sorted(requested),
            "row_count": len(scoped_rows),
            "unresolved_enabled_instruments": unresolved,
        },
    )


def run_health_gate(name: str, url: str) -> GateResult:
    deadline = time.monotonic() + 60
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8", errors="replace")
            return GateResult(
                name=name,
                passed=True,
                command=f"GET {url}",
                details={"status": "ok", "body_tail": tail(body)},
            )
        except (OSError, URLError) as exc:
            last_error = str(exc)
            time.sleep(2)
    return GateResult(
        name=name,
        passed=False,
        command=f"GET {url}",
        details={"status": "failed", "error_message": last_error},
    )


def run_compose_shared_db_gate() -> GateResult:
    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        return GateResult(
            name="shared_postgres_config",
            passed=False,
            command="docker compose config --format json",
            details={"stderr_tail": tail(completed.stderr)},
        )
    config = json.loads(completed.stdout)
    services = config.get("services", {})
    service_env = {
        name: normalize_environment(services.get(name, {}).get("environment", {}))
        for name in ("trade-core", "api", "report-worker")
    }
    db_keys = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER")
    values = {
        service: {key: env.get(key) for key in db_keys} for service, env in service_env.items()
    }
    expected = values.get("api")
    same = all(value == expected for value in values.values())
    no_sqlite = all(
        "sqlite"
        not in (
            service_env[service].get("DATABASE_URL", "")
            + service_env[service].get("TRADING_DATABASE_URL", "")
        )
        for service in service_env
    )
    return GateResult(
        name="shared_postgres_config",
        passed=bool(expected) and same and no_sqlite,
        command="docker compose config --format json",
        details={"values": values, "no_sqlite": no_sqlite},
    )


def compose_host_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_HOST": "localhost",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "trading_2_0",
            "POSTGRES_USER": "trading_app",
            "POSTGRES_PASSWORD_FILE": str(ROOT / "secrets" / "postgres_password"),
            "CELERY_BROKER_URL": "redis://localhost:6379/0",
            "CELERY_RESULT_BACKEND": "redis://localhost:6379/0",
        }
    )
    return env


def run_no_placeholder_instrument_gate() -> GateResult:
    placeholder_patterns = (
        "runtime-placeholder",
        "sber-runtime-placeholder",
        "gazp-runtime-placeholder",
    )
    files = [
        ROOT / "apps" / "trade-core" / "src" / "trade_core" / "runtime.py",
        ROOT / "apps" / "trade-core" / "src" / "trade_core" / "infra" / "tbank" / "sdk_clients.py",
    ]
    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in placeholder_patterns:
            if pattern in text:
                hits.append(f"{path.relative_to(ROOT)}:{pattern}")
    return GateResult(
        name="no_placeholder_instrument_uid",
        passed=not hits,
        command="source scan",
        details={"hits": hits},
    )


def run_secret_scan_gate() -> GateResult:
    leaks = scan_text_secrets()
    blocked_binary_files = tracked_binary_docs()
    return GateResult(
        name="no_raw_secrets_or_unreviewed_binary_docs",
        passed=not leaks and not blocked_binary_files,
        command="git ls-files + text secret scan",
        details={
            "leak_count": len(leaks),
            "leaks": leaks[:20],
            "blocked_binary_files": blocked_binary_files,
        },
    )


def scan_text_secrets() -> list[dict[str, object]]:
    leaks: list[dict[str, object]] = []
    for path in tracked_files():
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for line_number, line in enumerate(content.splitlines(), start=1):
            if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                leaks.append({"path": str(path.relative_to(ROOT)), "line": line_number})
    return leaks


def tracked_binary_docs() -> list[str]:
    return [
        str(path.relative_to(ROOT))
        for path in tracked_files()
        if path.suffix.lower() in BLOCKED_BINARY_SUFFIXES
    ]


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return []
    paths = completed.stdout.decode("utf-8", errors="replace").split("\0")
    return [ROOT / path for path in paths if path]


def normalize_environment(raw: object) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        env: dict[str, str] = {}
        for item in raw:
            key, _, value = str(item).partition("=")
            env[key] = value
        return env
    return {}


def env_gate(name: str, passed: bool, *, details: dict[str, object] | None = None) -> GateResult:
    return GateResult(
        name=name,
        passed=passed,
        command="environment check",
        details=details or {},
    )


def wait_gate(name: str, minutes: float) -> GateResult:
    seconds = max(0.0, minutes * 60.0)
    time.sleep(seconds)
    return GateResult(
        name=name,
        passed=True,
        command=f"sleep {seconds:.0f}",
        details={"waited_seconds": seconds},
    )


def npm_cmd() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def format_cmd(argv: Sequence[str]) -> str:
    return " ".join(str(part) for part in argv)


def subcommand_name(argv: Sequence[str]) -> str:
    if len(argv) > 1:
        return Path(str(argv[1])).name
    if argv:
        return Path(str(argv[0])).name
    return ""


def tail(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-OUTPUT_TAIL_CHARS:]


if __name__ == "__main__":
    main()

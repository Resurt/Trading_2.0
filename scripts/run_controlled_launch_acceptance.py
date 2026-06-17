"""Run controlled-launch acceptance gates without placing real broker orders."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADING_DATE = "2026-06-12"
DEFAULT_STRATEGY_ID = "baseline"
OUTPUT_TAIL_CHARS = 4000

SECRET_FILE_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
SECRET_FILE_SUFFIXES = {
    ".bat",
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".js",
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
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"t\.CUtCVmpY"),
    re.compile(r"\bTINVEST_TOKEN\s*=\s*t\.", re.IGNORECASE),
    re.compile(r"\bTBANK_(?:FULL_ACCESS|READONLY)_TOKEN\s*=\s*t\.", re.IGNORECASE),
    re.compile(r"\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
    re.compile(r"\b(?:full_access_token|readonly_token)\s*=\s*['\"]t\.", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """One controlled-launch gate result."""

    name: str
    passed: bool
    command: str
    details: dict[str, object]


def main() -> None:
    args = parse_args()
    results: list[GateResult] = []

    if not args.skip_full_check:
        results.append(
            run_subprocess_gate(
                "python_scripts_check",
                [sys.executable, "scripts/check.py"],
                timeout_seconds=args.full_check_timeout,
            )
        )
    results.append(
        run_subprocess_gate(
            "analytics_smoke",
            [
                sys.executable,
                "scripts/run_logging_analytics_acceptance.py",
                "--date",
                args.trading_date,
                "--strategy-id",
                args.strategy_id,
            ],
        )
    )
    results.append(
        run_subprocess_gate(
            "report_rebuild",
            [
                sys.executable,
                "scripts/run_report_rebuild.py",
                "--date",
                args.trading_date,
                "--strategy-id",
                args.strategy_id,
            ],
        )
    )
    results.append(
        run_subprocess_gate(
            "replay_day",
            [sys.executable, "scripts/run_replay_day.py", "--date", args.trading_date],
        )
    )
    if not args.skip_docker:
        results.append(
            run_subprocess_gate(
                "docker_compose_config",
                ["docker", "compose", "config", "--quiet"],
                timeout_seconds=120,
            )
        )
    results.append(run_sqlite_migration_gate())
    results.append(
        run_subprocess_gate(
            "sandbox_dry_run",
            [sys.executable, "scripts/run_sandbox_smoke.py", "--dry-run"],
        )
    )
    results.append(
        run_subprocess_gate(
            "production_safety_guards",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_controlled_launch.py::test_launch_mode_default_and_production_confirmation",
                "tests/test_trade_core_runtime.py::test_runtime_rejects_unconfirmed_production",
                "tests/test_api_bff.py::test_production_refuses_dev_auth_without_auth_service",
                "-q",
            ],
        )
    )
    results.append(run_secret_scan_gate(ROOT))

    passed = all(result.passed for result in results)
    payload = {
        "passed": passed,
        "trading_date": args.trading_date,
        "strategy_id": args.strategy_id,
        "gates": [asdict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="trading_date", default=DEFAULT_TRADING_DATE)
    parser.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID)
    parser.add_argument(
        "--skip-full-check",
        action="store_true",
        help="Skip python scripts/check.py when a fast local acceptance pass is enough.",
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="Skip docker compose config validation when Docker is unavailable.",
    )
    parser.add_argument("--full-check-timeout", type=int, default=240)
    return parser.parse_args()


def run_subprocess_gate(
    name: str,
    argv: Sequence[str],
    *,
    timeout_seconds: int = 180,
) -> GateResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return GateResult(
            name=name,
            passed=False,
            command=format_command(argv),
            details={
                "status": "timeout",
                "timeout_seconds": timeout_seconds,
                "stdout_tail": tail(exc.stdout or ""),
                "stderr_tail": tail(exc.stderr or ""),
            },
        )

    return GateResult(
        name=name,
        passed=completed.returncode == 0,
        command=format_command(argv),
        details={
            "status": "completed",
            "returncode": completed.returncode,
            "stdout_tail": tail(completed.stdout),
            "stderr_tail": tail(completed.stderr),
        },
    )


def run_sqlite_migration_gate() -> GateResult:
    with TemporaryDirectory(prefix="trading-migration-") as temp_dir:
        database_path = Path(temp_dir) / "migration-smoke.db"
        database_url = f"sqlite:///{database_path}"
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "packages" / "common" / "alembic"))
        config.set_main_option("sqlalchemy.url", database_url)
        try:
            command.upgrade(config, "head")
            command.downgrade(config, "-1")
            command.upgrade(config, "head")
        except Exception as exc:
            return GateResult(
                name="migration_upgrade_downgrade_upgrade",
                passed=False,
                command="alembic upgrade head; alembic downgrade -1; alembic upgrade head",
                details={
                    "database": "sqlite",
                    "status": "failed",
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
    return GateResult(
        name="migration_upgrade_downgrade_upgrade",
        passed=True,
        command="alembic upgrade head; alembic downgrade -1; alembic upgrade head",
        details={"database": "sqlite", "status": "completed"},
    )


def run_secret_scan_gate(root: Path) -> GateResult:
    leaks = secret_scan(root)
    blocked_binary_files = tracked_binary_docs(root)
    return GateResult(
        name="no_raw_secrets",
        passed=not leaks and not blocked_binary_files,
        command="python secret scan",
        details={
            "status": "completed",
            "leak_count": len(leaks),
            "leaks": leaks[:20],
            "blocked_binary_files": blocked_binary_files,
        },
    )


def secret_scan(root: Path) -> list[dict[str, object]]:
    leaks: list[dict[str, object]] = []
    for path in iter_secret_scan_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                leaks.append(
                    {
                        "path": str(path.relative_to(root)),
                        "line": line_number,
                    }
                )
    return leaks


def iter_secret_scan_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SECRET_FILE_SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in SECRET_FILE_SUFFIXES:
            continue
        files.append(path)
    return files


def tracked_binary_docs(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return []
    paths = completed.stdout.decode("utf-8", errors="replace").split("\0")
    return [
        path
        for path in paths
        if Path(path).suffix.lower() in BLOCKED_BINARY_SUFFIXES
    ]


def format_command(argv: Sequence[str]) -> str:
    return " ".join(str(part) for part in argv)


def tail(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-OUTPUT_TAIL_CHARS:]


if __name__ == "__main__":
    main()

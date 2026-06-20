"""Run local smoke checks without requiring make."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "apps" / "frontend"


def run(command: list[str], cwd: Path = ROOT) -> None:
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def main() -> None:
    run([sys.executable, "-m", "pytest"])
    run([sys.executable, "-m", "ruff", "check", "."])
    run([sys.executable, "-m", "mypy"])
    run([sys.executable, "scripts/run_frontend_text_encoding_check.py"])
    run([npm_command(), "run", "typecheck"], cwd=FRONTEND)
    run([npm_command(), "run", "test:unit"], cwd=FRONTEND)
    run([npm_command(), "run", "build"], cwd=FRONTEND)


if __name__ == "__main__":
    main()

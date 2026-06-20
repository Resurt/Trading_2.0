"""Fail if frontend source contains common UTF-8/Windows mojibake."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "apps" / "frontend" / "src"


def _sequence(*codepoints: int) -> str:
    return "".join(chr(codepoint) for codepoint in codepoints)


MOJIBAKE_PATTERNS: tuple[str, ...] = (
    _sequence(0x0420, 0x045C),  # "Рќ"
    _sequence(0x0420, 0x045F),  # "Рџ"
    _sequence(0x0420, 0x045B),  # "Рћ"
    _sequence(0x0420, 0x201D),  # "Р”"
    _sequence(0x0420, 0x203A),  # "Р›"
    _sequence(0x0420, 0x00B5),  # "Рµ"
    _sequence(0x0420, 0x0405),  # "РЅ"
    _sequence(0x0420, 0x0451),  # "Рё"
    _sequence(0x0420, 0x00B0),  # "Р°"
    _sequence(0x0420, 0x0454),  # "Рє"
    _sequence(0x0421, 0x0403),  # "СЃ"
    _sequence(0x0421, 0x201A),  # "С‚"
    _sequence(0x0421, 0x040A),  # "СЊ"
    _sequence(0x0421, 0x2039),  # "С‹"
    _sequence(0x0421, 0x2021),  # "С‡"
    _sequence(0x0421, 0x2030),  # "С‰"
    chr(0x00D0),  # "Ð"
    chr(0x00D1),  # "Ñ"
)


def iter_frontend_files(root: Path = FRONTEND_SRC) -> list[Path]:
    return sorted(
        path
        for suffix in ("*.vue", "*.ts")
        for path in root.rglob(suffix)
        if path.is_file()
    )


def find_mojibake(root: Path = FRONTEND_SRC) -> list[str]:
    findings: list[str] = []
    for path in iter_frontend_files(root):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            matched = [pattern for pattern in MOJIBAKE_PATTERNS if pattern in line]
            if matched:
                relative = path.relative_to(ROOT)
                escaped = ", ".join(
                    pattern.encode("unicode_escape").decode("ascii")
                    for pattern in matched
                )
                findings.append(f"{relative}:{line_number}: mojibake pattern(s): {escaped}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=FRONTEND_SRC,
        help="Frontend source root to scan.",
    )
    args = parser.parse_args()

    findings = find_mojibake(args.root)
    if findings:
        print("Frontend mojibake detected:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print("Frontend text encoding check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

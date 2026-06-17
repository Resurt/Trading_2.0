from __future__ import annotations

from pathlib import Path

from scripts.run_controlled_launch_acceptance import run_sqlite_migration_gate, secret_scan


def test_controlled_launch_sqlite_migration_gate_runs_upgrade_downgrade_upgrade() -> None:
    result = run_sqlite_migration_gate()

    assert result.passed
    assert result.name == "migration_upgrade_downgrade_upgrade"
    assert result.details["database"] == "sqlite"


def test_controlled_launch_secret_scan_detects_raw_tbank_token_assignment(
    tmp_path: Path,
) -> None:
    leaking_file = tmp_path / "local.env"
    leaking_file.write_text(
        "TINVEST_" + "TOKEN=" + "t.fake_raw_token_value\n",
        encoding="utf-8",
    )

    leaks = secret_scan(tmp_path)

    assert leaks == [{"path": "local.env", "line": 1}]


def test_controlled_launch_secret_scan_allows_variable_name_documentation(tmp_path: Path) -> None:
    docs_file = tmp_path / "README.md"
    docs_file.write_text("Use TINVEST_TOKEN only as a local env name.\n", encoding="utf-8")

    assert secret_scan(tmp_path) == []

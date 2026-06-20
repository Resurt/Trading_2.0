from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence

import pytest
import scripts.run_launch_readiness as readiness


def args() -> argparse.Namespace:
    return argparse.Namespace(
        instruments="SBER,GAZP",
        shadow_minutes=10,
        dry_run=False,
        skip_dividend_sync_check=True,
        gate_timeout_seconds=120,
    )


def passing_gate(name: str) -> readiness.GateResult:
    return readiness.GateResult(
        name=name,
        passed=True,
        command=name,
        details={"status": "test_passed"},
    )


def install_common_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        readiness,
        "run_cmd",
        lambda name, argv, **kwargs: passing_gate(name),
    )
    monkeypatch.setattr(
        readiness,
        "run_instrument_registry_gate",
        lambda launch_args, allow_empty=False: passing_gate("instrument_registry_resolved"),
    )
    monkeypatch.setattr(
        readiness,
        "env_gate",
        lambda name, passed, details=None: readiness.GateResult(
            name=name,
            passed=bool(passed),
            command=name,
            details=dict(details or {}),
        ),
    )


def test_data_shadow_readiness_closed_market_passes_without_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_common_gates(monkeypatch)
    smoke_calls: list[Sequence[str]] = []
    monkeypatch.setattr(
        readiness,
        "run_json_cmd_capture",
        lambda name, argv, **kwargs: readiness.GateResult(
            name=name,
            passed=True,
            command=readiness.format_cmd(argv),
            details={
                "json_payload": {
                    "passed": True,
                    "market_open": False,
                    "market_closed_expected": True,
                    "reason_code": "weekend_session_closed",
                    "next_session_at": "2026-06-21T10:00:00+03:00",
                }
            },
        ),
    )
    monkeypatch.setattr(
        readiness,
        "run_json_cmd_gate",
        _closed_market_smoke_gate(smoke_calls),
    )

    results = readiness.run_data_shadow(args(), {})

    assert all(result.passed for result in results)
    assert smoke_calls == []
    closed_gate = next(
        result
        for result in results
        if result.name == "data_only_shadow_closed_market_expected"
    )
    assert closed_gate.details["smoke_was_run"] is False
    assert closed_gate.details["no_live_samples_expected"] is True


def _closed_market_smoke_gate(
    smoke_calls: list[Sequence[str]],
) -> object:
    def fake_smoke_gate(name: str, argv: Sequence[str], **kwargs: object) -> readiness.GateResult:
        del kwargs
        smoke_calls.append(argv)
        return passing_gate(name)

    return fake_smoke_gate


def test_data_shadow_readiness_open_market_runs_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_common_gates(monkeypatch)
    smoke_calls: list[Sequence[str]] = []
    monkeypatch.setattr(
        readiness,
        "run_json_cmd_capture",
        lambda name, argv, **kwargs: readiness.GateResult(
            name=name,
            passed=True,
            command=readiness.format_cmd(argv),
            details={
                "json_payload": {
                    "passed": True,
                    "market_open": True,
                    "market_closed_expected": False,
                    "reason_code": "market_open",
                }
            },
        ),
    )

    def fake_smoke_gate(
        name: str,
        argv: Sequence[str],
        *,
        expected: Mapping[str, object],
        timeout_seconds: int,
    ) -> readiness.GateResult:
        del expected, timeout_seconds
        smoke_calls.append(argv)
        return passing_gate(name)

    monkeypatch.setattr(readiness, "run_json_cmd_gate", fake_smoke_gate)

    results = readiness.run_data_shadow(args(), {})

    assert all(result.passed for result in results)
    assert smoke_calls
    smoke_gate = next(result for result in results if result.name == "data_only_shadow_smoke")
    assert smoke_gate.details["smoke_was_run"] is True

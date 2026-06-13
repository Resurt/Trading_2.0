"""Sandbox launch smoke checks for T-Bank adapter configuration."""

from __future__ import annotations

from dataclasses import dataclass

from trade_core.infra.tbank.config import TBankBrokerConfig, TBankEnvironment
from trade_core.infra.tbank.secrets import TBankTokenBundle
from trading_common import LaunchModePolicy, RuntimeMode


@dataclass(frozen=True, slots=True)
class SandboxSmokePlan:
    """Result of validating a sandbox adapter smoke scenario."""

    mode: RuntimeMode
    target: str
    app_name: str
    full_access_token_configured: bool
    readonly_token_configured: bool
    dry_run: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "target": self.target,
            "app_name": self.app_name,
            "full_access_token_configured": self.full_access_token_configured,
            "readonly_token_configured": self.readonly_token_configured,
            "dry_run": self.dry_run,
            "note": "sandbox smoke validates wiring; it is not real execution-quality evidence",
        }


def build_sandbox_smoke_plan(
    *,
    policy: LaunchModePolicy,
    config: TBankBrokerConfig,
    tokens: TBankTokenBundle,
    dry_run: bool,
) -> SandboxSmokePlan:
    """Validate that sandbox smoke cannot accidentally use live order transport."""

    if policy.mode is not RuntimeMode.SANDBOX:
        msg = "sandbox smoke requires TRADING_RUNTIME_MODE=sandbox"
        raise RuntimeError(msg)
    if config.environment is not TBankEnvironment.SANDBOX:
        msg = "sandbox smoke requires TBankBrokerConfig.environment=sandbox"
        raise RuntimeError(msg)
    if not dry_run and not tokens.full_access_token:
        msg = "sandbox smoke with broker calls requires full-access sandbox token"
        raise RuntimeError(msg)

    return SandboxSmokePlan(
        mode=policy.mode,
        target=config.target,
        app_name=config.app_name,
        full_access_token_configured=tokens.full_access_token is not None,
        readonly_token_configured=(
            tokens.readonly_token is not None or tokens.full_access_token is not None
        ),
        dry_run=dry_run,
    )

"""Controlled launch modes and safety gates shared by backend services."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from trading_common.enums import RuntimeMode

TRADING_RUNTIME_MODE_ENV = "TRADING_RUNTIME_MODE"
PRODUCTION_CONFIRM_ENV = "TRADING_PRODUCTION_CONFIRM"
PRODUCTION_CONFIRM_VALUE = "I_UNDERSTAND_LIVE_ORDERS"


@dataclass(frozen=True, slots=True)
class LaunchModePolicy:
    """Machine-readable behavior switches for one runtime mode."""

    mode: RuntimeMode
    broker_environment: str
    market_data_source: str
    allows_real_orders: bool
    uses_pseudo_orders: bool
    writes_domain_events: bool
    writes_reports: bool
    requires_full_access_token: bool
    requires_readonly_token: bool
    production_confirmed: bool = False

    @classmethod
    def from_mode(
        cls,
        mode: RuntimeMode,
        *,
        production_confirmed: bool = False,
    ) -> LaunchModePolicy:
        """Build a policy for a known mode without reading process env."""

        if mode is RuntimeMode.HISTORICAL_REPLAY:
            return cls(
                mode=mode,
                broker_environment="none",
                market_data_source="replay_fixtures",
                allows_real_orders=False,
                uses_pseudo_orders=True,
                writes_domain_events=True,
                writes_reports=True,
                requires_full_access_token=False,
                requires_readonly_token=False,
                production_confirmed=False,
            )
        if mode is RuntimeMode.SANDBOX:
            return cls(
                mode=mode,
                broker_environment="sandbox",
                market_data_source="tbank_sandbox",
                allows_real_orders=True,
                uses_pseudo_orders=False,
                writes_domain_events=True,
                writes_reports=True,
                requires_full_access_token=True,
                requires_readonly_token=True,
                production_confirmed=False,
            )
        if mode is RuntimeMode.SHADOW:
            return cls(
                mode=mode,
                broker_environment="live",
                market_data_source="tbank_live",
                allows_real_orders=False,
                uses_pseudo_orders=True,
                writes_domain_events=True,
                writes_reports=True,
                requires_full_access_token=False,
                requires_readonly_token=True,
                production_confirmed=False,
            )
        return cls(
            mode=mode,
            broker_environment="live",
            market_data_source="tbank_live",
            allows_real_orders=True,
            uses_pseudo_orders=False,
            writes_domain_events=True,
            writes_reports=True,
            requires_full_access_token=True,
            requires_readonly_token=True,
            production_confirmed=production_confirmed,
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> LaunchModePolicy:
        """Read launch mode from env. Default is deliberately non-production."""

        env = environ if environ is not None else os.environ
        mode = parse_runtime_mode(env.get(TRADING_RUNTIME_MODE_ENV))
        production_confirmed = env.get(PRODUCTION_CONFIRM_ENV) == PRODUCTION_CONFIRM_VALUE
        policy = cls.from_mode(mode, production_confirmed=production_confirmed)
        policy.validate_startup()
        return policy

    @property
    def order_submission_mode(self) -> str:
        if self.allows_real_orders:
            return "broker"
        if self.mode is RuntimeMode.HISTORICAL_REPLAY:
            return "replay_pseudo_order"
        return "shadow_pseudo_order"

    @property
    def real_order_block_reason_code(self) -> str | None:
        if self.allows_real_orders:
            return None
        if self.mode is RuntimeMode.HISTORICAL_REPLAY:
            return "historical_replay_no_broker_orders"
        return "shadow_mode_no_broker_orders"

    def validate_startup(self) -> None:
        """Raise before startup if a dangerous mode is not explicitly confirmed."""

        if self.mode is RuntimeMode.PRODUCTION and not self.production_confirmed:
            msg = (
                "production mode requires "
                f"{PRODUCTION_CONFIRM_ENV}={PRODUCTION_CONFIRM_VALUE}"
            )
            raise RuntimeError(msg)

    def assert_real_orders_allowed(self) -> None:
        """Guard code paths that would place or cancel a real broker order."""

        if not self.allows_real_orders:
            msg = (
                f"real broker orders are disabled in {self.mode.value}; "
                f"reason_code={self.real_order_block_reason_code}"
            )
            raise RuntimeError(msg)

    def as_payload(self) -> dict[str, object]:
        """Serializable launch-mode payload for logs, health and audit rows."""

        return {
            "mode": self.mode.value,
            "broker_environment": self.broker_environment,
            "market_data_source": self.market_data_source,
            "allows_real_orders": self.allows_real_orders,
            "uses_pseudo_orders": self.uses_pseudo_orders,
            "writes_domain_events": self.writes_domain_events,
            "writes_reports": self.writes_reports,
            "requires_full_access_token": self.requires_full_access_token,
            "requires_readonly_token": self.requires_readonly_token,
            "order_submission_mode": self.order_submission_mode,
            "real_order_block_reason_code": self.real_order_block_reason_code,
        }


def parse_runtime_mode(value: str | RuntimeMode | None) -> RuntimeMode:
    """Parse runtime mode with safe historical replay default."""

    if value is None or value == "":
        return RuntimeMode.HISTORICAL_REPLAY
    if isinstance(value, RuntimeMode):
        return value
    return RuntimeMode(value)

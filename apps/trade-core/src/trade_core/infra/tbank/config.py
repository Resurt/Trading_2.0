"""Configuration for T-Bank broker connections."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class TBankEnvironment(StrEnum):
    """Supported T-Invest API environments."""

    LIVE = "live"
    SANDBOX = "sandbox"


LIVE_TARGET = "invest-public-api.tbank.ru:443"
SANDBOX_TARGET = "sandbox-invest-public-api.tbank.ru:443"
DEFAULT_APP_NAME = "Resurt.Trading_2_0"


@dataclass(frozen=True, slots=True)
class TBankBrokerConfig:
    """Runtime config for the T-Bank adapter."""

    environment: TBankEnvironment
    app_name: str = DEFAULT_APP_NAME
    live_target: str = LIVE_TARGET
    sandbox_target: str = SANDBOX_TARGET
    max_retry_attempts: int = 3
    backoff_initial_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 60.0
    stream_ping_timeout_seconds: float = 180.0
    stream_ping_interval_seconds: float = 120.0

    @property
    def target(self) -> str:
        if self.environment is TBankEnvironment.SANDBOX:
            return self.sandbox_target
        return self.live_target

    @classmethod
    def from_env(cls) -> TBankBrokerConfig:
        environment = TBankEnvironment(os.getenv("TBANK_ENVIRONMENT", TBankEnvironment.SANDBOX))
        return cls(
            environment=environment,
            app_name=os.getenv("TBANK_APP_NAME", DEFAULT_APP_NAME),
            live_target=os.getenv("TBANK_LIVE_TARGET", LIVE_TARGET),
            sandbox_target=os.getenv("TBANK_SANDBOX_TARGET", SANDBOX_TARGET),
            max_retry_attempts=int(os.getenv("TBANK_MAX_RETRY_ATTEMPTS", "3")),
        )

"""Safe T-Bank token loading from Docker secrets with local dev fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from trading_common import LaunchModePolicy, RuntimeMode

FULL_ACCESS_TOKEN_FILE_ENV = "TBANK_FULL_ACCESS_TOKEN_FILE"
READONLY_TOKEN_FILE_ENV = "TBANK_READONLY_TOKEN_FILE"
FULL_ACCESS_TOKEN_ENV = "TBANK_FULL_ACCESS_TOKEN"
READONLY_TOKEN_ENV = "TBANK_READONLY_TOKEN"
LEGACY_DEV_TOKEN_ENV = "TINVEST_TOKEN"
DEFAULT_FULL_ACCESS_TOKEN_FILE = "/run/secrets/tbank_full_access_token"
DEFAULT_READONLY_TOKEN_FILE = "/run/secrets/tbank_readonly_token"
LOCAL_DEV_FULL_ACCESS_TOKEN_FILE = "secrets/tbank_full_access_token"
LOCAL_DEV_READONLY_TOKEN_FILE = "secrets/tbank_readonly_token"


@dataclass(frozen=True, slots=True)
class TBankTokenBundle:
    """Loaded T-Bank tokens. Values must never be logged."""

    full_access_token: str | None
    readonly_token: str | None

    def token_for_trading(self) -> str:
        if self.full_access_token:
            return self.full_access_token
        msg = "T-Bank full-access token is required for trading methods."
        raise RuntimeError(msg)

    def token_for_readonly(self) -> str:
        if self.readonly_token:
            return self.readonly_token
        if self.full_access_token:
            return self.full_access_token
        msg = "T-Bank readonly or full-access token is required for market data methods."
        raise RuntimeError(msg)


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    secret_path = Path(path)
    if not secret_path.exists():
        return None
    value = secret_path.read_text(encoding="utf-8").strip()
    return value or None


def _load_token(
    file_env: str,
    default_file: str,
    value_env: str,
    local_dev_file: str,
) -> str | None:
    token_from_file = _read_secret_file(os.getenv(file_env, default_file))
    if token_from_file:
        return token_from_file
    if file_env not in os.environ:
        token_from_local_dev_file = _read_secret_file(local_dev_file)
        if token_from_local_dev_file:
            return token_from_local_dev_file
    return os.getenv(value_env) or None


def load_tbank_tokens(*, allow_legacy_dev_token: bool = True) -> TBankTokenBundle:
    """Load tokens from Docker secrets first, then env fallback for local dev."""

    full_access_token = _load_token(
        FULL_ACCESS_TOKEN_FILE_ENV,
        DEFAULT_FULL_ACCESS_TOKEN_FILE,
        FULL_ACCESS_TOKEN_ENV,
        LOCAL_DEV_FULL_ACCESS_TOKEN_FILE,
    )
    readonly_token = _load_token(
        READONLY_TOKEN_FILE_ENV,
        DEFAULT_READONLY_TOKEN_FILE,
        READONLY_TOKEN_ENV,
        LOCAL_DEV_READONLY_TOKEN_FILE,
    )

    if allow_legacy_dev_token and not full_access_token and not readonly_token:
        legacy_token = os.getenv(LEGACY_DEV_TOKEN_ENV) or None
        full_access_token = legacy_token
        readonly_token = legacy_token

    return TBankTokenBundle(
        full_access_token=full_access_token,
        readonly_token=readonly_token,
    )


def load_tbank_tokens_for_launch(policy: LaunchModePolicy) -> TBankTokenBundle:
    """Load and validate tokens according to the controlled launch policy."""

    if policy.mode is RuntimeMode.PRODUCTION:
        tokens = load_tbank_tokens_from_files()
    else:
        tokens = load_tbank_tokens(allow_legacy_dev_token=True)
    if policy.requires_full_access_token and not tokens.full_access_token:
        msg = f"{policy.mode.value} mode requires a full-access T-Bank token"
        raise RuntimeError(msg)
    if policy.requires_readonly_token and not (
        tokens.readonly_token or tokens.full_access_token
    ):
        msg = f"{policy.mode.value} mode requires a readonly or full-access T-Bank token"
        raise RuntimeError(msg)
    return tokens


def load_tbank_tokens_from_files() -> TBankTokenBundle:
    """Load production tokens from secret files only, without env value fallback."""

    return TBankTokenBundle(
        full_access_token=_read_secret_file(
            os.getenv(FULL_ACCESS_TOKEN_FILE_ENV, DEFAULT_FULL_ACCESS_TOKEN_FILE)
        ),
        readonly_token=_read_secret_file(
            os.getenv(READONLY_TOKEN_FILE_ENV, DEFAULT_READONLY_TOKEN_FILE)
        ),
    )

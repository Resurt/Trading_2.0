"""Production-safe auth abstraction for the FastAPI BFF control plane."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException, Request, WebSocket, status

from trading_api.schemas import ApiRole
from trading_common import RuntimeMode

AUTH_MODE_ENV = "TRADING_AUTH_MODE"
DEV_AUTH_MODE = "dev"
STATIC_BEARER_AUTH_MODE = "static_bearer"


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Authenticated API principal and its control-plane role."""

    role: ApiRole
    subject: str
    auth_mode: str


class AuthProvider(Protocol):
    """Auth provider interface decoupled from FastAPI route code."""

    @property
    def auth_mode(self) -> str:
        """Name of the active auth mode."""
        ...

    def authenticate(self, request: Request | WebSocket) -> AuthContext:
        """Return an authenticated context or raise/close with an auth error."""


@dataclass(frozen=True, slots=True)
class DevHeaderAuthProvider:
    """Local-dev auth provider based on explicit headers.

    This provider is deliberately unavailable in production mode. It preserves
    the existing frontend/dev contract while keeping production startup strict.
    """

    auth_mode: str = DEV_AUTH_MODE

    def authenticate(self, request: Request | WebSocket) -> AuthContext:
        raw_role = request.headers.get("X-API-Role")
        raw_actor = request.headers.get("X-API-Actor")
        role = ApiRole.OBSERVER
        if raw_role:
            try:
                role = ApiRole(raw_role.strip().lower())
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unknown API role",
                ) from exc
        return AuthContext(
            role=role,
            subject=(raw_actor or f"local-dev:{role.value}").strip(),
            auth_mode=self.auth_mode,
        )


@dataclass(frozen=True, slots=True)
class StaticBearerAuthProvider:
    """Static bearer-token provider for production-like deployments."""

    token_to_role: Mapping[str, ApiRole]
    auth_mode: str = STATIC_BEARER_AUTH_MODE

    def authenticate(self, request: Request | WebSocket) -> AuthContext:
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token is required",
            )
        role = self.token_to_role.get(token)
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
            )
        return AuthContext(
            role=role,
            subject=f"static-token:{role.value}",
            auth_mode=self.auth_mode,
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        strict: bool,
    ) -> StaticBearerAuthProvider:
        env = environ if environ is not None else os.environ
        token_to_role: dict[str, ApiRole] = {}
        for role in ApiRole:
            token = _read_secret_or_env(env, f"TRADING_API_{role.value.upper()}_TOKEN")
            if token:
                token_to_role[token] = role
        if strict and not token_to_role:
            msg = (
                "production API auth requires at least one static bearer token "
                "via TRADING_API_*_TOKEN or TRADING_API_*_TOKEN_FILE"
            )
            raise RuntimeError(msg)
        return cls(token_to_role=token_to_role)


def build_auth_provider(
    *,
    runtime_mode: RuntimeMode,
    environ: Mapping[str, str] | None = None,
) -> AuthProvider:
    """Build the configured auth provider and fail unsafe production startup."""

    env = environ if environ is not None else os.environ
    default_mode = (
        STATIC_BEARER_AUTH_MODE if runtime_mode is RuntimeMode.PRODUCTION else DEV_AUTH_MODE
    )
    auth_mode = env.get(AUTH_MODE_ENV, default_mode).strip().lower()
    if runtime_mode is RuntimeMode.PRODUCTION and auth_mode == DEV_AUTH_MODE:
        msg = "production API startup refuses dev auth; set TRADING_AUTH_MODE=static_bearer"
        raise RuntimeError(msg)
    if auth_mode == DEV_AUTH_MODE:
        return DevHeaderAuthProvider()
    if auth_mode == STATIC_BEARER_AUTH_MODE:
        return StaticBearerAuthProvider.from_env(
            env,
            strict=runtime_mode is RuntimeMode.PRODUCTION,
        )
    msg = f"unsupported TRADING_AUTH_MODE={auth_mode}"
    raise RuntimeError(msg)


def auth_context_from_request(request: Request) -> AuthContext:
    provider = getattr(request.app.state, "auth_provider", None)
    if provider is None:
        provider = build_auth_provider(runtime_mode=RuntimeMode.HISTORICAL_REPLAY)
        request.app.state.auth_provider = provider
    return provider.authenticate(request)


def authenticate_websocket(websocket: WebSocket) -> AuthContext:
    provider = getattr(websocket.app.state, "auth_provider", None)
    if provider is None:
        provider = build_auth_provider(runtime_mode=RuntimeMode.HISTORICAL_REPLAY)
        websocket.app.state.auth_provider = provider
    return provider.authenticate(websocket)


def require_role(auth: AuthContext, allowed: Iterable[ApiRole]) -> AuthContext:
    if auth.role not in set(allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role {auth.role.value} is not allowed for this endpoint",
        )
    return auth


def _read_secret_or_env(env: Mapping[str, str], name: str) -> str | None:
    file_value = env.get(f"{name}_FILE")
    if file_value:
        try:
            return Path(file_value).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            msg = f"unable to read auth token file {name}_FILE"
            raise RuntimeError(msg) from exc
    value = env.get(name)
    return value.strip() if value else None

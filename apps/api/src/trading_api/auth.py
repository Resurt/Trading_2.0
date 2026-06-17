"""Production-safe auth abstraction for the FastAPI BFF control plane."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from typing import Protocol, cast

from fastapi import HTTPException, Request, WebSocket, status

from trading_api.schemas import ApiRole
from trading_common import RuntimeMode

AUTH_MODE_ENV = "TRADING_AUTH_MODE"
DEV_AUTH_MODE = "dev"
STATIC_BEARER_AUTH_MODE = "static_bearer"
WS_TICKET_SECRET_ENV = "TRADING_WS_TICKET_SECRET"
WS_TICKET_TTL_SECONDS_ENV = "TRADING_WS_TICKET_TTL_SECONDS"


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


@dataclass(frozen=True, slots=True)
class WebSocketTicket:
    ticket: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WebSocketTicketManager:
    """Short-lived signed ticket issuer for browser WebSocket auth."""

    secret: str
    ttl_seconds: int = 60

    def issue(self, auth: AuthContext) -> WebSocketTicket:
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=self.ttl_seconds)
        payload = {
            "role": auth.role.value,
            "subject": auth.subject,
            "auth_mode": auth.auth_mode,
            "exp": int(expires_at.timestamp()),
            "nonce": token_urlsafe(12),
        }
        encoded_payload = _b64_json(payload)
        signature = _b64_bytes(
            hmac.new(
                self.secret.encode("utf-8"),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return WebSocketTicket(ticket=f"{encoded_payload}.{signature}", expires_at=expires_at)

    def authenticate_ticket(self, ticket: str | None) -> AuthContext:
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="WS ticket required",
            )
        encoded_payload, sep, signature = ticket.partition(".")
        if not sep:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad WS ticket")
        expected_signature = _b64_bytes(
            hmac.new(
                self.secret.encode("utf-8"),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature, expected_signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad WS ticket")
        payload = _json_from_b64(encoded_payload)
        exp_value = payload.get("exp", 0)
        exp_ts = int(exp_value) if isinstance(exp_value, int | str) else 0
        if exp_ts < int(datetime.now(tz=UTC).timestamp()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Expired WS ticket",
            )
        role = ApiRole(str(payload.get("role", ApiRole.OBSERVER.value)))
        subject = str(payload.get("subject") or "ws-ticket:unknown")
        auth_mode = str(payload.get("auth_mode") or "ws_ticket")
        return AuthContext(role=role, subject=subject, auth_mode=auth_mode)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        strict: bool,
    ) -> WebSocketTicketManager | None:
        env = environ if environ is not None else os.environ
        secret = _read_secret_or_env(env, WS_TICKET_SECRET_ENV)
        if not secret:
            if strict:
                msg = (
                    "production WebSocket auth requires TRADING_WS_TICKET_SECRET "
                    "or TRADING_WS_TICKET_SECRET_FILE"
                )
                raise RuntimeError(msg)
            return None
        return cls(
            secret=secret,
            ttl_seconds=int(env.get(WS_TICKET_TTL_SECONDS_ENV, "60")),
        )


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


def build_ws_ticket_manager(
    *,
    runtime_mode: RuntimeMode,
    environ: Mapping[str, str] | None = None,
) -> WebSocketTicketManager | None:
    return WebSocketTicketManager.from_env(
        environ,
        strict=runtime_mode is RuntimeMode.PRODUCTION,
    )


def auth_context_from_request(request: Request) -> AuthContext:
    provider = getattr(request.app.state, "auth_provider", None)
    if provider is None:
        provider = build_auth_provider(runtime_mode=RuntimeMode.HISTORICAL_REPLAY)
        request.app.state.auth_provider = provider
    return provider.authenticate(request)


def authenticate_websocket(websocket: WebSocket) -> AuthContext:
    ticket_manager = cast(
        WebSocketTicketManager | None,
        getattr(websocket.app.state, "ws_ticket_manager", None),
    )
    ticket = websocket.query_params.get("ticket")
    if ticket_manager is not None and ticket:
        return ticket_manager.authenticate_ticket(ticket)
    provider = cast(AuthProvider | None, getattr(websocket.app.state, "auth_provider", None))
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


def _b64_json(payload: Mapping[str, object]) -> str:
    return _b64_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _b64_bytes(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _json_from_b64(payload: str) -> Mapping[str, object]:
    padded = payload + "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    value = json.loads(decoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad WS ticket")
    return value

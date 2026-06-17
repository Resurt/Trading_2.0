"""Database connection configuration helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit


def _read_password_from_file(path_value: str | None) -> str | None:
    if not path_value:
        return None
    password_path = Path(path_value)
    if not password_path.exists():
        return None
    return password_path.read_text(encoding="utf-8").strip()


def build_database_url_from_mapping(env: Mapping[str, str]) -> str:
    """Build a SQLAlchemy URL from explicit env-like values.

    This helper is intentionally strict: callers that want a local SQLite fallback must
    choose it explicitly instead of silently losing Postgres as the source of truth.
    """

    explicit_url = env.get("TRADING_DATABASE_URL") or env.get("DATABASE_URL")
    if explicit_url:
        return explicit_url

    host = env.get("POSTGRES_HOST", "localhost")
    port = env.get("POSTGRES_PORT", "5432")
    database = env.get("POSTGRES_DB", "trading_2_0")
    user = env.get("POSTGRES_USER", "trading_app")
    password = env.get("POSTGRES_PASSWORD") or _read_password_from_file(
        env.get("POSTGRES_PASSWORD_FILE", "secrets/postgres_password")
    )

    if not password:
        msg = (
            "Set DATABASE_URL, POSTGRES_PASSWORD, or POSTGRES_PASSWORD_FILE "
            "before running database migrations."
        )
        raise RuntimeError(msg)

    return (
        "postgresql+psycopg://"
        f"{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"
    )


def build_database_url_from_env() -> str:
    """Build a SQLAlchemy URL without putting secrets in committed config."""

    return build_database_url_from_mapping(os.environ)


def database_backend_from_url(database_url: str) -> str:
    """Return a stable backend label for audit/logging/readiness checks."""

    scheme = urlsplit(database_url).scheme
    if scheme.startswith("postgresql"):
        return "postgresql"
    if scheme.startswith("sqlite"):
        return "sqlite"
    return scheme or "unknown"


def redact_database_url(database_url: str) -> str:
    """Remove credentials from a SQLAlchemy URL before logs or audit events."""

    parts = urlsplit(database_url)
    if "@" not in parts.netloc:
        return database_url
    userinfo, hostinfo = parts.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    redacted_netloc = f"{username}:***@{hostinfo}" if username else f"***@{hostinfo}"
    return urlunsplit(
        (
            parts.scheme,
            redacted_netloc,
            parts.path,
            parts.query,
            parts.fragment,
        )
    )

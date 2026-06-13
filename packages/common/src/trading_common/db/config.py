"""Database connection configuration helpers."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus


def _read_password_from_file(path_value: str | None) -> str | None:
    if not path_value:
        return None
    password_path = Path(path_value)
    if not password_path.exists():
        return None
    return password_path.read_text(encoding="utf-8").strip()


def build_database_url_from_env() -> str:
    """Build a SQLAlchemy URL without putting secrets in committed config."""

    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url

    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "trading_2_0")
    user = os.getenv("POSTGRES_USER", "trading_app")
    password = os.getenv("POSTGRES_PASSWORD") or _read_password_from_file(
        os.getenv("POSTGRES_PASSWORD_FILE", "secrets/postgres_password")
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

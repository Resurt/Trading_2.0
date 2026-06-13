"""Alembic environment for the shared PostgreSQL schema."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[3]
COMMON_SRC = ROOT / "packages" / "common" / "src"
if str(COMMON_SRC) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC))

from trading_common.db import models as _models  # noqa: F401,E402
from trading_common.db.base import Base  # noqa: E402
from trading_common.db.config import build_database_url_from_env  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url and not configured_url.endswith("change_me@localhost:5432/trading_2_0"):
        return configured_url
    return build_database_url_from_env()


def run_migrations_offline() -> None:
    """Run migrations without creating an engine."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

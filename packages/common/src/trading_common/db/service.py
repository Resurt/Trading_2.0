"""Small SQLAlchemy session service used by backend components."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


class DatabaseService:
    """Owns the engine and provides transactional session scopes."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.engine: Engine = create_engine(database_url, echo=echo, future=True)
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """Yield a session and commit or roll back as a unit."""

        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

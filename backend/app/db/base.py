"""Database engine and session plumbing.

SQLite is the default so the project runs with no external services. The same
code works against Postgres by changing DATABASE_URL only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


_connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session for background tasks and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create tables. Import models first so they register on the metadata."""
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

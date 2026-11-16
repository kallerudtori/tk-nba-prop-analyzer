"""
Database engine and session handling.

Postgres only. SQLite is deliberately not supported: the learning loop depends
on durable storage, and Railway's container filesystem is ephemeral, so a
SQLite file silently loses every graded prediction on redeploy.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings

_engine: Engine | None = None
_Session: sessionmaker | None = None


class DatabaseNotConfigured(RuntimeError):
    pass


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        if not settings.database_url:
            raise DatabaseNotConfigured(
                "DATABASE_URL is not set. On Railway, add a PostgreSQL service "
                "and reference it from the web service as "
                "DATABASE_URL=${{Postgres.DATABASE_URL}}."
            )
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,   # Railway recycles connections aggressively
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _Session


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on clean exit, rolls back on exception."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine/sessionmaker — used by tests that swap DATABASE_URL."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None

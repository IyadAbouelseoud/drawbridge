"""Synchronous session factory for the MCP servers.

The API runs asyncpg on a long-lived pool; the MCP servers handle one tool call at a time
and are better served by short synchronous sessions. Same database, same models, same
constraints — only the driver differs.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    """Sync DSN, derived from the same setting the API uses.

    The API's URL carries the asyncpg driver; psycopg is what a synchronous session
    needs, so the driver is swapped rather than a second setting being introduced that
    could drift out of step.
    """
    url = os.environ.get(
        "DRAWBRIDGE_DATABASE_URL",
        "postgresql+asyncpg://drawbridge:drawbridge@postgres:5432/drawbridge",
    )
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg://")


@lru_cache(maxsize=1)
def engine() -> Engine:
    return create_engine(database_url(), pool_pre_ping=True, pool_size=5)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction that commits on success and rolls back on failure.

    Analyst decisions are the durable fact in this system, so they commit before any
    notification is attempted — see services/api/src/resume.py.
    """
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

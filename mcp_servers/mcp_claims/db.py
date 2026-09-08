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
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services.api.src.tenancy import scope_to_claim, scope_to_review, set_tenant


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
def session_scope(
    tenant_id: UUID | None = None,
    *,
    claim_id: UUID | None = None,
    review_id: UUID | None = None,
) -> Iterator[Session]:
    """A transaction that commits on success and rolls back on failure.

    Analyst decisions are the durable fact in this system, so they commit before any
    notification is attempted — see services/api/src/resume.py.

    Scoping under row-level security takes whichever identifier the tool actually has.
    Most MCP tools are addressed by claim or review id and never see a tenant, so
    `claim_id` and `review_id` resolve the owner through a SECURITY DEFINER lookup and
    scope the rest of the transaction to it — see `services/api/src/tenancy.py`. A tool
    that supplies none of the three runs unscoped and, under the `drawbridge_app` role,
    reads nothing.
    """
    session = _session_factory()()
    try:
        if tenant_id is not None:
            set_tenant(session, tenant_id)
        elif claim_id is not None:
            scope_to_claim(session, claim_id)
        elif review_id is not None:
            scope_to_review(session, review_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

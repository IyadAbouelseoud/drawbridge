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

from services.api.src.auth import expected_tenant
from services.api.src.tenancy import (
    TenantScopeError,
    scope_to_claim,
    scope_to_review,
    set_tenant,
)

__all__ = ["TenantScopeError", "database_url", "engine", "session_scope"]


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
    # The compose files give the app role's DSN without a password, and the API completes
    # it from the secrets backend (`Settings._apply_db_password`). This read the variable
    # raw, so an MCP server pointed at `drawbridge_app` had no password to connect with.
    # An inline password still wins — `inject_password` leaves one alone.
    from services.api.src.config import get_settings
    from services.api.src.secrets import inject_password

    url = inject_password(url, get_settings().app_db_password)
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

    Since v1.1.0 every branch is checked against the caller's tenant (`expected_tenant`,
    set by `mcp_servers.security.guarded` from the verified token): a tenant user naming
    another tenant, or another tenant's claim or review, gets the same "not found" the API
    gives. A tenant user naming nothing is scoped to their own tenant rather than to none.
    """
    expected = expected_tenant()
    session = _session_factory()()
    try:
        if tenant_id is not None:
            if expected is not None and tenant_id != expected:
                msg = f"no tenant {tenant_id}"
                raise TenantScopeError(msg)
            set_tenant(session, tenant_id)
        elif claim_id is not None:
            scope_to_claim(session, claim_id, expected)
        elif review_id is not None:
            scope_to_review(session, review_id, expected)
        elif expected is not None:
            set_tenant(session, expected)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

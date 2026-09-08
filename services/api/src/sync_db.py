"""One synchronous engine, for the request paths that cannot be async.

Most of the API talks to Postgres through the async session on `app.state`. Three things
cannot: the agent worker (the Anthropic SDK call in the middle is blocking), the packager
(rendering is CPU work), and the persistence layer, which is shared verbatim with
`mcp-claims` and `services/analyst` and is written against a synchronous `Session`.

Rewriting that shared code twice — once async for the API, once sync for the MCP servers —
would give two implementations of the claim state machine, and the interesting bugs would
be in whichever one the tests did not cover. So the sync code stays single, and the API
runs it in a worker thread.

The engine is process-wide and cached. The previous shape built a fresh `create_engine`
per request, which discards the pool on every call and opens a new connection for work
that is already slow enough.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.api.src.config import get_settings
from services.api.src.tenancy import scope_to_claim, set_tenant

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from uuid import UUID

    from sqlalchemy.engine import Engine


@lru_cache(maxsize=1)
def sync_engine() -> Engine:
    """The process-wide synchronous engine.

    `pool_pre_ping` because these connections sit idle between pipeline runs and a stale
    one surfaces as a failed claim transition rather than as a connection error.
    """
    return create_engine(get_settings().sync_database_url, pool_pre_ping=True)


@contextmanager
def sync_session(tenant_id: UUID | None = None) -> Iterator[Session]:
    """A session that commits on success and rolls back on any exception.

    Explicit rather than left to the caller: a half-written claim — entry lines in,
    refund lines not — is worse than no claim, because it looks like a claim.

    `tenant_id` scopes the transaction under row-level security. It is optional because
    the corpus loaders and the offboarding path are genuinely tenant-less, not because
    omitting it is safe: under the `drawbridge_app` role an unscoped session reads no
    tenant rows at all, which is the intended failure. Transaction-scoped, so the value
    cannot survive into the next checkout of a pooled connection.
    """
    with Session(sync_engine()) as session:
        try:
            if tenant_id is not None:
                set_tenant(session, tenant_id)
            yield session
        except Exception:
            session.rollback()
            raise
        else:
            session.commit()


async def in_thread[T](work: Callable[[Session], T], tenant_id: UUID | None = None) -> T:
    """Run one unit of synchronous session work off the event loop.

    Imported locally in the routes rather than at module scope so that importing the API
    package does not pull anyio into environments that only want the models.
    """
    from anyio import to_thread

    def _run() -> T:
        with sync_session(tenant_id) as session:
            return work(session)

    return await to_thread.run_sync(_run)


async def in_thread_for_claim[T](claim_id: UUID, work: Callable[[Session], T]) -> T:
    """The same, for a route that knows a claim id and not its owner.

    `GET /claims/{id}`, the packager and the transition endpoint are all addressed this
    way. The owner is resolved through `tenancy.scope_to_claim` — one SECURITY DEFINER
    lookup — and the rest of the transaction runs scoped, so a caller holding a claim id
    still cannot reach anything else belonging to that tenant.
    """
    from anyio import to_thread

    def _run() -> T:
        with sync_session() as session:
            scope_to_claim(session, claim_id)
            return work(session)

    return await to_thread.run_sync(_run)


def reset_engine() -> None:
    """Dispose the cached engine and forget it.

    For tests that repoint settings at another database, and for a clean shutdown — a
    pool left holding connections keeps the Postgres side allocated until it times out.
    """
    if sync_engine.cache_info().currsize:
        sync_engine().dispose()
    sync_engine.cache_clear()

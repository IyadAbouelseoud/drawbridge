"""Row-level security — cross-tenant isolation enforced by Postgres, not by a `WHERE`.

Every tenant-scoped table already carries `tenant_id`, and every query in this repository
already filters on it. That is not the same as isolation. A filter is a thing a developer
remembers; the first query that forgets is a data breach with a passing test suite, and
nothing in the type system distinguishes the two. RLS moves the guarantee under the query,
where forgetting produces zero rows instead of another tenant's claim.

**How a connection says who it is.** `SET LOCAL "tenant.id"` — transaction-scoped, so it
cannot leak into the next checkout of a pooled connection. `set_tenant` writes it through
`set_config(..., is_local => true)` rather than literal SQL, because a `SET LOCAL` cannot
take a bind parameter and interpolating a tenant id into DDL-shaped text is the injection
this control exists to make irrelevant. Unset means no rows: the policies compare against
`app_current_tenant()`, which is NULL when nothing set it, and `tenant_id = NULL` is never
true. Fail-closed is the only safe default here — a policy that fell open when the GUC was
missing would be satisfied by every code path that forgot, which is exactly the set of
paths this is written to catch.

**The strength of the control rests on one thing, and it is worth saying plainly.**
Postgres superusers and roles with `BYPASSRLS` ignore policies entirely, and the
`drawbridge` role that owns these tables is both. So RLS is inert for anyone connecting as
the owner, and `FORCE ROW LEVEL SECURITY` would not change that — FORCE binds the owner
only where the owner is not also a superuser. The isolation therefore comes from the
application connecting as `drawbridge_app`, an unprivileged role created by
`ensure_app_role`, and it is worth nothing otherwise. Migrations, the offboarding script,
and the integration fixtures deliberately keep the owner connection, because each of them
has a job that spans tenants.

**A tombstoned tenant disappears.** `app_current_tenant()` resolves the GUC through the
`tenants` table and returns NULL when `offboarded_at` is set, so one predicate hides an
offboarded tenant's documents, claims, ledger and queue at once — without deleting a row
that a five-year retention obligation still covers. See `scripts/tenant_offboard.py`.

**Two SECURITY DEFINER lookups.** Several entry points are addressed by claim id or review
id and never see a tenant: `GET /claims/{id}`, the packager, `trace_figure`. They resolve
the owner first and then set the GUC, which needs a read that RLS has not yet scoped.
`tenant_of_claim` and `tenant_of_review` do exactly that and nothing else — they return one
uuid, so what a caller learns from guessing a random UUID is which tenant owns it, and both
pin `search_path` so the definer privilege cannot be redirected at another schema's table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import event, text

from services.api.src.models import Base

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

# The session variable every connection sets. A custom GUC needs a dotted name; the
# prefix is what makes it a custom one rather than an attempt to set a Postgres setting.
TENANT_GUC = "tenant.id"

# The unprivileged role the application connects as. Nothing else about RLS matters if
# this is not the role in the DSN — see the module docstring.
APP_ROLE = "drawbridge_app"

# Printable ASCII minus the backslash — see `_sql_literal`.
_PASSWORD_ALPHABET = frozenset(
    "".join(chr(code) for code in range(0x20, 0x7F) if chr(code) != "\\")
)


class TenantScopeError(ValueError):
    """A session needed a tenant and could not be given one.

    A `ValueError` because that is what it is from the caller's side: the claim or review
    id it supplied names nothing this connection can act on. The MCP layer already returns
    `ValueError` to the model as data rather than raising, which is the right treatment —
    "no claim <uuid>" is an answer, and a tool that raised instead would leave the agent
    with a transport error and nothing to say.
    """


# --------------------------------------------------------------------------------------
# The DDL
# --------------------------------------------------------------------------------------

# SECURITY DEFINER so it can read `tenants` while a policy on `tenants` is being
# evaluated. The owner is not subject to that policy (RLS is enabled, not forced), which
# is what breaks the recursion a plain SQL function would cause.
CURRENT_TENANT_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid AS $$
    SELECT t.tenant_id
      FROM public.tenants t
     WHERE t.tenant_id = nullif(current_setting('tenant.id', true), '')::uuid
       AND t.offboarded_at IS NULL
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""

TENANT_OF_CLAIM_FUNCTION = """
CREATE OR REPLACE FUNCTION app_tenant_of_claim(p_claim uuid) RETURNS uuid AS $$
    SELECT c.tenant_id FROM public.claims c WHERE c.claim_id = p_claim
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""

TENANT_OF_REVIEW_FUNCTION = """
CREATE OR REPLACE FUNCTION app_tenant_of_review(p_review uuid) RETURNS uuid AS $$
    SELECT r.tenant_id FROM public.review_queue r WHERE r.review_id = p_review
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""

# The drafting worker's one cross-tenant question: which tenants have work waiting? It used
# to ask by running unscoped, which under the app role returns nothing — so the on-prem
# worker, the only one ever deployed as the app role, polled an empty queue forever and
# logged nothing, because it only logs passes that did something. The answer is a list of
# tenant ids and no row content; the worker then scopes to each one in turn, so the
# drafting itself happens inside the same policy every other tenant read does.
TENANTS_WITH_UNDRAFTED_FUNCTION = """
CREATE OR REPLACE FUNCTION app_tenants_with_undrafted_reviews() RETURNS SETOF uuid AS $$
    SELECT DISTINCT r.tenant_id
      FROM public.review_queue r
      JOIN public.tenants t ON t.tenant_id = r.tenant_id
     WHERE r.state = 'open' AND r.agent_memo IS NULL AND r.agent_model IS NULL
       AND t.offboarded_at IS NULL
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""

# The review dispatcher's question, the same shape as the drafter's: which tenants have
# open exceptions. Ids only.
TENANTS_WITH_OPEN_REVIEWS_FUNCTION = """
CREATE OR REPLACE FUNCTION app_tenants_with_open_reviews() RETURNS SETOF uuid AS $$
    SELECT DISTINCT r.tenant_id
      FROM public.review_queue r
      JOIN public.tenants t ON t.tenant_id = r.tenant_id
     WHERE r.state = 'open' AND t.offboarded_at IS NULL
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""

TENANT_OF_TOKEN_FUNCTION = """
CREATE OR REPLACE FUNCTION app_tenant_of_resume_token(p_token text) RETURNS uuid AS $$
    SELECT r.tenant_id FROM public.review_queue r WHERE r.resume_token = p_token
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""

# Table to the predicate that decides whether a row belongs to the current tenant.
#
# `refund_lines` and `claim_transitions` carry no `tenant_id` of their own — they are
# addressed through their claim, and duplicating the column to simplify a policy would
# create a second place for the answer to be wrong. The EXISTS costs an index lookup on
# the claims primary key, which is the cheapest join in the schema.
#
# `classification_queries.tenant_id` is nullable: a classification run outside any tenant
# is a corpus query, and rows that belong to nobody are readable by everybody.
TENANT_PREDICATES: dict[str, str] = {
    "tenants": "tenant_id = app_current_tenant()",
    # The filing identity: EIN, CR number, IBAN. Scoped like any other tenant table —
    # a refund destination account is the single most useful row here to a reader who
    # should not have it.
    "tenant_profiles": "tenant_id = app_current_tenant()",
    "documents": "tenant_id = app_current_tenant()",
    "entry_lines": "tenant_id = app_current_tenant()",
    "export_lines": "tenant_id = app_current_tenant()",
    "claims": "tenant_id = app_current_tenant()",
    "audit_ledger": "tenant_id = app_current_tenant()",
    "review_queue": "tenant_id = app_current_tenant()",
    "classification_queries": "tenant_id IS NULL OR tenant_id = app_current_tenant()",
    "refund_lines": (
        "EXISTS (SELECT 1 FROM claims c "
        "WHERE c.claim_id = refund_lines.claim_id AND c.tenant_id = app_current_tenant())"
    ),
    "claim_transitions": (
        "EXISTS (SELECT 1 FROM claims c "
        "WHERE c.claim_id = claim_transitions.claim_id AND c.tenant_id = app_current_tenant())"
    ),
}

# Operational tables: global, not tenant data, and append-only. The app role writes the
# kill switch's history through the operator route and reads it on every mutating request;
# it never needs UPDATE, and the triggers would refuse it anyway.
OPERATIONAL_TABLES: dict[str, str] = {"control_events": "SELECT, INSERT"}

# Reference data. The tariff schedule and the CROSS rulings are the same for every tenant
# and carry nothing that identifies one, so no policy applies — but the app role still
# needs to read them, and a table left ungranted fails in a way that looks like RLS.
SHARED_TABLES = ("tariff_lines", "tariff_rulings", "alembic_version")


def _policy_statements(table: str, predicate: str) -> tuple[str, ...]:
    """Enable RLS on one table and install its policy, idempotently.

    One `FOR ALL` policy rather than four. Split policies would let SELECT and INSERT
    drift apart, and a row a tenant can write but not read is a bug that only shows up
    under a second tenant.
    """
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}",
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})",
    )


def _table_exists(connection: Connection, table: str) -> bool:
    """Whether the table is actually there.

    `create_all` fires the hook below after building whatever subset of the metadata the
    importing module pulled in, and `alembic_version` is never in the metadata at all.
    Policies are applied to the tables that exist rather than assumed onto all of them,
    because the alternative is a hook that fails on a partially-built schema and takes the
    ledger's guards down with it.
    """
    return (
        connection.execute(text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}).scalar()
        is not None
    )


def install_rls(connection: Connection) -> None:
    """Install the helper functions and every table policy.

    Idempotent: `CREATE OR REPLACE` for the functions, `DROP POLICY IF EXISTS` before each
    `CREATE POLICY`. Called from the migration and from the `after_create` hook below, so
    a database built by `create_all` carries the same policies as a migrated one — the
    same reasoning as `install_ledger_guards`, and for the same reason: a control that
    exists only where Alembic has run is a control no test can check.
    """
    for function in (
        CURRENT_TENANT_FUNCTION,
        TENANT_OF_CLAIM_FUNCTION,
        TENANT_OF_REVIEW_FUNCTION,
        TENANT_OF_TOKEN_FUNCTION,
        TENANTS_WITH_UNDRAFTED_FUNCTION,
        TENANTS_WITH_OPEN_REVIEWS_FUNCTION,
    ):
        connection.execute(text(function))

    for table, predicate in TENANT_PREDICATES.items():
        if not _table_exists(connection, table):
            continue
        for statement in _policy_statements(table, predicate):
            connection.execute(text(statement))


def _sql_literal(password: str) -> str:
    """Escape a password for a single-quoted SQL literal, refusing anything exotic.

    Printable ASCII only. A password containing a newline, a NUL or a backslash is
    rejected rather than escaped: `standard_conforming_strings` decides what a backslash
    means, and a control character in a role password is far more likely to be a
    truncated secret than an intended one.
    """
    if not password or any(character not in _PASSWORD_ALPHABET for character in password):
        raise ValueError(
            "the app role password must be non-empty printable ASCII without backslashes"
        )
    return password.replace("'", "''")


def ensure_app_role(connection: Connection, password: str) -> None:
    """Create or update the unprivileged role the application connects as.

    `NOSUPERUSER NOBYPASSRLS` is the entire point; a role with either attribute reads
    every tenant's rows no matter what the policies say.

    Grants are table-level and deliberately do not include DELETE on `audit_ledger` —
    the append-only triggers refuse it anyway, and a privilege that only ever produces an
    exception is one more thing to reason about during an audit. `USAGE` on sequences
    covers the ledger's identity column.
    """
    connection.execute(
        text(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN;
            END IF;
            ALTER ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        END $$;
    """)
    )
    # `ALTER ROLE ... PASSWORD` is a utility statement and takes no bind parameter, so the
    # value has to be a literal. The character set is restricted first and the quote is
    # doubled second — belt and braces, because this is the one statement in the module
    # where a parameter placeholder is not available to make the question moot.
    connection.execute(text(f"ALTER ROLE {APP_ROLE} WITH PASSWORD '{_sql_literal(password)}'"))
    grant_app_role(connection)


def grant_app_role(connection: Connection) -> None:
    """Give the app role exactly the object privileges the running services need."""
    connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
    connection.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))
    for table in TENANT_PREDICATES:
        if not _table_exists(connection, table):
            continue
        privileges = "SELECT, INSERT" if table == "audit_ledger" else "SELECT, INSERT, UPDATE"
        connection.execute(text(f"GRANT {privileges} ON {table} TO {APP_ROLE}"))

    for table, privileges in OPERATIONAL_TABLES.items():
        if _table_exists(connection, table):
            connection.execute(text(f"GRANT {privileges} ON {table} TO {APP_ROLE}"))

    for table in SHARED_TABLES:
        if not _table_exists(connection, table):
            continue
        # The corpus loaders (`scripts/ingest_tariff.py`, `scripts/embed_corpus.py`) run
        # as the owner, so the app role only reads the schedule and the rulings.
        connection.execute(text(f"GRANT SELECT ON {table} TO {APP_ROLE}"))


@event.listens_for(Base.metadata, "after_create")
def _install_rls_on_create(
    target: object,  # noqa: ARG001 - fixed SQLAlchemy event signature
    connection: Connection,
    **kwargs: Any,  # noqa: ARG001 - fixed SQLAlchemy event signature
) -> None:
    """So `create_all` produces the same policies a migration would."""
    install_rls(connection)


# --------------------------------------------------------------------------------------
# Setting the scope on a session
# --------------------------------------------------------------------------------------

_SET = text("SELECT set_config(:name, :value, true)")
_GET = text("SELECT app_current_tenant()")


def set_tenant(session: Session, tenant_id: UUID) -> None:
    """Scope this transaction to one tenant. Undone by commit or rollback.

    Inside a mutating unit of work this is also where a kill switch covering the tenant —
    or the acting principal, or everything — is enforced. See `killswitch.MUTATING`.
    """
    from services.api.src import killswitch

    if killswitch.MUTATING.get():
        killswitch.assert_running(
            session, principal=killswitch.PRINCIPAL.get(), tenant_id=tenant_id
        )
    session.execute(_SET, {"name": TENANT_GUC, "value": str(tenant_id)})


async def set_tenant_async(session: AsyncSession, tenant_id: UUID) -> None:
    """The async path's equivalent — `review.py` runs on the asyncpg pool."""
    from services.api.src import killswitch

    if killswitch.MUTATING.get():
        covering = (await killswitch.state_async(session)).covering(
            principal=killswitch.PRINCIPAL.get(), tenant_id=tenant_id
        )
        if covering is not None:
            raise killswitch.KillSwitchEngagedError(covering)
    await session.execute(_SET, {"name": TENANT_GUC, "value": str(tenant_id)})


def current_tenant(session: Session) -> UUID | None:
    """What the database believes this transaction is scoped to.

    Resolved through `app_current_tenant()` rather than by reading the GUC back, so a
    tenant that has been tombstoned since the value was set reports as None — which is
    what every policy on the connection will also conclude.
    """
    value = session.execute(_GET).scalar_one_or_none()
    return UUID(str(value)) if value else None


def _lookup(session: Session, function: str, key: object) -> UUID | None:
    row = session.execute(text(f"SELECT {function}(:key)"), {"key": key}).scalar_one_or_none()
    return UUID(str(row)) if row else None


def tenant_of_claim(session: Session, claim_id: UUID) -> UUID | None:
    return _lookup(session, "app_tenant_of_claim", claim_id)


def tenant_of_review(session: Session, review_id: UUID) -> UUID | None:
    return _lookup(session, "app_tenant_of_review", review_id)


def tenant_of_resume_token(session: Session, token: str) -> UUID | None:
    return _lookup(session, "app_tenant_of_resume_token", token)


def tenants_with_open_reviews(session: Session) -> list[UUID]:
    """Tenant ids with open review rows. Ids only."""
    rows = session.execute(text("SELECT app_tenants_with_open_reviews()")).scalars()
    return sorted(UUID(str(row)) for row in rows)


def tenants_with_undrafted_reviews(session: Session) -> list[UUID]:
    """Tenant ids with open, undrafted review rows. Ids only — see the function's DDL."""
    rows = session.execute(text("SELECT app_tenants_with_undrafted_reviews()")).scalars()
    return sorted(UUID(str(row)) for row in rows)


def _require(owner: UUID | None, expected: UUID | None, missing: str) -> UUID:
    """The owner an identifier resolved to, checked against the one the caller may see.

    Week 11 resolved the owner and scoped to it, which made a claim id sufficient to read
    a claim — stated at the time as the exact size of the hole fail-closed semantics left.
    `expected` is week 12 closing it: `auth.expected_tenant()` supplies the token's tenant,
    and a mismatch raises the same error a missing row does.

    The same error on purpose. Distinguishing "not yours" from "does not exist" turns a
    guessed uuid into a membership oracle, and the caller cannot act on the difference.

    None means no constraint, which is the service principal running the pipeline across
    tenants — not an absent check.
    """
    if owner is None or (expected is not None and owner != expected):
        raise TenantScopeError(missing)
    return owner


def scope_to_claim(session: Session, claim_id: UUID, expected: UUID | None = None) -> UUID:
    """Resolve a claim's owner and scope the session to it.

    Raises rather than returning None. A caller that reached here has a claim id and
    intends to act on it; continuing unscoped would produce an empty result that reads
    like "no such claim" whichever of the two it actually was.
    """
    owner = _require(tenant_of_claim(session, claim_id), expected, f"no claim {claim_id}")
    set_tenant(session, owner)
    return owner


def scope_to_review(session: Session, review_id: UUID, expected: UUID | None = None) -> UUID:
    owner = _require(tenant_of_review(session, review_id), expected, f"no review {review_id}")
    set_tenant(session, owner)
    return owner


async def _lookup_async(session: AsyncSession, function: str, key: object) -> UUID | None:
    row = (
        await session.execute(text(f"SELECT {function}(:key)"), {"key": key})
    ).scalar_one_or_none()
    return UUID(str(row)) if row else None


async def scope_to_review_async(
    session: AsyncSession, review_id: UUID, expected: UUID | None = None
) -> UUID:
    """The async equivalent of `scope_to_review`, for the review routes.

    `POST /review/{id}/resolve` is addressed by review id alone — an analyst clicking a
    link has nothing else — so the owner is resolved before the update runs and the
    `WHERE` then executes inside the tenant's scope. Without it the update would find any
    tenant's row by a guessed uuid.
    """
    resolved = await _lookup_async(session, "app_tenant_of_review", review_id)
    owner = _require(resolved, expected, f"no review {review_id}")
    await set_tenant_async(session, owner)
    return owner


async def scope_to_resume_token_async(
    session: AsyncSession, token: str, expected: UUID | None = None
) -> UUID:
    """Scope by the token n8n polls with. The token *is* the credential on that path."""
    resolved = await _lookup_async(session, "app_tenant_of_resume_token", token)
    owner = _require(resolved, expected, "unknown token")
    await set_tenant_async(session, owner)
    return owner

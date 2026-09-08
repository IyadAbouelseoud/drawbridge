"""Integration fixtures — a real Postgres, not a mock.

These tests exist to prove the analyst path works against the actual state machine and
its CHECK constraints. A mocked session would pass while the database rejected the same
write, which is the failure mode integration tests are for.

Skipped rather than failed when no database is reachable, so the unit suite stays runnable
without Docker.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from services.api.src.models import Base, Claim, ReviewQueue, Tenant, install_ledger_guards
from services.api.src.tenancy import APP_ROLE, ensure_app_role, install_rls

TEST_DSN = os.environ.get(
    "DRAWBRIDGE_TEST_DATABASE_URL",
    "postgresql+psycopg://drawbridge:drawbridge@localhost:5432/drawbridge",
)

# The password the suite gives `drawbridge_app`. Local only, and it has to be a literal
# somewhere: the point of the role is that it is unprivileged, so a leaked test password
# grants exactly what an unscoped connection grants, which is nothing.
TEST_APP_PASSWORD = os.environ.get("DRAWBRIDGE_TEST_APP_PASSWORD", "drawbridge-app-test")


def _reachable(dsn: str) -> bool:
    try:
        engine = create_engine(dsn, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _reachable(TEST_DSN),
    reason=f"no Postgres at {TEST_DSN}; run `docker compose up -d postgres`",
)


@pytest.fixture(scope="session")
def engine() -> Engine:
    eng = create_engine(TEST_DSN, pool_pre_ping=True)
    Base.metadata.create_all(eng, checkfirst=True)
    # `create_all` installs the ledger's append-only triggers via its after_create hook,
    # but only for a table it actually creates. A developer database carrying an
    # `audit_ledger` from before the guards existed would otherwise run the immutability
    # tests against a table that has none — which is the one failure mode those tests
    # cannot detect from the inside.
    with eng.begin() as connection:
        install_ledger_guards(connection)
        # Same reasoning for the policies: `create_all` installs them through the metadata
        # hook only for a schema it actually builds, and a developer database that predates
        # this week would otherwise run the isolation tests against unprotected tables —
        # which is the one failure those tests cannot detect from the inside.
        install_rls(connection)
        ensure_app_role(connection, TEST_APP_PASSWORD)
    return eng


@pytest.fixture(scope="session")
def app_engine(engine: Engine) -> Engine:  # noqa: ARG001 - depends on the schema, not the value
    """The same database, seen through the unprivileged role the services connect as.

    Every other fixture here connects as the owner, which is a superuser and therefore
    bypasses every policy — convenient for building test data, useless for proving
    isolation. `tests/integration/test_rls.py` is the only suite that uses this one, and
    it is the only suite where RLS is in effect at all.
    """
    url = make_url(TEST_DSN).set(username=APP_ROLE, password=TEST_APP_PASSWORD)
    return create_engine(url, pool_pre_ping=True)


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose writes are rolled back at the end of the test.

    Nested in an outer transaction so each test sees a clean slate without truncating
    tables another test might be using.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def tenant(session: Session) -> Tenant:
    row = Tenant(
        tenant_id=uuid4(),
        name="Integration Test Tenant",
        default_jurisdiction="ksa",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def claim(session: Session, tenant: Tenant) -> Claim:
    """A GCC claim sitting in ANALYST_REVIEW — the state exceptions are raised from."""
    row = Claim(
        claim_id=uuid4(),
        tenant_id=tenant.tenant_id,
        state="analyst_review",
        jurisdiction="ksa",
        currency="SAR",
        lane="gcc_reexport_drawback",
        drawback_type="gcc_unused_reexport",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        filing_deadline=date(2025, 3, 12),
        absolute_bar_date=date(2027, 2, 8),
        total_refund=Decimal("19531.25"),
    )
    session.add(row)
    session.flush()
    return row


def make_exception(
    session: Session,
    *,
    tenant_id: UUID,
    claim_id: UUID | None,
    reason: str = "threshold_near_miss",
    severity: str = "normal",
    workflow_run_id: str | None = "wf-integration-1",
) -> ReviewQueue:
    """A queued exception of the shape week 4's triage produces."""
    row = ReviewQueue(
        review_id=uuid4(),
        tenant_id=tenant_id,
        claim_id=claim_id,
        reason=reason,
        severity=severity,
        summary=(
            "1 re-export fell within 10% of the USD 5,000 minimum and was rejected; "
            "confirm the valuation basis before abandoning it"
        ),
        citation="GCC Rules of Implementation Art. 16 §2",
        payload={"details": ["18000.00 SAR = 4800.00 USD; short by 200.00 USD [near_miss]"]},
        resume_token=uuid4().hex,
        workflow_run_id=workflow_run_id,
        state="open",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def exception_row(session: Session, tenant: Tenant, claim: Claim) -> ReviewQueue:
    return make_exception(session, tenant_id=tenant.tenant_id, claim_id=claim.claim_id)


@pytest.fixture
def imminent_deadline() -> date:
    return date.today() + timedelta(days=11)


@pytest.fixture
def now() -> datetime:
    return datetime.now()

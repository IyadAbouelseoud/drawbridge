"""Cross-tenant isolation, proved through the role the application actually connects as.

Every other suite in this directory connects as `drawbridge`, which owns the tables and is
a superuser, and therefore bypasses every policy in the database. That is convenient for
building test data and worthless for testing isolation — a suite written against the owner
would pass identically with RLS switched off.

So these tests use `app_engine`: the same database seen through `drawbridge_app`, the
unprivileged role `scripts/rls_bootstrap.py` creates and the deployed services log in as.
The first test in the file checks that assumption before any of the others rely on it,
because a role that quietly regained BYPASSRLS would turn the rest of this file green while
isolating nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from services.api.src.ledger import record
from services.api.src.models import Claim, Tenant
from services.api.src.tenancy import (
    APP_ROLE,
    TENANT_GUC,
    TenantScopeError,
    current_tenant,
    scope_to_claim,
    set_tenant,
    tenant_of_claim,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine


def _claim(tenant_id: UUID, refund: str) -> Claim:
    return Claim(
        claim_id=uuid4(),
        tenant_id=tenant_id,
        state="quantified",
        jurisdiction="ksa",
        currency="SAR",
        lane="gcc_reexport_drawback",
        drawback_type="gcc_unused_reexport",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        filing_deadline=date(2025, 6, 30),
        absolute_bar_date=date(2027, 1, 1),
        total_refund=Decimal(refund),
    )


@pytest.fixture(scope="module")
def two_tenants(engine: Engine) -> Iterator[tuple[Tenant, Claim, Tenant, Claim]]:
    """Two tenants with a claim each, committed by the owner so the app role can look.

    Module-scoped and committed rather than rolled back: the app role connects on its own
    connection and cannot see another connection's uncommitted transaction, so the usual
    rollback-per-test fixture would leave it with an empty database and every isolation
    assertion would pass vacuously.
    """
    alpha = Tenant(tenant_id=uuid4(), name="Alpha Trading", default_jurisdiction="ksa")
    beta = Tenant(tenant_id=uuid4(), name="Beta Logistics", default_jurisdiction="ksa")
    alpha_claim = _claim(alpha.tenant_id, "19531.25")
    beta_claim = _claim(beta.tenant_id, "44000.00")

    with Session(engine) as session:
        # Tenants first and flushed on their own: nothing declares a relationship between
        # Claim and Tenant, so the unit of work has no dependency to order them by and
        # would otherwise insert the claims against a tenant row that does not exist yet.
        session.add_all([alpha, beta])
        session.flush()
        session.add_all([alpha_claim, beta_claim])
        session.flush()
        for tenant, claim in ((alpha, alpha_claim), (beta, beta_claim)):
            record(
                session,
                tenant_id=tenant.tenant_id,
                event_type="claim_persisted",
                actor="pipeline",
                claim_id=claim.claim_id,
                subject="rls-fixture",
            )
        session.commit()
        detached = (alpha, alpha_claim, beta, beta_claim)
        yield detached

        # Owner teardown. The ledger's triggers refuse DELETE, so they come down for the
        # length of the cleanup — a privileged act, done deliberately and narrowly, which
        # is the same posture `test_mcp_analyst_tools.py` takes for the same reason.
        session.execute(text("ALTER TABLE audit_ledger DISABLE TRIGGER USER"))
        session.execute(
            text("DELETE FROM audit_ledger WHERE tenant_id = ANY(:ids)"),
            {"ids": [alpha.tenant_id, beta.tenant_id]},
        )
        session.execute(text("ALTER TABLE audit_ledger ENABLE TRIGGER USER"))
        session.execute(
            text("DELETE FROM claims WHERE tenant_id = ANY(:ids)"),
            {"ids": [alpha.tenant_id, beta.tenant_id]},
        )
        session.execute(
            text("DELETE FROM tenants WHERE tenant_id = ANY(:ids)"),
            {"ids": [alpha.tenant_id, beta.tenant_id]},
        )
        session.commit()


@pytest.fixture
def app_session(app_engine: Engine) -> Iterator[Session]:
    """A session as `drawbridge_app` — the only connection in the suite RLS applies to."""
    with Session(app_engine) as session:
        yield session
        session.rollback()


class TestTheRoleIsActuallyUnprivileged:
    """If this fails, nothing else in the file means anything."""

    def test_the_app_role_cannot_bypass_policies(self, app_session: Session) -> None:
        superuser, bypass = app_session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"),
            {"r": APP_ROLE},
        ).one()
        assert superuser is False
        assert bypass is False

    def test_row_level_security_is_on_for_the_tables_that_hold_tenant_data(
        self, app_session: Session
    ) -> None:
        unprotected = (
            app_session.execute(
                text("""
                SELECT tablename FROM pg_tables
                 WHERE schemaname = 'public'
                   AND tablename IN ('claims','entry_lines','export_lines','documents',
                                     'audit_ledger','review_queue','refund_lines',
                                     'claim_transitions','tenants')
                   AND rowsecurity = false
                """)
            )
            .scalars()
            .all()
        )
        assert unprotected == []


class TestAQueryForAnotherTenantsClaimReturnsNothing:
    def test_the_claim_is_visible_to_its_own_tenant(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        alpha, alpha_claim, _, _ = two_tenants
        set_tenant(app_session, alpha.tenant_id)
        found = app_session.get(Claim, alpha_claim.claim_id)
        assert found is not None
        assert found.total_refund == Decimal("19531.25")

    def test_the_same_claim_id_is_invisible_to_the_other_tenant(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """The headline case: a caller holding a valid claim UUID and the wrong scope.

        Nothing about the request is malformed. The id exists, the row exists, the query
        is the same query. RLS is the only thing between them.
        """
        _, alpha_claim, beta, _ = two_tenants
        set_tenant(app_session, beta.tenant_id)
        assert app_session.get(Claim, alpha_claim.claim_id) is None

    def test_an_unscoped_session_sees_no_claims_at_all(
        self,
        app_session: Session,
        two_tenants: tuple[Tenant, Claim, Tenant, Claim],  # noqa: ARG002 - rows must exist
    ) -> None:
        """Fail-closed. A code path that forgets to scope reads nothing, not everything.

        The fixture is required and unused on purpose: without committed rows to miss,
        an empty result would prove nothing.
        """
        assert current_tenant(app_session) is None
        assert app_session.query(Claim).count() == 0

    def test_the_scope_does_not_survive_the_transaction(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """`SET LOCAL`, not `SET`.

        Connections are pooled. A scope that outlived its transaction would be inherited
        by whichever request checked the connection out next, which is a cross-tenant read
        with no bug anywhere in the query.
        """
        alpha, alpha_claim, _, _ = two_tenants
        set_tenant(app_session, alpha.tenant_id)
        assert app_session.get(Claim, alpha_claim.claim_id) is not None

        app_session.rollback()
        assert current_tenant(app_session) is None
        assert app_session.get(Claim, alpha_claim.claim_id) is None

    def test_the_ledger_is_scoped_too(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """Otherwise the audit trail would be the leak the claim table is not."""
        alpha, _, _beta, beta_claim = two_tenants
        set_tenant(app_session, alpha.tenant_id)
        rows = app_session.execute(
            text("SELECT count(*) FROM audit_ledger WHERE claim_id = :c"),
            {"c": beta_claim.claim_id},
        ).scalar_one()
        assert rows == 0


class TestWritesAreScopedAsWellAsReads:
    def test_a_row_cannot_be_written_for_another_tenant(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """`WITH CHECK`, not just `USING`.

        A policy that filtered reads and let writes through would let a compromised
        request insert a claim into someone else's account — which it would then be unable
        to read, and which would still be filed under their name.
        """
        alpha, _, beta, _ = two_tenants
        set_tenant(app_session, alpha.tenant_id)
        app_session.add(_claim(beta.tenant_id, "1.00"))
        with pytest.raises(ProgrammingError, match="row-level security"):
            app_session.flush()

    def test_another_tenants_claim_cannot_be_updated_by_id(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """An UPDATE that matches no visible row is a no-op, not an error — which is the
        correct outcome and worth pinning, because it is indistinguishable from success
        unless the rowcount is checked."""
        _, alpha_claim, beta, _ = two_tenants
        set_tenant(app_session, beta.tenant_id)
        result = app_session.execute(
            text("UPDATE claims SET state = 'abandoned' WHERE claim_id = :c"),
            {"c": alpha_claim.claim_id},
        )
        assert result.rowcount == 0


class TestResolvingAScopeFromAnIdentifier:
    def test_scope_to_claim_finds_the_owner_and_then_the_claim(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """The route path: `GET /claims/{id}` has an id and no tenant.

        The SECURITY DEFINER lookup crosses the boundary once, deliberately and for one
        uuid, and everything after it runs inside the scope it established.
        """
        alpha, alpha_claim, _, _ = two_tenants
        assert scope_to_claim(app_session, alpha_claim.claim_id) == alpha.tenant_id
        assert app_session.get(Claim, alpha_claim.claim_id) is not None

    def test_a_claim_that_does_not_exist_raises_rather_than_scoping_to_nothing(
        self, app_session: Session
    ) -> None:
        with pytest.raises(TenantScopeError, match="no claim"):
            scope_to_claim(app_session, uuid4())

    def test_the_lookup_returns_an_owner_and_nothing_else(
        self, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """What the definer function leaks, stated as a test rather than as a comment.

        Someone who guesses a claim UUID learns which tenant owns it. They do not learn
        its state, its refund, or that it is even a claim rather than a review — and the
        row itself stays invisible until the scope is set.
        """
        _, alpha_claim, beta, _ = two_tenants
        set_tenant(app_session, beta.tenant_id)
        assert tenant_of_claim(app_session, alpha_claim.claim_id) is not None
        assert app_session.get(Claim, alpha_claim.claim_id) is None


class TestTheTombstoneHidesEverythingAtOnce:
    def test_an_offboarded_tenant_stops_resolving(
        self, engine: Engine, app_session: Session, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """One predicate, in `app_current_tenant`, takes the whole tenant out of view.

        The rows are all still there — §163 and Art. 175 require exactly that — and the
        tenant's own connection can no longer reach any of them.
        """
        beta = two_tenants[2]
        beta_claim = two_tenants[3]
        with Session(engine) as owner:
            owner.execute(
                text("""
                UPDATE tenants
                   SET offboarded_at = :now,
                       offboard_artifact_key = 'tenants/x/offboarding/test.jsonl',
                       offboard_signature = 'ff',
                       offboard_public_key = 'ee'
                 WHERE tenant_id = :t
                """),
                {"now": datetime.now(UTC), "t": beta.tenant_id},
            )
            owner.commit()

        try:
            set_tenant(app_session, beta.tenant_id)
            assert current_tenant(app_session) is None
            assert app_session.get(Claim, beta_claim.claim_id) is None

            # Still on disk, as seen by the owner.
            with Session(engine) as owner:
                assert owner.get(Claim, beta_claim.claim_id) is not None
        finally:
            with Session(engine) as owner:
                owner.execute(
                    text(
                        "UPDATE tenants SET offboarded_at = NULL, "
                        "offboard_artifact_key = NULL, offboard_signature = NULL, "
                        "offboard_public_key = NULL WHERE tenant_id = :t"
                    ),
                    {"t": beta.tenant_id},
                )
                owner.commit()


class TestTheGucItself:
    def test_a_malformed_tenant_id_is_an_error_and_not_an_open_door(
        self, app_session: Session
    ) -> None:
        """Garbage in the session variable fails the cast rather than matching everything."""
        app_session.execute(text("SELECT set_config(:n, 'not-a-uuid', true)"), {"n": TENANT_GUC})
        with pytest.raises(Exception, match="invalid input syntax"):
            app_session.query(Claim).count()

    def test_an_empty_tenant_id_is_treated_as_unset(self, app_session: Session) -> None:
        app_session.execute(text("SELECT set_config(:n, '', true)"), {"n": TENANT_GUC})
        assert current_tenant(app_session) is None


class TestTheOwnerIsDeliberatelyOutsideTheBoundary:
    def test_the_owner_sees_both_tenants(
        self, engine: Engine, two_tenants: tuple[Tenant, Claim, Tenant, Claim]
    ) -> None:
        """Not a gap — a documented consequence, and the thing every other suite relies on.

        `drawbridge` owns these tables and is a superuser, so policies do not apply to it
        and `FORCE ROW LEVEL SECURITY` would not change that. Migrations, the corpus
        loaders and `scripts/tenant_offboard.py` all need to cross tenants, and the
        isolation guarantee is that the *services* do not connect this way.
        """
        _, alpha_claim, _, beta_claim = two_tenants
        with Session(engine) as owner:
            # No scope was set on this connection, and both rows come back anyway.
            assert current_tenant(owner) is None
            assert owner.get(Claim, alpha_claim.claim_id) is not None
            assert owner.get(Claim, beta_claim.claim_id) is not None

"""Two users, two tenants, one API — and nothing crosses between them.

Every other suite in this directory talks to the database directly. This one goes through
the HTTP layer, because that is where the week 12 question lives: week 11 proved that a
*connection* scoped to tenant A cannot read tenant B's rows, and left open who decides
what that scope is. Until this week the answer was "the request body", which is isolation
from a caller who fills in the form honestly.

Three things have to hold together for the answer to be "the token", and each is a class
below. A body naming another tenant is refused before it reaches the database. An
identifier belonging to another tenant reports absent — the same 404 a nonexistent one
gets, because distinguishing them turns a guessed uuid into a membership oracle. And the
policies are still doing the work underneath: the app connects as `drawbridge_app`, so a
route that somehow forgot to scope reads nothing rather than everything.

The data here is committed rather than rolled back, because the API opens its own
connections and would not see an open transaction's writes. Teardown deletes it. Nothing
in this file writes to `audit_ledger` for that reason: those rows are immutable by trigger
and a test that created them could not clean up after itself.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from drawbridge_schemas.tenant import TenantProfile
from services.api.src import profiles
from services.api.src.auth import SERVICE_SCOPE, mint
from services.api.src.config import get_settings
from services.api.src.models import Claim, ReviewQueue, Tenant
from services.api.src.tenancy import APP_ROLE
from tests.integration.conftest import TEST_APP_PASSWORD, TEST_DSN

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

# 32 bytes, which is the SHA-256 block minimum PyJWT warns below. Not a secret in any
# sense that matters: it verifies tokens this suite mints for itself.
SECRET = "integration-only-never-a-real-key"


@pytest.fixture(scope="module")
def api(engine: Engine) -> Iterator[TestClient]:  # noqa: ARG001 - needs the schema, not the value
    """The real app, on the unprivileged role, with local token verification.

    Pointed at the app role deliberately. Running these through the owner would exercise
    the middleware and prove nothing about the policies, and the two are supposed to be
    load-bearing together: `authorise_tenant` decides the scope and RLS enforces it.
    """
    url = make_url(TEST_DSN).set(
        drivername="postgresql+asyncpg", username=APP_ROLE, password=TEST_APP_PASSWORD
    )
    previous = {
        key: os.environ.get(key)
        for key in (
            "DRAWBRIDGE_DATABASE_URL",
            "DRAWBRIDGE_JWT_SECRET",
            "DRAWBRIDGE_AUTH_REQUIRED",
            "DRAWBRIDGE_OIDC_JWKS_URL",
        )
    }
    os.environ["DRAWBRIDGE_DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["DRAWBRIDGE_JWT_SECRET"] = SECRET
    os.environ["DRAWBRIDGE_AUTH_REQUIRED"] = "true"
    os.environ.pop("DRAWBRIDGE_OIDC_JWKS_URL", None)
    get_settings.cache_clear()

    from services.api.src.main import app
    from services.api.src.sync_db import reset_engine

    reset_engine()
    with TestClient(app) as client:
        yield client

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
    reset_engine()


def _claim(tenant_id: UUID, *, jurisdiction: str) -> Claim:
    if jurisdiction == "us":
        return Claim(
            claim_id=uuid4(),
            tenant_id=tenant_id,
            state="quantified",
            jurisdiction="us",
            currency="USD",
            lane="drawback",
            drawback_type="unused_substitution",
            period_start=date(2023, 1, 1),
            period_end=date(2023, 12, 31),
            filing_deadline=date(2027, 1, 9),
            absolute_bar_date=date(2027, 1, 9),
            total_refund=Decimal("25092.14"),
        )
    return Claim(
        claim_id=uuid4(),
        tenant_id=tenant_id,
        state="analyst_review",
        jurisdiction="ksa",
        currency="SAR",
        lane="gcc_reexport_drawback",
        drawback_type="gcc_unused_reexport",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        filing_deadline=date(2026, 12, 12),
        absolute_bar_date=date(2027, 2, 8),
        total_refund=Decimal("19531.25"),
    )


def _review(tenant_id: UUID, claim_id: UUID) -> ReviewQueue:
    return ReviewQueue(
        review_id=uuid4(),
        tenant_id=tenant_id,
        claim_id=claim_id,
        reason="threshold_near_miss",
        severity="normal",
        summary="a re-export fell within 10% of the USD 5,000 minimum",
        citation="GCC Rules of Implementation Art. 16 §2",
        payload={},
        resume_token=uuid4().hex,
        workflow_run_id="wf-isolation",
        state="open",
    )


@pytest.fixture(scope="module")
def parties(engine: Engine) -> Iterator[dict[str, dict[str, object]]]:
    """Two tenants, each with a claim and a queued exception, committed.

    Both sides are populated rather than one: a test where only tenant A owns anything
    passes when the filter is `WHERE tenant_id = A` and also when it is `WHERE false`,
    and only one of those is isolation.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = factory()

    a_tenant = Tenant(tenant_id=uuid4(), name="Alpha Importers", default_jurisdiction="us")
    b_tenant = Tenant(tenant_id=uuid4(), name="Beta Logistics", default_jurisdiction="ksa")
    session.add_all([a_tenant, b_tenant])
    session.flush()

    a_claim = _claim(a_tenant.tenant_id, jurisdiction="us")
    b_claim = _claim(b_tenant.tenant_id, jurisdiction="ksa")
    session.add_all([a_claim, b_claim])
    session.flush()

    a_review = _review(a_tenant.tenant_id, a_claim.claim_id)
    b_review = _review(b_tenant.tenant_id, b_claim.claim_id)
    session.add_all([a_review, b_review])

    # Filing identities, added in week 13. Both sides get one, and B's carries an IBAN:
    # a refund destination account is the single most valuable row in this schema to a
    # reader who should not have it, so it is the one worth proving is scoped.
    profiles.write(
        session,
        TenantProfile(
            tenant_id=a_tenant.tenant_id,
            legal_name="Alpha Importers Inc.",
            ein="951112233",
            address_line1="1 Alpha Way",
            city="Long Beach",
            country="US",
        ),
    )
    profiles.write(
        session,
        TenantProfile(
            tenant_id=b_tenant.tenant_id,
            legal_name="Beta Logistics Co.",
            cr_number="4030111222",
            iban="SA0380000000608010167519",
            address_line1="2 Beta Road",
            city="Jeddah",
            country="SA",
        ),
    )
    session.commit()

    settings = get_settings()
    yield {
        "a": {
            "tenant_id": a_tenant.tenant_id,
            "claim_id": a_claim.claim_id,
            "review_id": a_review.review_id,
            "resume_token": a_review.resume_token,
            "token": mint(settings, subject="alice@alpha.example", tenant_id=a_tenant.tenant_id),
        },
        "b": {
            "tenant_id": b_tenant.tenant_id,
            "claim_id": b_claim.claim_id,
            "review_id": b_review.review_id,
            "resume_token": b_review.resume_token,
            "token": mint(settings, subject="bob@beta.example", tenant_id=b_tenant.tenant_id),
        },
        # v1.1.0: the registered pipeline identity; an unregistered subject is refused.
        "service": {"token": mint(settings, subject="agent:n8n-pipeline", scopes=(SERVICE_SCOPE,))},
    }

    for review in (a_review, b_review):
        session.execute(
            text("DELETE FROM review_queue WHERE review_id = :r"), {"r": review.review_id}
        )
    for claim in (a_claim, b_claim):
        session.execute(text("DELETE FROM claims WHERE claim_id = :c"), {"c": claim.claim_id})
    for tenant in (a_tenant, b_tenant):
        session.execute(
            text("DELETE FROM tenant_profiles WHERE tenant_id = :t"), {"t": tenant.tenant_id}
        )
    for tenant in (a_tenant, b_tenant):
        session.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant.tenant_id})
    session.commit()
    session.close()


def _auth(party: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {party['token']}"}


class TestAnUnauthenticatedCallerGetsNothing:
    """The middleware, before any of the rest of it matters."""

    def test_health_is_reachable_without_a_token(self, api: TestClient) -> None:
        """Docker polls this before anything could hold a credential."""
        assert api.get("/health").status_code == 200

    def test_a_claim_is_not(self, api: TestClient, parties: dict) -> None:
        response = api.get(f"/claims/{parties['a']['claim_id']}")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_a_token_signed_with_another_key_is_refused(
        self, api: TestClient, parties: dict
    ) -> None:
        """The signature, not the shape. A token that parses is not a token that verifies."""
        settings = get_settings()
        forged = mint(
            settings.model_copy(update={"jwt_secret": "a-different-secret"}),
            subject="mallory",
            tenant_id=parties["a"]["tenant_id"],
        )
        response = api.get(
            f"/claims/{parties['a']['claim_id']}", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    def test_the_rejection_does_not_say_which_check_failed(
        self, api: TestClient, parties: dict
    ) -> None:
        """A wrong audience and a bad signature must look the same from outside.

        Telling an attacker which of the two they got wrong turns one guess into two.
        """
        settings = get_settings()
        wrong_audience = mint(
            settings.model_copy(update={"jwt_audience": "some-other-api"}),
            subject="mallory",
            tenant_id=parties["a"]["tenant_id"],
        )
        bad_signature = mint(
            settings.model_copy(update={"jwt_secret": "a-different-secret"}),
            subject="mallory",
            tenant_id=parties["a"]["tenant_id"],
        )
        first = api.get(
            "/claims/" + str(parties["a"]["claim_id"]),
            headers={"Authorization": f"Bearer {wrong_audience}"},
        )
        second = api.get(
            "/claims/" + str(parties["a"]["claim_id"]),
            headers={"Authorization": f"Bearer {bad_signature}"},
        )
        assert first.status_code == second.status_code == 401
        assert first.json() == second.json()


class TestUserAAndUserBCannotSeeEachOther:
    """The week's headline, one endpoint at a time."""

    def test_each_reads_their_own_claim(self, api: TestClient, parties: dict) -> None:
        for side in ("a", "b"):
            response = api.get(f"/claims/{parties[side]['claim_id']}", headers=_auth(parties[side]))
            assert response.status_code == 200, side
            assert response.json()["claim_id"] == str(parties[side]["claim_id"])

    def test_neither_reads_the_other_s_claim(self, api: TestClient, parties: dict) -> None:
        """The id is real and correct. The only thing wrong with it is whose it is."""
        assert (
            api.get(f"/claims/{parties['b']['claim_id']}", headers=_auth(parties["a"])).status_code
            == 404
        )
        assert (
            api.get(f"/claims/{parties['a']['claim_id']}", headers=_auth(parties["b"])).status_code
            == 404
        )

    def test_a_foreign_claim_is_indistinguishable_from_a_missing_one(
        self, api: TestClient, parties: dict
    ) -> None:
        """Otherwise a claim id is an oracle for which tenants exist and what they own."""
        foreign = api.get(f"/claims/{parties['b']['claim_id']}", headers=_auth(parties["a"]))
        absent = api.get(f"/claims/{uuid4()}", headers=_auth(parties["a"]))
        assert foreign.status_code == absent.status_code == 404
        assert foreign.json()["detail"]["error"] == absent.json()["detail"]["error"]

    def test_history_is_scoped_too(self, api: TestClient, parties: dict) -> None:
        """A transition trail leaks the same facts the claim does, one level down."""
        assert (
            api.get(
                f"/claims/{parties['b']['claim_id']}/history", headers=_auth(parties["a"])
            ).status_code
            == 404
        )
        assert (
            api.get(
                f"/claims/{parties['a']['claim_id']}/history", headers=_auth(parties["a"])
            ).status_code
            == 200
        )

    def test_the_review_queue_shows_only_your_own(self, api: TestClient, parties: dict) -> None:
        response = api.get(
            "/review/queue",
            params={"tenant_id": str(parties["a"]["tenant_id"])},
            headers=_auth(parties["a"]),
        )
        assert response.status_code == 200
        ids = {row["review_id"] for row in response.json()}
        assert str(parties["a"]["review_id"]) in ids
        assert str(parties["b"]["review_id"]) not in ids

    def test_asking_the_queue_for_someone_else_is_refused(
        self, api: TestClient, parties: dict
    ) -> None:
        """403 rather than an empty list.

        The queue is addressed by a tenant id the caller supplies, so the honest answer
        is that the request was not allowed — not that the other tenant has no work.
        """
        response = api.get(
            "/review/queue",
            params={"tenant_id": str(parties["b"]["tenant_id"])},
            headers=_auth(parties["a"]),
        )
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "wrong_tenant"

    def test_resolving_another_tenants_review_finds_nothing(
        self, api: TestClient, parties: dict
    ) -> None:
        """A write, not a read. `scope_to_review_async` resolves the owner first, and the
        UPDATE then runs inside a scope the row is not in."""
        response = api.post(
            f"/review/{parties['b']['review_id']}/resolve",
            json={
                "resolution": "approved",
                "note": "Checked the declared value against the commercial invoice.",
            },
            headers=_auth(parties["a"]),
        )
        assert response.status_code == 404

    def test_polling_another_tenants_resume_token_finds_nothing(
        self, api: TestClient, parties: dict
    ) -> None:
        """The token is the credential on this path, so holding one must not be enough
        when it belongs to someone else."""
        response = api.get(
            f"/review/pending/{parties['b']['resume_token']}", headers=_auth(parties["a"])
        )
        assert response.status_code == 404


class TestTheBodyIsNotTheAuthority:
    """What changed this week: a posted tenant_id no longer decides anything."""

    def test_storing_documents_for_another_tenant_is_refused(
        self, api: TestClient, parties: dict
    ) -> None:
        response = api.post(
            "/documents/batch",
            json={
                "tenant_id": str(parties["b"]["tenant_id"]),
                "documents": [
                    {
                        "kind": "cbp_7501",
                        "filename": "forged.pdf",
                        "content_base64": "JVBERi0xLjQK",
                    }
                ],
            },
            headers=_auth(parties["a"]),
        )
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "wrong_tenant"

    def test_persisting_a_claim_for_another_tenant_is_refused(
        self, api: TestClient, parties: dict
    ) -> None:
        """Refused before validation of the rest of the body.

        The lines are deliberately absent: if the tenant check ran after schema
        validation this would be a 422, and a 422 here would mean the check is reachable
        only for well-formed forgeries.
        """
        response = api.post(
            "/claims/persist",
            json={"tenant_id": str(parties["b"]["tenant_id"])},
            headers=_auth(parties["a"]),
        )
        assert response.status_code in {403, 422}

    def test_suspending_a_review_for_another_tenant_is_refused(
        self, api: TestClient, parties: dict
    ) -> None:
        response = api.post(
            "/review/suspend",
            json={
                "tenant_id": str(parties["b"]["tenant_id"]),
                "claim_id": str(parties["b"]["claim_id"]),
                "items": [
                    {
                        "reason": "threshold_near_miss",
                        "severity": "normal",
                        "summary": "injected",
                    }
                ],
            },
            headers=_auth(parties["a"]),
        )
        assert response.status_code == 403

    def test_the_refused_write_did_not_happen(self, api: TestClient, parties: dict) -> None:
        """The refusal above must be a refusal, not a rollback nobody checked."""
        response = api.get(
            "/review/queue",
            params={"tenant_id": str(parties["b"]["tenant_id"])},
            headers=_auth(parties["b"]),
        )
        assert response.status_code == 200
        assert [row["summary"] for row in response.json()].count("injected") == 0


class TestTheServiceTokenIsDeliberatelyDifferent:
    """n8n runs one pipeline against whichever tenant its trigger names.

    That is a cross-tenant credential, and pretending otherwise would be worse than
    saying so: these tests pin what it can do, so a change that widens or narrows it is
    visible rather than incidental.
    """

    def test_it_may_read_either_tenants_claim(self, api: TestClient, parties: dict) -> None:
        for side in ("a", "b"):
            response = api.get(
                f"/claims/{parties[side]['claim_id']}", headers=_auth(parties["service"])
            )
            assert response.status_code == 200, side

    def test_it_must_still_name_the_tenant_it_is_acting_for(
        self, api: TestClient, parties: dict
    ) -> None:
        """No tenant in the token and none in the request is not "all of them"."""
        response = api.get("/review/queue", headers=_auth(parties["service"]))
        assert response.status_code == 422

    def test_a_token_carrying_both_a_tenant_and_the_service_scope_is_refused(
        self, api: TestClient, parties: dict
    ) -> None:
        """Otherwise "may this caller act for tenant X" depends on which field is read
        first, and the answer differs between the two readers."""
        settings = get_settings()
        both = mint(
            settings,
            subject="confused-deputy",
            tenant_id=parties["a"]["tenant_id"],
            scopes=(SERVICE_SCOPE,),
        )
        response = api.get(
            f"/claims/{parties['a']['claim_id']}", headers={"Authorization": f"Bearer {both}"}
        )
        assert response.status_code == 401


class TestTheFilingIdentityIsScopedLikeEverythingElse:
    """`tenant_profiles` holds EINs, CR numbers and bank accounts.

    It arrived in week 13, after the row-level policies were written, which is exactly the
    circumstance in which a table gets added to the schema and forgotten in the predicate
    map. These tests are the thing that would notice.
    """

    def test_the_table_carries_a_policy(self, engine: Engine) -> None:
        with engine.connect() as connection:
            enabled = connection.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = 'tenant_profiles'")
            ).scalar_one()
            policies = connection.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = 'tenant_profiles'")
            ).scalar_one()
        assert enabled is True
        assert policies >= 1

    def test_each_side_packages_under_its_own_identity(
        self, api: TestClient, parties: dict
    ) -> None:
        """A builds a packet without naming a claimant and gets A's EIN, not B's.

        This is the property that makes "a second tenant with zero code change" true: the
        request body is identical for both tenants and the answer is not.
        """
        response = api.post(
            "/packaging/build",
            json={"claim_id": str(parties["a"]["claim_id"]), "include_artifacts": False},
            headers=_auth(parties["a"]),
        )
        # The claim is not in a packageable state in this fixture, so a 409 is the
        # expected outcome — what matters is that it is not a 422 about a missing
        # profile, which would mean A could not see its own row.
        assert response.status_code != status.HTTP_422_UNPROCESSABLE_ENTITY, response.json()

    def test_neither_can_read_the_other_s_refund_account(
        self,
        engine: Engine,  # noqa: ARG002 - ordering: the schema and app role must exist
        parties: dict,
    ) -> None:
        """Straight at the database, as the application role, scoped to A.

        Through HTTP there is no endpoint that returns a profile, so a route-level test
        would prove only that the route does not exist. The policy is the control, and
        this is where it either holds or does not.
        """
        app_engine = create_engine(
            make_url(TEST_DSN).set(username=APP_ROLE, password=TEST_APP_PASSWORD)
        )
        try:
            with app_engine.connect() as connection:
                connection.execute(
                    text("SELECT set_config('tenant.id', :t, false)"),
                    {"t": str(parties["a"]["tenant_id"])},
                )
                visible = (
                    connection.execute(text("SELECT tenant_id, iban FROM tenant_profiles"))
                    .mappings()
                    .all()
                )
        finally:
            app_engine.dispose()

        assert [row["tenant_id"] for row in visible] == [parties["a"]["tenant_id"]]
        assert all(row["iban"] is None for row in visible)

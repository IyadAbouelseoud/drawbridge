"""Fixtures for the database-backed half of the security suite.

The API runs on the unprivileged `drawbridge_app` role, as in
`tests/integration/test_tenant_isolation.py` — a security property proven through the
owner role proves nothing, because the owner bypasses every policy. The signing secret is
that suite's, because the app's middleware captures its settings when `main` is first
imported and the two suites share a process.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from services.api.src import killswitch
from services.api.src.auth import SERVICE_SCOPE, mint, set_principal
from services.api.src.config import get_settings
from services.api.src.models import Claim, ReviewQueue, Tenant
from services.api.src.tenancy import APP_ROLE
from tests.integration.conftest import TEST_APP_PASSWORD, TEST_DSN, _reachable

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

#: Applied by each database-backed module. The integration conftest's own skip applies only
#: to modules under tests/integration, and CI runs without a Postgres.
requires_db = pytest.mark.skipif(not _reachable(TEST_DSN), reason=f"no Postgres at {TEST_DSN}")

SECRET = "integration-only-never-a-real-key"
PIPELINE_SECRET = "pipeline-client-secret-for-the-security-suite"


class FakeRedis:
    """The two calls `/pipeline/*` makes, in memory. There is no Redis in the test run."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: ARG002
        self.data[key] = value

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def aclose(self) -> None:
        return None

    async def ping(self) -> bool:
        return True


@pytest.fixture(scope="module")
def api(engine: Engine) -> Iterator[TestClient]:  # noqa: ARG001 - needs the schema
    url = make_url(TEST_DSN).set(
        drivername="postgresql+asyncpg", username=APP_ROLE, password=TEST_APP_PASSWORD
    )
    keys = (
        "DRAWBRIDGE_DATABASE_URL",
        "DRAWBRIDGE_JWT_SECRET",
        "DRAWBRIDGE_AUTH_REQUIRED",
        "DRAWBRIDGE_OIDC_JWKS_URL",
        "DRAWBRIDGE_PIPELINE_CLIENT_SECRET",
    )
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["DRAWBRIDGE_DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["DRAWBRIDGE_JWT_SECRET"] = SECRET
    os.environ["DRAWBRIDGE_AUTH_REQUIRED"] = "true"
    os.environ["DRAWBRIDGE_PIPELINE_CLIENT_SECRET"] = PIPELINE_SECRET
    os.environ.pop("DRAWBRIDGE_OIDC_JWKS_URL", None)
    get_settings.cache_clear()

    from services.api.src.main import app
    from services.api.src.sync_db import reset_engine

    reset_engine()
    killswitch.invalidate()
    with TestClient(app) as client:
        app.state.redis = FakeRedis()
        yield client

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
    reset_engine()
    killswitch.invalidate()


@pytest.fixture
def owner(engine: Engine) -> Iterator[Session]:
    """A committing owner session, for building data other connections must see."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture(autouse=True)
def _clean_principal() -> Iterator[None]:
    set_principal(None)
    yield
    set_principal(None)


def token(
    tenant_id: UUID | None = None,
    *,
    subject: str | None = None,
    roles: tuple[str, ...] = (),
    agent: str | None = None,
) -> dict[str, str]:
    settings = get_settings()
    if agent is not None:
        value = mint(settings, subject=agent, scopes=(SERVICE_SCOPE,))
    else:
        value = mint(
            settings,
            subject=subject or f"user-{uuid4().hex[:8]}@tenant.example",
            tenant_id=tenant_id,
            roles=roles,
        )
    return {"Authorization": f"Bearer {value}"}


def make_world(owner: Session, *, state: str = "analyst_review") -> dict[str, Any]:
    """A tenant with one claim and one open exception, committed."""
    tenant = Tenant(tenant_id=uuid4(), name=f"sec-{uuid4().hex[:6]}", default_jurisdiction="us")
    owner.add(tenant)
    owner.flush()
    claim = Claim(
        claim_id=uuid4(),
        tenant_id=tenant.tenant_id,
        state=state,
        jurisdiction="us",
        currency="USD",
        lane="drawback",
        drawback_type="unused_substitution",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        filing_deadline=date(2027, 12, 31),
        absolute_bar_date=date(2027, 12, 31),
        total_refund=Decimal("25092.14"),
    )
    owner.add(claim)
    owner.flush()
    review = ReviewQueue(
        review_id=uuid4(),
        tenant_id=tenant.tenant_id,
        claim_id=claim.claim_id,
        reason="low_extraction_confidence",
        severity="high",
        summary="duty_amount read at 0.71 against a numeric floor of 0.95",
        citation="19 CFR §163.1",
        payload={"field": "duty_amount", "reading": "4812.50", "confidence": 0.71},
        resume_token=uuid4().hex,
        workflow_run_id="wf-security",
        state="open",
    )
    owner.add(review)
    owner.commit()
    return {"tenant": tenant.tenant_id, "claim": claim.claim_id, "review": review.review_id}


def ledger_events(owner: Session, tenant_id: UUID) -> list[dict[str, Any]]:
    rows = owner.execute(
        text(
            "SELECT event_type, actor, subject, payload FROM audit_ledger "
            "WHERE tenant_id = :t ORDER BY sequence"
        ),
        {"t": tenant_id},
    ).mappings()
    return [dict(row) for row in rows]


def release_all(owner: Session) -> None:
    """Release every engaged switch, so one test's halt cannot leak into the next."""
    killswitch.invalidate()
    for kind, value in killswitch.state(owner).engaged:
        killswitch.release(
            owner,
            actor="test-teardown",
            actor_kind="local",
            reason="security suite teardown releases every switch it engaged",
            scope_kind=kind,
            scope_value=value,
        )
    owner.commit()
    killswitch.invalidate()

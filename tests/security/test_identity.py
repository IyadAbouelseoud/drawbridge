"""Identity and least privilege: the registry's invariants, and what the verifier refuses.

`tests/unit/test_week12.py` covers the week-12 contract (signature, audience, issuer,
tenant claim). This file covers what v1.1.0 added on top: every machine principal is a
registered identity with an owner and a ceiling, tokens are short-lived by verifier
decree, and what a caller may do is a closed set derived from the registry or its roles.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import jwt
import pytest
from pydantic import ValidationError

from drawbridge_schemas.agents import (
    AGENTS,
    HUMAN_ONLY_SCOPES,
    ROLE_SCOPES,
    AgentIdentity,
    AgentKind,
    OwnerRole,
    Role,
    Scope,
)
from services.api.src.auth import (
    MAX_TOKEN_CHARS,
    SERVICE_SCOPE,
    AuthError,
    Principal,
    actor_name,
    check_identity_posture,
    decode,
    mint,
    set_principal,
)
from services.api.src.config import Settings
from services.api.src.secrets import SecretsError, check_secret_posture

SECRET = "identity-suite-secret-long-enough-for-hs256-x"


@pytest.fixture
def settings() -> Settings:
    return Settings(jwt_secret=SECRET, auth_required=True, environment="development")


@pytest.fixture
def production() -> Settings:
    return Settings(
        jwt_secret=SECRET,
        auth_required=True,
        environment="production",
        owner_platform="Platform Lead <platform@example.com>",
        owner_compliance="Compliance Lead <compliance@example.com>",
        owner_security="Security Lead <security@example.com>",
    )


@pytest.fixture(autouse=True)
def _no_principal() -> Iterator[None]:
    set_principal(None)
    yield
    set_principal(None)


def _raw(settings: Settings, **claims: Any) -> str:
    now = int(time.time())
    base = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode({**base, **claims}, SECRET, algorithm="HS256")


class TestTheRegistry:
    def test_ids_are_unique_and_well_formed(self) -> None:
        assert len(AGENTS) == len({a.agent_id for a in AGENTS.values()})
        assert all(agent_id.startswith("agent:") for agent_id in AGENTS)

    def test_every_identity_has_an_accountable_owner(self) -> None:
        assert all(isinstance(a.owner, OwnerRole) for a in AGENTS.values())

    def test_no_machine_identity_holds_a_human_only_scope(self) -> None:
        for identity in AGENTS.values():
            assert not identity.scopes & HUMAN_ONLY_SCOPES, identity.agent_id

    def test_the_registry_refuses_to_register_one_that_would(self) -> None:
        with pytest.raises(ValidationError, match="human-only"):
            AgentIdentity(
                agent_id="agent:rogue",
                display_name="rogue",
                purpose="an agent that would like to approve its own claims",
                kind=AgentKind.API_CLIENT,
                owner=OwnerRole.PLATFORM,
                scopes=frozenset({Scope.CLAIMS_APPROVE}),
                cross_tenant=True,
            )

    def test_bearer_token_identities_are_short_lived(self) -> None:
        for identity in AGENTS.values():
            if identity.kind is AgentKind.API_CLIENT:
                assert identity.max_token_ttl_seconds <= 900, identity.agent_id

    def test_only_the_backup_job_holds_the_owner_dsn(self) -> None:
        assert [a.agent_id for a in AGENTS.values() if a.holds_owner_dsn] == ["agent:retention"]

    def test_the_harness_exists_only_in_development(self) -> None:
        assert AGENTS["agent:e2e-harness"].environments == frozenset({"development"})

    def test_the_drafter_can_only_read_and_draft(self) -> None:
        assert AGENTS["agent:memo-drafter"].scopes == {Scope.REVIEW_READ, Scope.REVIEW_DRAFT}

    def test_the_operator_role_and_the_approver_role_do_not_overlap_on_money(self) -> None:
        assert Scope.CONTROL_KILL not in ROLE_SCOPES[Role.APPROVER]
        assert Scope.CLAIMS_RELEASE not in ROLE_SCOPES[Role.OPERATOR]
        assert Scope.CLAIMS_APPROVE not in ROLE_SCOPES[Role.OPERATOR]


class TestMachineTokens:
    def test_a_registered_agent_gets_exactly_its_registered_scopes(
        self, settings: Settings
    ) -> None:
        principal = decode(
            mint(settings, subject="agent:n8n-pipeline", scopes=(SERVICE_SCOPE,)), settings
        )
        assert principal.agent_id == "agent:n8n-pipeline"
        assert principal.effective_permissions == {
            s.value for s in AGENTS["agent:n8n-pipeline"].scopes
        }

    def test_an_unregistered_subject_is_refused(self, settings: Settings) -> None:
        with pytest.raises(AuthError, match="registered agent"):
            decode(mint(settings, subject="agent:made-up", scopes=(SERVICE_SCOPE,)), settings)

    def test_a_service_scope_on_an_ordinary_subject_is_refused(self, settings: Settings) -> None:
        """The day-long `n8n` token this release retired would fail here."""
        with pytest.raises(AuthError, match="registered agent"):
            decode(mint(settings, subject="n8n", scopes=(SERVICE_SCOPE,)), settings)

    def test_a_token_cannot_widen_its_identity(self, settings: Settings) -> None:
        token = mint(
            settings,
            subject="agent:n8n-pipeline",
            scopes=(SERVICE_SCOPE, Scope.CLAIMS_RELEASE.value, Scope.CLAIMS_READ.value),
        )
        principal = decode(token, settings)
        assert principal.effective_permissions == {Scope.CLAIMS_READ.value}
        assert not principal.can(Scope.CLAIMS_RELEASE)

    @pytest.mark.parametrize("agent_id", ["agent:memo-drafter", "agent:mcp-claims"])
    def test_identities_that_never_hold_a_token_cannot_present_one(
        self, settings: Settings, agent_id: str
    ) -> None:
        with pytest.raises(AuthError, match="does not authenticate with a bearer token"):
            decode(mint(settings, subject=agent_id, scopes=(SERVICE_SCOPE,)), settings)

    def test_the_harness_is_refused_outside_development(self, production: Settings) -> None:
        with pytest.raises(AuthError, match="environment"):
            decode(
                mint(production, subject="agent:e2e-harness", scopes=(SERVICE_SCOPE,)),
                production,
            )

    def test_a_machine_token_longer_than_its_ceiling_is_refused(self, settings: Settings) -> None:
        """Signed, unexpired, correctly addressed — and a day long. Refused on lifetime."""
        token = mint(
            settings, subject="agent:n8n-pipeline", scopes=(SERVICE_SCOPE,), ttl_seconds=86400
        )
        with pytest.raises(AuthError, match="lifetime"):
            decode(token, settings)


class TestHumanTokens:
    def test_roles_decide_permissions(self, settings: Settings) -> None:
        tenant = uuid4()
        principal = decode(
            mint(settings, subject="bob", tenant_id=tenant, roles=("approver",)), settings
        )
        assert principal.can(Scope.CLAIMS_RELEASE)
        assert not principal.can(Scope.CONTROL_KILL)

    def test_no_roles_means_analyst_in_development(self, settings: Settings) -> None:
        principal = decode(mint(settings, subject="alice", tenant_id=uuid4()), settings)
        assert principal.roles == {"analyst"}
        assert not principal.can(Scope.CLAIMS_RELEASE)

    def test_no_roles_means_read_only_everywhere_else(self, production: Settings) -> None:
        principal = decode(mint(production, subject="alice", tenant_id=uuid4()), production)
        assert principal.roles == {"auditor"}
        assert not principal.can(Scope.CLAIMS_TRANSITION)
        assert principal.can(Scope.CLAIMS_READ)

    def test_an_unknown_role_grants_nothing(self, production: Settings) -> None:
        principal = decode(
            mint(production, subject="eve", tenant_id=uuid4(), roles=("superadmin",)), production
        )
        assert principal.roles == {"auditor"}

    def test_a_human_token_longer_than_an_hour_is_refused(self, settings: Settings) -> None:
        token = mint(settings, subject="alice", tenant_id=uuid4(), ttl_seconds=7200)
        with pytest.raises(AuthError, match="lifetime"):
            decode(token, settings)

    def test_a_token_without_iat_is_refused(self, settings: Settings) -> None:
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "alice",
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "exp": now + 60,
                "tenant_id": str(uuid4()),
            },
            SECRET,
            algorithm="HS256",
        )
        with pytest.raises(AuthError):
            decode(token, settings)

    def test_a_token_issued_in_the_future_is_refused(self, settings: Settings) -> None:
        future = int(time.time()) + 3600
        token = _raw(settings, sub="alice", tenant_id=str(uuid4()), iat=future, exp=future + 60)
        # PyJWT refuses a future `iat` itself (ImmatureSignatureError) before the lifetime
        # check runs; `_check_lifetime` repeats it for issuers whose library does not.
        with pytest.raises(AuthError):
            decode(token, settings)


class TestWhoIsRecorded:
    def test_the_verified_subject_beats_the_declared_one(self) -> None:
        set_principal(Principal(subject="alice@tenant", tenant_id=uuid4()))
        assert actor_name("analyst") == "alice@tenant"

    def test_the_declared_one_survives_only_without_a_principal(self) -> None:
        assert actor_name("cli-operator") == "cli-operator"


class TestStartupPosture:
    def test_production_refuses_unowned_agents(self) -> None:
        with pytest.raises(RuntimeError, match="accountable"):
            check_identity_posture(
                Settings(jwt_secret=SECRET, environment="production", owner_platform="x")
            )

    def test_production_accepts_bound_owners(self, production: Settings) -> None:
        check_identity_posture(production)

    def test_a_short_signing_key_is_refused_in_production(self) -> None:
        with pytest.raises(SecretsError, match="jwt_secret"):
            check_secret_posture(Settings(jwt_secret="short", environment="production"))

    def test_docs_are_off_outside_development(self, production: Settings) -> None:
        assert production.docs_enabled is False
        assert Settings(environment="development").docs_enabled is True

    def test_an_oversized_token_is_not_parsed(self) -> None:
        assert MAX_TOKEN_CHARS <= 16384

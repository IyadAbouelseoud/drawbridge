"""Token verification and trace stamping, without a database or a transport.

The integration suite proves two tenants cannot reach each other through the API. These
are the pieces underneath it, tested where their edges are cheap to reach: what `decode`
accepts and refuses, what `authorise_tenant` does with each shape of principal, and
whether a trace id exists to be recorded.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException

from services.api.src.auth import (
    SERVICE_SCOPE,
    AuthError,
    Principal,
    authorise_tenant,
    check_auth_configuration,
    decode,
    expected_tenant,
    mint,
    set_principal,
)
from services.api.src.config import Settings
from services.api.src.telemetry import configure_tracing, current_trace_id, tracer

if TYPE_CHECKING:
    from collections.abc import Iterator

SECRET = "unit-test-secret-not-a-real-key!!"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        jwt_secret=SECRET,
        jwt_issuer="drawbridge",
        jwt_audience="drawbridge-api",
        auth_required=True,
        oidc_jwks_url=None,
    )


@pytest.fixture(autouse=True)
def _no_leaked_principal() -> Iterator[None]:
    """Every test starts with no caller.

    The principal lives in a context variable, so a test that sets one and does not clear
    it would make the next test pass for the wrong reason.
    """
    set_principal(None)
    yield
    set_principal(None)


class TestWhatDecodeAccepts:
    def test_a_user_token_carries_its_tenant(self, settings: Settings) -> None:
        tenant = uuid4()
        principal = decode(mint(settings, subject="alice", tenant_id=tenant), settings)
        assert principal.tenant_id == tenant
        assert principal.is_service is False

    def test_a_service_token_carries_no_tenant(self, settings: Settings) -> None:
        principal = decode(mint(settings, subject="n8n", scopes=(SERVICE_SCOPE,)), settings)
        assert principal.tenant_id is None
        assert principal.is_service is True

    def test_scopes_are_read_from_either_spelling(self, settings: Settings) -> None:
        """OAuth 2 says a space-delimited string; Authentik mappings usually emit a list.

        Accepting both is cheaper than constraining every issuer, and getting it wrong
        would silently downgrade a service token to a user one with no tenant — which
        fails later, somewhere less obvious.
        """
        now = int(time.time())
        base = {
            "sub": "n8n",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": now + 60,
        }
        as_list = jwt.encode({**base, "scopes": [SERVICE_SCOPE]}, SECRET, algorithm="HS256")
        as_string = jwt.encode({**base, "scope": SERVICE_SCOPE}, SECRET, algorithm="HS256")
        assert decode(as_list, settings).is_service
        assert decode(as_string, settings).is_service


class TestWhatDecodeRefuses:
    def test_another_key(self, settings: Settings) -> None:
        forged = mint(
            settings.model_copy(update={"jwt_secret": "some-other-secret-entirely!!!!!!"}),
            subject="mallory",
            tenant_id=uuid4(),
        )
        with pytest.raises(AuthError):
            decode(forged, settings)

    def test_another_audience(self, settings: Settings) -> None:
        """A token minted for a different service must not open this one."""
        other = mint(
            settings.model_copy(update={"jwt_audience": "some-other-api"}),
            subject="alice",
            tenant_id=uuid4(),
        )
        with pytest.raises(AuthError):
            decode(other, settings)

    def test_another_issuer(self, settings: Settings) -> None:
        other = mint(
            settings.model_copy(update={"jwt_issuer": "not-drawbridge"}),
            subject="alice",
            tenant_id=uuid4(),
        )
        with pytest.raises(AuthError):
            decode(other, settings)

    def test_an_expired_token(self, settings: Settings) -> None:
        with pytest.raises(AuthError, match="expired"):
            decode(mint(settings, subject="alice", tenant_id=uuid4(), ttl_seconds=-1), settings)

    def test_an_unsigned_token(self, settings: Settings) -> None:
        """`alg: none`, the oldest JWT bug there is.

        `decode` fixes the algorithm list per key source rather than reading the header,
        so this is refused by construction — the test is here to keep it that way.
        """
        now = int(time.time())
        unsigned = jwt.encode(
            {
                "sub": "mallory",
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "exp": now + 60,
                "tenant_id": str(uuid4()),
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(AuthError):
            decode(unsigned, settings)

    def test_a_user_token_with_no_tenant(self, settings: Settings) -> None:
        """Falling back to "any tenant" here is how a bug becomes a breach."""
        with pytest.raises(AuthError, match="tenant_id"):
            decode(mint(settings, subject="alice"), settings)

    def test_a_tenant_claim_that_is_not_a_uuid(self, settings: Settings) -> None:
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "alice",
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "exp": now + 60,
                "tenant_id": "'; DROP TABLE claims; --",
            },
            SECRET,
            algorithm="HS256",
        )
        with pytest.raises(AuthError, match="not a uuid"):
            decode(token, settings)

    def test_a_token_that_is_both_a_user_and_a_service(self, settings: Settings) -> None:
        with pytest.raises(AuthError, match="must not carry a tenant"):
            decode(
                mint(settings, subject="x", tenant_id=uuid4(), scopes=(SERVICE_SCOPE,)), settings
            )


class TestWhichTenantAResultMayActFor:
    def test_a_user_gets_their_own(self) -> None:
        tenant = uuid4()
        set_principal(Principal(subject="alice", tenant_id=tenant))
        assert authorise_tenant(tenant) == tenant
        assert authorise_tenant(None) == tenant
        assert expected_tenant() == tenant

    def test_a_user_naming_someone_else_is_refused(self) -> None:
        set_principal(Principal(subject="alice", tenant_id=uuid4()))
        with pytest.raises(HTTPException) as caught:
            authorise_tenant(uuid4())
        assert caught.value.status_code == 403

    def test_a_service_acts_for_whoever_it_names(self) -> None:
        target = uuid4()
        set_principal(Principal(subject="n8n", tenant_id=None, scopes=frozenset({SERVICE_SCOPE})))
        assert authorise_tenant(target) == target

    def test_a_service_that_names_nobody_is_refused(self) -> None:
        """No tenant in the token and none in the request is not "all of them"."""
        set_principal(Principal(subject="n8n", tenant_id=None, scopes=frozenset({SERVICE_SCOPE})))
        with pytest.raises(HTTPException) as caught:
            authorise_tenant(None)
        assert caught.value.status_code == 422

    def test_a_service_constrains_nothing_by_identifier(self) -> None:
        """`expected_tenant` is the constraint on `GET /claims/{id}`; a service run is
        addressed by claim id four times in one workflow and cannot carry one."""
        set_principal(Principal(subject="n8n", tenant_id=None, scopes=frozenset({SERVICE_SCOPE})))
        assert expected_tenant() is None


class TestTheConfigurationRefusesTheUselessCase:
    def test_required_with_no_key_will_not_start(self) -> None:
        """The same posture as rls_bootstrap: a control that is installed and inert is
        worse than one that is absent, because it looks finished."""
        with pytest.raises(RuntimeError, match="no key is configured"):
            check_auth_configuration(
                Settings(auth_required=True, jwt_secret=None, oidc_jwks_url=None)
            )

    def test_a_jwks_url_alone_is_enough(self) -> None:
        check_auth_configuration(
            Settings(
                auth_required=True,
                jwt_secret=None,
                oidc_jwks_url="https://id.example/application/o/drawbridge/jwks/",
            )
        )

    def test_disabled_is_allowed_and_loud(self) -> None:
        check_auth_configuration(Settings(auth_required=False, jwt_secret=None))

    def test_minting_without_a_secret_is_refused(self) -> None:
        """In a deployment Authentik issues tokens and this process has no key. A second
        issuer nobody registered is a second way in."""
        with pytest.raises(RuntimeError, match="only Authentik"):
            mint(Settings(jwt_secret=None), subject="alice", tenant_id=uuid4())


class TestTheTraceIdTheLedgerRecords:
    def test_there_is_none_outside_a_span(self) -> None:
        configure_tracing("unit-test")
        assert current_trace_id() is None

    def test_inside_a_span_it_is_32_hex_characters(self) -> None:
        """The column is String(32) and an auditor pastes the value into a trace UI, so
        the format is part of the contract rather than an implementation detail."""
        configure_tracing("unit-test")
        with tracer("test").start_as_current_span("work"):
            trace_id = current_trace_id()
        assert trace_id is not None
        assert len(trace_id) == 32
        assert int(trace_id, 16) != 0

    def test_two_spans_in_one_trace_share_it(self) -> None:
        """Otherwise the id in the ledger identifies a step rather than a run, and the
        thing an auditor wants is every row a single pipeline run wrote."""
        configure_tracing("unit-test")
        with tracer("test").start_as_current_span("outer"):
            outer = current_trace_id()
            with tracer("test").start_as_current_span("inner"):
                inner = current_trace_id()
        assert outer == inner

    def test_configuring_twice_is_a_no_op(self) -> None:
        """The MCP servers import the API package; a second provider would be ignored by
        OpenTelemetry with a warning, and spans would go to the first one silently."""
        first = configure_tracing("unit-test")
        assert configure_tracing("unit-test-again") is first

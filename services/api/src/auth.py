"""Who the caller is — the half of multi-tenancy week 11's RLS deliberately left open.

Week 11 answered *which rows may this connection see* and said plainly that nothing
answered *who is this caller*. This does. The two halves meet in exactly one place: the
tenant this module extracts from a verified token is the tenant `tenancy.set_tenant`
writes into `tenant.id`, and every row-level policy compares against that.

**What the middleware does and does not do.** It verifies the bearer token and puts the
resulting `Principal` on a context variable for the duration of the request. It does not
execute `SET LOCAL tenant.id` itself, and the shape of the request path is why: a request
here does not hold one connection. It opens a session per unit of work — some async on
`app.state`, some synchronous in a worker thread — and `SET LOCAL` is transaction-scoped
by design, because a value set outside a transaction survives the connection's return to
the pool and would arrive on somebody else's next request. So the scope statement stays
where the transaction is, in `sync_session` and `set_tenant_async`, and what changes here
is where those callers get the tenant from: the token, rather than the request body.

**That change is the security property.** Before this, `POST /claims/persist` scoped the
transaction to `body.tenant_id`, so the policies compared each row against a value the
caller supplied — isolation from a caller who filled in the form honestly. The token is
now authoritative and a body that disagrees with it is refused.

**Two kinds of principal.** A *user* principal is bound to one tenant by the `tenant_id`
claim. A *service* principal carries `drawbridge:service` in `scopes` and no tenant, and
may act for any of them: n8n runs one pipeline against whichever tenant its trigger names,
and minting it a token per tenant would put tenant credentials in a workflow file. A
service token is therefore a cross-tenant credential. Since v1.1.0 it names a registered
agent, lives fifteen minutes, and is obtained per run from `/auth/token` rather than held;
`tests/integration/test_tenant_isolation.py` proves the *user* path, which is the one that
faces a human.

**Verification.** RS256 against Authentik's JWKS in a real deployment; HS256 against a
shared secret for local work, where standing up an identity provider to run the test suite
would be its own kind of dishonesty. Both paths check `iss`, `aud` and `exp`; the
difference is where the key comes from.

**v1.1.0: who, not just which tenant.** A machine token must name a registered identity in
`sub` (`drawbridge_schemas.agents`), and what it may do is that identity's scopes — the
token can narrow them and never widen them. A human token carries `roles`, and a token
without them gets the environment's default role, which outside development is read-only.
Every token must carry `iat`, and `exp - iat` may not exceed the ceiling for its kind of
principal: fifteen minutes for the pipeline, an hour for a person. That is enforced here
rather than requested of the issuer, because a lifetime limit that depends on every issuer
remembering it is a limit on the issuers that remembered.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import jwt
import structlog
from fastapi import HTTPException, status
from starlette.responses import JSONResponse

from drawbridge_schemas.agents import (
    AgentKind,
    Role,
    Scope,
    agent,
    is_agent_subject,
    scopes_for_roles,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import ASGIApp, Receive, Send
    from starlette.types import Scope as ASGIScope

    from services.api.src.config import Settings

log = structlog.get_logger()

SERVICE_SCOPE = "drawbridge:service"

# Reachable without a token. Health and readiness are polled by Docker before anything
# could hold a credential; the schema endpoints describe the API rather than any tenant's
# data.
#
# `/auth/token` is here because it authenticates by client secret rather than by bearer
# token — it is the place a bearer token comes from. The documentation paths are listed
# but only mounted in development (`Settings.docs_enabled`), so outside it they 404.
PUBLIC_PATHS = frozenset({"/health", "/ready", "/docs", "/redoc", "/openapi.json", "/auth/token"})

#: The longest bearer token the verifier will parse. See `AuthMiddleware`.
MAX_TOKEN_CHARS = 8192

#: Clock skew tolerated on `exp`, `iat` and the lifetime ceiling. Thirty seconds covers
#: NTP drift between an issuer and this host without meaningfully extending a token.
LEEWAY_SECONDS = 30

#: The claim a human token's roles arrive in. Authentik's scope mapping emits it from the
#: user's `drawbridge_roles` attribute (infra/authentik_bootstrap.py).
ROLES_CLAIM = "roles"

_SCOPE_VALUES = frozenset(scope.value for scope in Scope)
_ROLE_VALUES = frozenset(role.value for role in Role)


class AuthError(Exception):
    """A token that cannot be trusted. Always a 401, never a 403.

    The distinction is load-bearing for the caller: 401 means *authenticate again* and n8n
    should refresh and retry, 403 means *you may not do this* and retrying is pointless.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified caller.

    `tenant_id` is None exactly when `is_service` is true. A token claiming both is
    rejected in `decode` rather than resolved here, because "may this caller act for
    tenant X" must not have two answers depending on which field is read first.
    """

    subject: str
    tenant_id: UUID | None
    scopes: frozenset[str] = field(default_factory=frozenset)
    email: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    #: The registered identity, for a machine principal. Equal to `subject` when set.
    agent_id: str | None = None
    #: The token's `jti`, where the issuer supplied one — what an access log line and a
    #: ledger row can quote to name the exact credential that acted.
    token_id: str | None = None
    #: What this principal may do, as `Scope` values. Computed once by `decode`; a
    #: principal constructed directly (tests, in-process callers) derives it on read.
    permissions: frozenset[str] | None = None

    @property
    def is_service(self) -> bool:
        return SERVICE_SCOPE in self.scopes

    @property
    def is_machine(self) -> bool:
        return self.agent_id is not None or self.is_service

    @property
    def is_human(self) -> bool:
        return not self.is_machine

    @property
    def effective_permissions(self) -> frozenset[str]:
        if self.permissions is not None:
            return self.permissions
        if self.is_machine:
            identity = agent(self.agent_id or self.subject)
            return frozenset(s.value for s in identity.scopes) if identity else frozenset()
        known = {Role(r) for r in self.roles if r in _ROLE_VALUES}
        return frozenset(s.value for s in scopes_for_roles(known))

    def can(self, scope: Scope) -> bool:
        return scope.value in self.effective_permissions


_principal: ContextVar[Principal | None] = ContextVar("drawbridge_principal", default=None)

_JWKS_CLIENTS: dict[str, jwt.PyJWKClient] = {}


def current_principal() -> Principal | None:
    """The caller on this request, or None outside one.

    A context variable rather than `request.state` because the code that needs it is
    several layers down — `sync_session`, the packager, the agent worker — and threading a
    `Request` through them would put a web framework in the signature of the claim state
    machine.
    """
    return _principal.get()


def set_principal(principal: Principal | None) -> None:
    """For the middleware, and for tests that exercise a route without a transport."""
    _principal.set(principal)


def _jwks_client(url: str) -> jwt.PyJWKClient:
    """Cached JWKS client. PyJWT caches the keys; this caches the client.

    Rebuilt per call it would re-fetch Authentik's key set on every request, which is a
    network round trip inside the auth path of every request.
    """
    client = _JWKS_CLIENTS.get(url)
    if client is None:
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=600)
        _JWKS_CLIENTS[url] = client
    return client


def _signing_key(token: str, settings: Settings) -> tuple[Any, list[str]]:
    """The key to verify with, and the algorithms it is allowed to have signed under.

    The algorithm list is fixed per key source rather than read from the token's header.
    Accepting the header's word for it is how a public key gets used as an HMAC secret.
    """
    if settings.oidc_jwks_url:
        try:
            key = _jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as exc:
            raise AuthError(f"no verification key: {exc}") from exc
        return key, ["RS256"]
    if settings.jwt_secret:
        return settings.jwt_secret, ["HS256"]
    # Startup refuses this combination (see `check_auth_configuration`); reaching it means
    # the settings were mutated after boot.
    raise AuthError("no verification key is configured")


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    """Scopes from either spelling.

    OAuth 2 puts them in a space-delimited `scope` string; Authentik property mappings are
    usually written to emit a `scopes` list. Accepting both is cheaper than constraining
    every issuer.
    """
    raw = claims.get("scope") or claims.get("scopes") or []
    if isinstance(raw, str):
        return frozenset(raw.split())
    return frozenset(str(item) for item in raw)


def _roles(claims: dict[str, Any]) -> frozenset[str]:
    """Roles from a list or a space-delimited string, keeping only the ones that exist.

    Unknown roles are dropped rather than refused. An identity provider shared with other
    applications will emit roles meaning nothing here, and refusing the token over them
    would make this API's availability depend on someone else's naming.
    """
    raw = claims.get(ROLES_CLAIM) or []
    values = raw.split() if isinstance(raw, str) else [str(item) for item in raw]
    return frozenset(value for value in values if value in _ROLE_VALUES)


def _check_lifetime(claims: dict[str, Any], ceiling: int) -> None:
    """Refuse a token that was issued to live longer than its principal may hold one.

    `exp` alone says when a token stops working. It does not say how long it was meant to
    work, and a token minted for a year and presented on day one passes every check that
    looks only at `exp`. The ceiling is the difference between "not expired" and
    "short-lived", and it is checked against what the issuer itself wrote.
    """
    issued = int(claims["iat"])
    expires = int(claims["exp"])
    if issued > int(time.time()) + LEEWAY_SECONDS:
        raise AuthError("token issued in the future")
    if expires - issued > ceiling + LEEWAY_SECONDS:
        log.warning("auth.lifetime_exceeded", lifetime=expires - issued, ceiling=ceiling)
        raise AuthError("token lifetime exceeds the permitted maximum")


def _default_roles(settings: Settings) -> frozenset[str]:
    role = (
        settings.default_user_role_development
        if settings.is_development
        else settings.default_user_role
    )
    return frozenset({role}) if role in _ROLE_VALUES else frozenset()


def decode(token: str, settings: Settings) -> Principal:
    """Verify a token and read the caller out of it."""
    key, algorithms = _signing_key(token, settings)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        # The reason is logged and not returned. PyJWT distinguishes a bad signature from
        # a wrong audience, and telling an attacker which one they got wrong turns one
        # guess into two.
        log.info("auth.rejected", reason=str(exc))
        raise AuthError("invalid token") from exc

    subject = str(claims["sub"])
    scopes = _scopes(claims)
    raw_tenant = claims.get(settings.jwt_tenant_claim)
    token_id = str(claims["jti"]) if claims.get("jti") else None

    if SERVICE_SCOPE in scopes or is_agent_subject(subject):
        return _machine(subject, scopes, raw_tenant, token_id, claims, settings)

    _check_lifetime(claims, settings.max_user_token_ttl_seconds)
    if not raw_tenant:
        raise AuthError(f"token carries no {settings.jwt_tenant_claim} claim")
    try:
        tenant_id = UUID(str(raw_tenant))
    except ValueError as exc:
        raise AuthError("tenant claim is not a uuid") from exc

    roles = _roles(claims) or _default_roles(settings)
    permissions = frozenset(s.value for s in scopes_for_roles({Role(r) for r in roles}))
    # A human token may narrow what its roles allow — a delegated token minted for one
    # task — and never widen it. Scopes this API does not define (`openid`, `profile`)
    # say nothing about it and are ignored.
    requested = scopes & _SCOPE_VALUES
    if requested:
        permissions &= requested

    return Principal(
        subject=subject,
        tenant_id=tenant_id,
        scopes=scopes,
        email=claims.get("email"),
        roles=roles,
        token_id=token_id,
        permissions=permissions,
    )


def _machine(
    subject: str,
    scopes: frozenset[str],
    raw_tenant: Any,
    token_id: str | None,
    claims: dict[str, Any],
    settings: Settings,
) -> Principal:
    """A machine principal: a registered identity, its ceiling, and nothing it did not
    register for."""
    identity = agent(subject)
    if identity is None:
        # Logged with the subject, returned without it: the caller knows what it sent.
        log.warning("auth.unregistered_agent", subject=subject)
        raise AuthError("a machine token must name a registered agent")
    if identity.kind is not AgentKind.API_CLIENT:
        # A tool server, a database worker and a privileged job never present a bearer
        # token. One arriving under their name is somebody else using it.
        log.warning("auth.agent_kind_refused", subject=subject, kind=identity.kind.value)
        raise AuthError("this identity does not authenticate with a bearer token")
    if settings.environment not in identity.environments:
        log.warning("auth.agent_environment_refused", subject=subject)
        raise AuthError("this identity may not authenticate in this environment")

    _check_lifetime(claims, identity.max_token_ttl_seconds)

    tenant_id: UUID | None
    if identity.cross_tenant:
        if SERVICE_SCOPE not in scopes:
            raise AuthError("a cross-tenant agent token must carry the service scope")
        if raw_tenant:
            raise AuthError("a service token must not carry a tenant")
        tenant_id = None
    else:
        if SERVICE_SCOPE in scopes:
            raise AuthError("a tenant-bound agent may not carry the service scope")
        try:
            tenant_id = UUID(str(raw_tenant))
        except ValueError as exc:
            raise AuthError("tenant claim is not a uuid") from exc

    registered = frozenset(s.value for s in identity.scopes)
    requested = scopes & _SCOPE_VALUES
    permissions = registered & requested if requested else registered

    return Principal(
        subject=subject,
        tenant_id=tenant_id,
        scopes=scopes,
        agent_id=identity.agent_id,
        token_id=token_id,
        permissions=permissions,
    )


def _unauthorized(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "unauthenticated", "message": reason},
        headers={"WWW-Authenticate": "Bearer"},
    )


class AuthMiddleware:
    """Verify the bearer token, or refuse the request.

    Pure ASGI rather than `BaseHTTPMiddleware` since v1.1.0. The base class runs every
    request through a task group and a pair of memory streams, which costs a measurable
    slice of every call and is known to interact badly with context variables — the
    mechanism this middleware exists to set. The behaviour is unchanged; the principal is
    also left on `scope["state"]` so the access log, which sits outside this layer, can
    name who a request was without re-verifying the token.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._settings = settings

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in PUBLIC_PATHS or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        header = ""
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                header = value.decode("latin-1")
                break
        scheme, _, token = header.partition(" ")

        if not token or scheme.lower() != "bearer":
            if not self._settings.auth_required:
                # The escape hatch, and it is loud at startup. See
                # `check_auth_configuration`.
                set_principal(None)
                await self.app(scope, receive, send)
                return
            await _unauthorized("missing bearer token")(scope, receive, send)
            return

        # A bearer token is a few hundred bytes; one of 64 KB is not a token but an
        # attempt to make the verifier parse something large before it refuses.
        if len(token) > MAX_TOKEN_CHARS:
            await _unauthorized("invalid token")(scope, receive, send)
            return

        try:
            principal = decode(token, self._settings)
        except AuthError as exc:
            await _unauthorized(exc.reason)(scope, receive, send)
            return

        scope.setdefault("state", {})["principal"] = principal
        reset = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(reset)


def authorise_tenant(claimed: UUID | None) -> UUID:
    """The tenant this request may act for, given the one it asked for.

    Called by every route that takes a `tenant_id` in its body or query string. The
    returned value is what reaches `set_tenant`, so a mismatch here is the difference
    between the policies checking the token and the policies checking the request body.
    """
    principal = current_principal()

    if principal is None:
        # Only reachable with `auth_required` off, where the body is all there is.
        if claimed is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthenticated", "message": "no principal and no tenant_id"},
            )
        return claimed

    if principal.is_service:
        if claimed is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "tenant_required",
                    "message": "a service token must name the tenant it is acting for",
                },
            )
        return claimed

    if principal.tenant_id is None:  # pragma: no cover - `decode` forbids this shape
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no tenant")

    if claimed is not None and claimed != principal.tenant_id:
        log.warning(
            "auth.tenant_mismatch",
            subject=principal.subject,
            token_tenant=str(principal.tenant_id),
            body_tenant=str(claimed),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "wrong_tenant", "message": "token is not scoped to that tenant"},
        )
    return principal.tenant_id


def expected_tenant() -> UUID | None:
    """The tenant an identifier-addressed route must find, or None for no constraint.

    `GET /claims/{id}` and its siblings resolve the owner from the identifier because they
    have nothing else to go on (week 11, `tenancy.scope_to_claim`). That resolution is what
    made a claim id sufficient to read a claim. This is the constraint that closes it: the
    owner the lookup returns must be the token's tenant, or the row is reported absent.

    None for a service principal, which runs the pipeline across tenants and is addressed
    by claim id at four points in one workflow.
    """
    principal = current_principal()
    if principal is None or principal.is_service:
        return None
    return principal.tenant_id


def require(scope: Scope) -> Callable[[], Awaitable[None]]:
    """A route dependency: the caller must hold `scope`.

    403 rather than 401, and the scope is named in the body. A caller refused here has
    authenticated correctly and asked for something its role does not include; telling
    it which permission was missing costs nothing an attacker could not read in the
    OpenAPI schema, and saves an operator an afternoon.

    No principal means authentication is switched off (development) — the middleware
    has already refused every request that needed one.
    """

    async def _dependency() -> None:
        principal = current_principal()
        if principal is None or principal.can(scope):
            return
        log.warning(
            "auth.forbidden",
            subject=principal.subject,
            agent_id=principal.agent_id,
            scope=scope.value,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "message": f"requires scope {scope.value}"},
        )

    return _dependency


def actor_name(declared: str | None = None) -> str:
    """Who to record as having acted.

    The verified subject whenever there is one. A body field saying `actor: "analyst"`
    was, until v1.1.0, what `claim_transitions` recorded — n8n's "Halt Claim" node wrote
    `analyst` on every rejection it forwarded, and the trail said a person had done what
    the pipeline did. `declared` survives only for in-process callers with no principal:
    CLI scripts and tests, which hold a database session and could write anything anyway.
    """
    principal = current_principal()
    if principal is not None:
        return principal.subject
    return declared or "unauthenticated"


def check_identity_posture(settings: Settings) -> None:
    """Refuse to start outside development while an agent has nobody answering for it.

    The registry names an owner *role* for every agent; the deployment binds the role to
    a person. An unbound role is a registry entry that reads as governance and routes an
    incident to no one — the present-and-inert shape again.
    """
    unbound = [
        name
        for name, value in (
            ("owner_platform", settings.owner_platform),
            ("owner_compliance", settings.owner_compliance),
            ("owner_security", settings.owner_security),
        )
        if not value.strip()
    ]
    if not unbound:
        return
    if settings.is_development:
        log.warning("identity.owners_unbound", fields=unbound)
        return
    msg = (
        f"refusing to start in environment={settings.environment!r}: agent owner roles "
        f"are unbound ({', '.join('DRAWBRIDGE_' + f.upper() for f in unbound)}). Every "
        "registered agent must have a named person accountable for it."
    )
    raise RuntimeError(msg)


def check_auth_configuration(settings: Settings) -> None:
    """Refuse to start in a state where the control is installed and does nothing.

    The same posture as `scripts/rls_bootstrap.py`: auth required with no key would reject
    everybody, and auth switched off looks identical to a working deployment from the
    outside — every request succeeds.
    """
    if not settings.auth_required:
        log.warning(
            "auth.disabled",
            detail=(
                "DRAWBRIDGE_AUTH_REQUIRED is false: any caller may name any tenant. "
                "Development only."
            ),
        )
        return
    if not settings.jwt_secret and not settings.oidc_jwks_url:
        msg = (
            "authentication is required but no key is configured: set "
            "DRAWBRIDGE_OIDC_JWKS_URL (Authentik) or DRAWBRIDGE_JWT_SECRET (local)"
        )
        raise RuntimeError(msg)


def mint(
    settings: Settings,
    *,
    subject: str,
    tenant_id: UUID | None = None,
    scopes: tuple[str, ...] = (),
    ttl_seconds: int = 900,
    roles: tuple[str, ...] = (),
    token_id: str | None = None,
) -> str:
    """Issue an HS256 token against the local secret.

    Local only, and it refuses without `jwt_secret` rather than falling back to something.
    In a deployment Authentik issues tokens and this function has no key to sign with —
    which is the correct failure, because a second issuer nobody registered is a second
    way in.
    """
    if not settings.jwt_secret:
        msg = "DRAWBRIDGE_JWT_SECRET is not set; only Authentik can issue tokens here"
        raise RuntimeError(msg)

    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": token_id or str(uuid4()),
    }
    if roles:
        claims[ROLES_CLAIM] = list(roles)
    if scopes:
        claims["scope"] = " ".join(scopes)
    if tenant_id is not None:
        claims[settings.jwt_tenant_claim] = str(tenant_id)
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")

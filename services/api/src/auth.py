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
service token is therefore a cross-tenant credential and the most valuable secret in the
deployment. It is minted separately (`scripts/mint_token.py --service`), and
`tests/integration/test_tenant_isolation.py` proves the *user* path, which is the one that
faces a human.

**Verification.** RS256 against Authentik's JWKS in a real deployment; HS256 against a
shared secret for local work, where standing up an identity provider to run the test suite
would be its own kind of dishonesty. Both paths check `iss`, `aud` and `exp`; the
difference is where the key comes from.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

import jwt
import structlog
from fastapi import HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

    from services.api.src.config import Settings

log = structlog.get_logger()

SERVICE_SCOPE = "drawbridge:service"

# Reachable without a token. Health and readiness are polled by Docker before anything
# could hold a credential; the schema endpoints describe the API rather than any tenant's
# data.
PUBLIC_PATHS = frozenset({"/health", "/ready", "/docs", "/redoc", "/openapi.json"})


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

    @property
    def is_service(self) -> bool:
        return SERVICE_SCOPE in self.scopes


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
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        # The reason is logged and not returned. PyJWT distinguishes a bad signature from
        # a wrong audience, and telling an attacker which one they got wrong turns one
        # guess into two.
        log.info("auth.rejected", reason=str(exc))
        raise AuthError("invalid token") from exc

    scopes = _scopes(claims)
    raw_tenant = claims.get(settings.jwt_tenant_claim)

    if SERVICE_SCOPE in scopes:
        if raw_tenant:
            raise AuthError("a service token must not carry a tenant")
        return Principal(subject=str(claims["sub"]), tenant_id=None, scopes=scopes)

    if not raw_tenant:
        raise AuthError(f"token carries no {settings.jwt_tenant_claim} claim")
    try:
        tenant_id = UUID(str(raw_tenant))
    except ValueError as exc:
        raise AuthError("tenant claim is not a uuid") from exc

    return Principal(
        subject=str(claims["sub"]),
        tenant_id=tenant_id,
        scopes=scopes,
        email=claims.get("email"),
    )


def _unauthorized(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "unauthenticated", "message": reason},
        headers={"WWW-Authenticate": "Bearer"},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify the bearer token, or refuse the request."""

    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")

        if not token or scheme.lower() != "bearer":
            if not self._settings.auth_required:
                # The escape hatch, and it is loud at startup. See
                # `check_auth_configuration`.
                set_principal(None)
                return await call_next(request)
            return _unauthorized("missing bearer token")

        try:
            principal = decode(token, self._settings)
        except AuthError as exc:
            return _unauthorized(exc.reason)

        reset = _principal.set(principal)
        try:
            return await call_next(request)
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
    ttl_seconds: int = 3600,
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
    }
    if scopes:
        claims["scope"] = " ".join(scopes)
    if tenant_id is not None:
        claims[settings.jwt_tenant_claim] = str(tenant_id)
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")

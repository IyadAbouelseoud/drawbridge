"""MCP authentication and per-tool authorisation — the tool boundary gets a door.

Until v1.1.0 all five MCP servers listened on `0.0.0.0` over streamable HTTP with no
authentication at all. In development their ports were published on every interface of the
host; on-prem they sat on the internal network, reachable by anything else on it. Any
caller that could open a TCP connection could resolve an exception, override a valuation,
approve a claim or fetch a document, for any tenant — and the `analyst` argument recorded
against the decision was whatever string the caller typed.

This module is the same identity model the API uses, applied at the second door:

- **Transport.** Each server is built with the SDK's `token_verifier` and `AuthSettings`, so
  the SDK's own `RequireAuthMiddleware` refuses a request without a valid bearer token
  before any tool runs, and advertises RFC 9728 protected-resource metadata. Verification
  is `services.api.src.auth.decode` — one verifier for both surfaces, so a token the API
  refuses the MCP servers refuse, for the same reason. DNS-rebinding protection is on
  with an explicit host list, because a server bound to `0.0.0.0` does not get it by
  default.
- **Session scope.** Each server names the scope a caller must hold to open a session at
  all (`AgentIdentity.scopes` for its registry entry).
- **Per tool.** `guarded` checks the tool's own scope, puts the verified principal on the
  same context variable the API uses — so `gates`, `expected_tenant` and the ledger all see
  the real caller — enforces the kill switch on write tools, and writes one access-log line
  per call.

**In-process calls are not HTTP calls.** A test or a CLI that imports a tool function and
calls it has no transport and no token; it runs as a local caller, exactly as it would
calling `analyst.py` directly. Anything arriving over the network has passed the SDK's
middleware first, so "no token here" can only mean "not a network call".
"""

from __future__ import annotations

import functools
import time
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

import structlog
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from drawbridge_schemas.agents import Scope, agent
from services.api.src import killswitch
from services.api.src.auth import (
    SERVICE_SCOPE,
    AuthError,
    Principal,
    current_principal,
    decode,
    set_principal,
)
from services.api.src.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger("drawbridge.mcp.access")

F = TypeVar("F", bound="Callable[..., Any]")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
#: Write tools are marked destructive so an MCP client — an analyst's Claude Code session —
#: asks its human before calling one. That prompt is the human-in-the-loop on the client
#: side; `gates.py` is the one on ours.
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)


class DrawbridgeTokenVerifier:
    """The SDK's `TokenVerifier`, backed by the API's own `decode`."""

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = get_settings()
        try:
            principal = decode(token, settings)
        except AuthError as exc:
            log.info("mcp.auth_rejected", reason=exc.reason)
            return None
        return AccessToken(
            token=token,
            client_id=principal.subject,
            scopes=sorted(principal.effective_permissions),
            subject=principal.subject,
            claims={
                "tenant_id": str(principal.tenant_id) if principal.tenant_id else None,
                "agent_id": principal.agent_id,
                "roles": sorted(principal.roles),
                "service": principal.is_service,
                "jti": principal.token_id,
            },
        )


def principal_from(access: AccessToken) -> Principal:
    claims = access.claims or {}
    tenant = claims.get("tenant_id")
    scopes = {SERVICE_SCOPE} if claims.get("service") else set()
    return Principal(
        subject=access.subject or access.client_id,
        tenant_id=UUID(str(tenant)) if tenant else None,
        scopes=frozenset(scopes),
        roles=frozenset(claims.get("roles") or ()),
        agent_id=claims.get("agent_id"),
        token_id=claims.get("jti"),
        permissions=frozenset(access.scopes),
    )


def server_kwargs(agent_id: str, url: str) -> dict[str, Any]:
    """Constructor arguments that make an `MCPServer` require authentication.

    Empty when authentication is switched off (development only — the API logs that loudly
    at startup and so do these servers in `run`). With it on, a server cannot start without
    a verification key, for the same reason the API cannot: a verifier with nothing to
    verify against refuses everyone or, worse, is replaced by someone "temporarily".
    """
    settings = get_settings()
    if not settings.auth_required:
        return {}
    identity = agent(agent_id)
    if identity is None:  # pragma: no cover - a coding error, caught by the registry tests
        msg = f"{agent_id} is not a registered identity"
        raise RuntimeError(msg)
    issuer = settings.jwt_issuer if settings.jwt_issuer.startswith("http") else "http://localhost"
    return {
        "token_verifier": DrawbridgeTokenVerifier(),
        "auth": AuthSettings(
            issuer_url=issuer,
            resource_server_url=url,
            required_scopes=sorted(s.value for s in identity.scopes) or None,
        ),
    }


def transport_security(service_host: str, port: int) -> TransportSecuritySettings:
    """DNS-rebinding protection with the hosts this server is actually addressed by."""
    hosts = [f"{service_host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts],
    )


def check_startup(name: str) -> None:
    """Refuse to serve with authentication required and no key; warn when it is off."""
    settings = get_settings()
    if not settings.auth_required:
        log.warning("mcp.auth_disabled", server=name, detail="development only")
        return
    if not settings.jwt_secret and not settings.oidc_jwks_url:
        msg = f"{name}: authentication is required but no verification key is configured"
        raise RuntimeError(msg)


def _halt_check(principal: Principal | None) -> None:
    from sqlalchemy.orm import Session

    from mcp_servers.mcp_claims.db import engine

    with Session(engine()) as session:
        covering = killswitch.state(session).covering(
            principal=principal.subject if principal else None
        )
    if covering is not None:
        raise killswitch.KillSwitchEngagedError(covering)


def guarded(scope: Scope | None, *, write: bool = False) -> Callable[[F], F]:
    """Authorise, scope, halt-check and log one tool call.

    Refusals come back as data (`{"ok": False, ...}`), the convention every tool here
    follows: an analyst in Claude Code should read why, not a transport error.
    """

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            access = get_access_token()
            principal = principal_from(access) if access is not None else current_principal()
            outcome = "ok"
            try:
                if principal is not None and scope is not None and not principal.can(scope):
                    outcome = "forbidden"
                    return {
                        "ok": False,
                        "error": "forbidden",
                        "detail": f"this tool requires scope {scope.value}",
                    }
                if write:
                    try:
                        _halt_check(principal)
                    except killswitch.KillSwitchEngagedError as exc:
                        outcome = "halted"
                        return {"ok": False, "error": "kill_switch_engaged", "detail": str(exc)}
                previous = current_principal()
                set_principal(principal)
                mutating = killswitch.MUTATING.set(write)
                acting = killswitch.PRINCIPAL.set(principal.subject if principal else None)
                try:
                    result = fn(*args, **kwargs)
                finally:
                    killswitch.PRINCIPAL.reset(acting)
                    killswitch.MUTATING.reset(mutating)
                    set_principal(previous)
                if isinstance(result, dict) and result.get("ok") is False:
                    outcome = "refused"
                return result
            except Exception:
                outcome = "error"
                raise
            finally:
                log.info(
                    "mcp.tool",
                    tool=fn.__name__,
                    write=write,
                    outcome=outcome,
                    principal=principal.subject if principal else "local",
                    agent_id=principal.agent_id if principal else None,
                    tenant=str(principal.tenant_id) if principal and principal.tenant_id else None,
                    ids={k: str(v) for k, v in kwargs.items() if k.endswith("_id")},
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )

        return wrapper  # type: ignore[return-value]

    return decorate


def run(server: Any, *, name: str, host_alias: str, port: int) -> None:
    """Start one server: startup posture, then streamable HTTP with rebinding protection."""
    check_startup(name)
    server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        transport_security=transport_security(host_alias, port),
    )

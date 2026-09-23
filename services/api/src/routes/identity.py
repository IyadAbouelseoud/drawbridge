"""The credential exchange, and the question every caller should be able to ask.

`POST /auth/token` is OAuth 2.0 client credentials (RFC 6749 §4.4) for the registered
API-client agents: the pipeline presents its client secret and gets back an access token
that lives fifteen minutes. This is what replaced the 24-hour service token.

**Why a long-lived secret is still acceptable here, when a long-lived token was not.** The
client secret authenticates at exactly one endpoint, is rate limited there, and every use
of it is recorded in `control_events` with the token it produced. The access token is what
reaches data, and it expires before a leaked copy of it is worth much. A leaked *secret*
is revoked by rotating one value or by throwing the kill switch on the agent's principal,
which stops the exchange as well as every token already issued under it — the switch is
checked on every mutation, not only at issuance.

**Local issuer only.** Under Authentik (`oidc_jwks_url` set) the API holds no signing key
and this endpoint refuses: a second issuer nobody registered is a second way in (§17). The
agents then use Authentik's own client-credentials grant, and the verifier applies the same
lifetime ceiling to what Authentik issues.

**Failures are uniform.** An unknown client, a wrong secret and an agent that does not
authenticate this way all return the same 401 `invalid_client`, because distinguishing them
tells a guesser which half of the pair was right.
"""

from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from drawbridge_schemas.agents import AgentKind, Scope, agent
from services.api.src import killswitch
from services.api.src.auth import SERVICE_SCOPE, current_principal, mint, require
from services.api.src.config import Settings, get_settings
from services.api.src.sync_db import in_thread

log = structlog.get_logger()

router = APIRouter(prefix="/auth", tags=["identity"])


def _client_secrets(settings: Settings) -> dict[str, str | None]:
    """Registered API-client agents and the secret each exchanges.

    A table rather than a lookup by naming convention so that registering an agent does
    not by itself create a way to mint tokens for it: the secret has to be provisioned too.
    """
    return {
        "agent:n8n-pipeline": settings.pipeline_client_secret,
        "agent:e2e-harness": settings.e2e_client_secret,
    }


_INVALID = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"error": "invalid_client"},
    headers={"WWW-Authenticate": "Basic"},
)


async def _form(request: Request) -> dict[str, str]:
    """The request body, form-encoded per the RFC or JSON for convenience.

    Parsed by hand rather than through FastAPI's `Form`, which would make a four-field
    body depend on `python-multipart` being installed.
    """
    raw = await request.body()
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items() if v}


@router.post("/token")
async def token(request: Request) -> dict[str, Any]:
    """Exchange an agent's client credentials for a short-lived access token."""
    settings = get_settings()
    if settings.oidc_jwks_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unsupported",
                "message": "tokens are issued by the identity provider in this deployment",
            },
        )

    body = await _form(request)
    if body.get("grant_type") != "client_credentials":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unsupported_grant_type"},
        )

    client_id = body.get("client_id", "")
    presented = body.get("client_secret", "")
    identity = agent(client_id)
    expected = _client_secrets(settings).get(client_id)
    if (
        identity is None
        or identity.kind is not AgentKind.API_CLIENT
        or settings.environment not in identity.environments
        or not expected
        or not presented
        or not hmac.compare_digest(presented.encode(), expected.encode())
    ):
        log.warning("auth.token_refused", client_id=client_id[:80])
        raise _INVALID

    registered = {s.value for s in identity.scopes}
    asked = set(body.get("scope", "").split()) & registered
    granted = sorted(asked or registered)
    token_id = str(uuid4())
    ttl = identity.max_token_ttl_seconds

    def _record(session: Any) -> None:
        # Refused if the agent, or everything, is halted — and recorded before the token
        # is returned, so an unlogged credential is never issued.
        killswitch.assert_running(session, principal=identity.agent_id)
        killswitch.record_event(
            session,
            event_type="token_issued",
            actor=identity.agent_id,
            actor_kind="machine",
            reason=f"client_credentials exchange for {identity.agent_id}",
            scope_kind="principal",
            scope_value=identity.agent_id,
            detail={"jti": token_id, "expires_in": ttl, "scopes": granted},
        )

    try:
        await in_thread(_record)
    except killswitch.KillSwitchEngagedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "kill_switch_engaged", "message": str(exc)},
        ) from exc

    scopes = (SERVICE_SCOPE, *granted) if identity.cross_tenant else tuple(granted)
    access = mint(
        settings,
        subject=identity.agent_id,
        scopes=scopes,
        ttl_seconds=ttl,
        token_id=token_id,
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ttl,
        "scope": " ".join(granted),
    }


@router.get("/whoami", dependencies=[Depends(require(Scope.CLAIMS_READ))])
async def whoami() -> dict[str, Any]:
    """What the API believes about the caller: identity, owner, roles, permissions.

    Behind `claims:read` rather than open, because the answer includes the caller's
    tenant. An operator (who holds no read scope on tenant data) checks their own access
    through `GET /control/kill-switch` instead.
    """
    principal = current_principal()
    if principal is None:
        return {"authenticated": False}
    identity = agent(principal.agent_id) if principal.agent_id else None
    return {
        "authenticated": True,
        "subject": principal.subject,
        "kind": "machine" if principal.is_machine else "human",
        "agent_id": principal.agent_id,
        "owner": identity.owner.value if identity else None,
        "tenant_id": str(principal.tenant_id) if principal.tenant_id else None,
        "roles": sorted(principal.roles),
        "permissions": sorted(principal.effective_permissions),
        "token_id": principal.token_id,
    }

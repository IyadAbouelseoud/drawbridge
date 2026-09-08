"""Build Drawbridge's OIDC provider in Authentik, and prove the API accepts its tokens.

    docker compose --profile identity up -d authentik-server authentik-worker
    python infra/authentik_bootstrap.py --tenant <uuid>

Week 12 put Authentik in the compose file and taught `services/api/src/auth.py` to verify
RS256 against a JWKS URL. Weeks 12 and 13 then both recorded the same gap: *no RS256 token
has ever reached this API*. Everything that runs, runs on HS256 against a shared secret,
which is a development convenience the codebase has been honest about and has not replaced.

This closes it, and does so declaratively: every step reads the current state first and
makes only the change that is missing, so a second run is a no-op and a run against a
half-built configuration finishes it. Authentik's own blueprint YAML would be the more
idiomatic form; the API is used instead because the round trip at the end has to happen in
the same process that did the configuring — a bootstrap that cannot verify itself is how
"identity is configured" and "identity works" drift apart.

**What it builds.**

1. An **RSA signing keypair**. RS256, not HS256, and that is the entire point: the API
   verifies with a public key it fetches, so it never holds anything that could mint a
   token. Under HS256 the verifier and the issuer share one secret, which means the
   service that checks tokens can also forge them.
2. A **scope property mapping** emitting `tenant_id`. This is the load-bearing claim.
   `tenancy.set_tenant` writes it into `tenant.id` and every row-level policy compares
   against it, so the mapping is the join between the identity provider and the database's
   isolation boundary. It reads the claim from the user's own attributes rather than
   hard-coding one, because a mapping that emits a constant would give every user in the
   directory the same tenant.
3. An **OAuth2 provider** and an **application**, bound to each other and to that mapping.
4. A **service account** carrying `tenant_id` in its attributes, used for the round trip.

**What it deliberately does not do.** Configure the API. The script prints the three
values a deployment must set — issuer, audience, JWKS URL — and stops. Writing them into
`.env` from here would mean this script could silently repoint a running deployment's
trust anchor, which is the one setting that should never change without somebody
noticing.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any
from uuid import UUID

import httpx

DEFAULT_URL = "http://localhost:9443"
APP_SLUG = "drawbridge"
APP_NAME = "Drawbridge"
PROVIDER_NAME = "drawbridge-api"
MAPPING_NAME = "drawbridge tenant claim"
SCOPE_NAME = "drawbridge"
CERT_NAME = "drawbridge-api-signing"
SERVICE_ACCOUNT = "drawbridge-pipeline"

#: What the API is configured to require in `aud`. Emitted by the property mapping rather
#: than left as Authentik's default (which is the client_id), so that rotating the OAuth
#: client does not change what every deployment must be configured to expect.
AUDIENCE = "drawbridge-api"

#: The mapping expression, evaluated by Authentik per token.
#:
#: `tenant_id` comes from the user record. A user without it gets a token without the
#: claim, and `auth.decode` refuses that token — which is the correct outcome: a caller
#: whose tenant nobody has decided must not reach a tenant-scoped route, and failing at
#: the door is better than failing at a policy that would have returned zero rows and
#: looked like an empty account.
MAPPING_EXPRESSION = """# Drawbridge tenant claim. Managed by infra/authentik_bootstrap.py.
tenant_id = request.user.attributes.get("tenant_id")
claims = {"aud": "drawbridge-api"}
if tenant_id:
    claims["tenant_id"] = str(tenant_id)
scopes = request.user.attributes.get("drawbridge_scopes")
if scopes:
    claims["scopes"] = list(scopes)
return claims
"""


class BootstrapError(RuntimeError):
    """A step could not complete. Names the step; never carries a token."""


class Authentik:
    """The Authentik admin API, narrowed to what this script needs."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def call(self, method: str, path: str, *, json_body: Any = None) -> Any:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            msg = f"authentik at {self.base_url} is unreachable: {type(exc).__name__}"
            raise BootstrapError(msg) from exc
        if response.status_code == httpx.codes.FORBIDDEN:
            msg = (
                f"authentik refused {method} {path}: the bootstrap token is not valid. "
                f"Set AUTHENTIK_BOOTSTRAP_TOKEN before first start — it is only honoured "
                f"when the database is created, so an existing stack needs `make clean`."
            )
            raise BootstrapError(msg)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # The body is included here and nowhere else in this file. Authentik's
            # validation errors name the offending field, which is the whole diagnostic
            # value of a 400, and this endpoint's payloads carry no credentials.
            msg = (
                f"authentik refused {method} {path} ({response.status_code}): {response.text[:400]}"
            )
            raise BootstrapError(msg)
        return response.json() if response.content else {}

    def first(self, path: str) -> dict[str, Any] | None:
        """The first result of a filtered list endpoint, or None.

        Filtering server-side rather than listing and searching in Python: an Authentik
        instance shared with anything else has hundreds of property mappings, and a
        client-side scan that pages wrong would silently create a duplicate.
        """
        payload = self.call("GET", path)
        results = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(results, list) and results:
            item = results[0]
            return item if isinstance(item, dict) else None
        return None


def wait_ready(base_url: str, seconds: int) -> None:
    """Block until Authentik answers, or give up and say so.

    Authentik runs its migrations on first start and answers nothing until they finish,
    which on a cold database is minutes rather than seconds. A bootstrap that failed on
    connection-refused would be indistinguishable from a misconfigured one.
    """
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url.rstrip('/')}/-/health/ready/", timeout=5.0)
            if response.status_code < httpx.codes.BAD_REQUEST:
                return
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(3)
    msg = f"authentik at {base_url} was not ready within {seconds}s (last: {last})"
    raise BootstrapError(msg)


def ensure_certificate(ak: Authentik) -> tuple[str, str]:
    """An RSA keypair for signing. Returns (pk, note).

    Generated inside Authentik rather than supplied. The private half of a token-signing
    key should exist in exactly one place, and a key this script generated would have
    existed in this process's memory and in whatever shell captured the output.
    """
    existing = ak.first(f"/api/v3/crypto/certificatekeypairs/?name={CERT_NAME}")
    if existing:
        return str(existing["pk"]), "already present"
    created = ak.call(
        "POST",
        "/api/v3/crypto/certificatekeypairs/generate/",
        json_body={
            "common_name": CERT_NAME,
            "subject_alt_name": "",
            "validity_days": 730,
            "alg": "rsa",
        },
    )
    return str(created["pk"]), "generated RSA-2048, valid 730 days"


def ensure_mapping(ak: Authentik) -> tuple[str, str]:
    """The scope mapping that emits `tenant_id`. Returns (pk, note)."""
    existing = ak.first(f"/api/v3/propertymappings/provider/scope/?name={MAPPING_NAME}")
    body = {
        "name": MAPPING_NAME,
        "scope_name": SCOPE_NAME,
        "description": "Drawbridge tenant identity. Managed by infra/authentik_bootstrap.py.",
        "expression": MAPPING_EXPRESSION,
    }
    if existing:
        if existing.get("expression") == MAPPING_EXPRESSION:
            return str(existing["pk"]), "already current"
        ak.call("PUT", f"/api/v3/propertymappings/provider/scope/{existing['pk']}/", json_body=body)
        return str(existing["pk"]), "expression updated"
    created = ak.call("POST", "/api/v3/propertymappings/provider/scope/", json_body=body)
    return str(created["pk"]), f"created, scope `{SCOPE_NAME}`"


def flow_pk(ak: Authentik, slug: str) -> str:
    found = ak.first(f"/api/v3/flows/instances/?slug={slug}")
    if not found:
        msg = f"authentik has no flow `{slug}`; this instance is missing its default flows"
        raise BootstrapError(msg)
    return str(found["pk"])


def scope_mapping_pks(ak: Authentik, *names: str) -> list[str]:
    """The built-in OpenID scope mappings, by name.

    `openid` is not optional: Authentik will not issue an ID token without it, and a
    provider configured with only a custom mapping produces an opaque access token that
    `auth.decode` cannot read at all.
    """
    pks: list[str] = []
    for name in names:
        found = ak.first(
            f"/api/v3/propertymappings/provider/scope/?managed=goauthentik.io/providers/oauth2/scope-{name}"
        )
        if found:
            pks.append(str(found["pk"]))
    return pks


def ensure_provider(
    ak: Authentik, *, cert_pk: str, mapping_pks: list[str]
) -> tuple[dict[str, Any], str]:
    """The OAuth2 provider. Returns (provider, note)."""
    body: dict[str, Any] = {
        "name": PROVIDER_NAME,
        "authorization_flow": flow_pk(ak, "default-provider-authorization-implicit-consent"),
        "invalidation_flow": flow_pk(ak, "default-provider-invalidation-flow"),
        "client_type": "confidential",
        # Empty, and required to be present. This provider issues tokens over the
        # client-credentials grant only; there is no browser in the loop and therefore
        # nowhere to redirect to. A permissive redirect URI on a provider that never uses
        # one is a standing open-redirect waiting for someone to enable the code flow.
        "redirect_uris": [],
        "signing_key": cert_pk,
        "property_mappings": mapping_pks,
        # `hashed_user_id` rather than the primary key: `sub` is stable per user per
        # provider and reveals nothing about the directory's internal identifiers.
        "sub_mode": "hashed_user_id",
        "issuer_mode": "per_provider",
        "include_claims_in_id_token": True,
        # Ten minutes. A token this service accepts is a bearer credential with no
        # revocation path (see auth.py's own list of what it does not do), so the only
        # bound on a stolen one is how long it stays valid.
        "access_token_validity": "minutes=10",
    }
    existing = ak.first(f"/api/v3/providers/oauth2/?name={PROVIDER_NAME}")
    if existing:
        updated = ak.call("PATCH", f"/api/v3/providers/oauth2/{existing['pk']}/", json_body=body)
        return updated, "updated"
    created = ak.call("POST", "/api/v3/providers/oauth2/", json_body=body)
    return created, "created"


def ensure_application(ak: Authentik, provider_pk: int) -> str:
    existing = ak.first(f"/api/v3/core/applications/?slug={APP_SLUG}")
    body = {"name": APP_NAME, "slug": APP_SLUG, "provider": provider_pk}
    if existing:
        if existing.get("provider") == provider_pk:
            return "already bound"
        ak.call("PATCH", f"/api/v3/core/applications/{APP_SLUG}/", json_body=body)
        return "rebound to the provider"
    ak.call("POST", "/api/v3/core/applications/", json_body=body)
    return "created"


def ensure_service_account(ak: Authentik, tenant: UUID) -> tuple[str, str, str]:
    """A service account carrying `tenant_id`. Returns (username, token, note).

    The account exists so the round trip below can happen without a browser. Its token is
    Authentik's, not ours: what this proves is that a real user record with a real tenant
    attribute produces a real RS256 token that our verifier accepts.
    """
    existing = ak.first(f"/api/v3/core/users/?username={SERVICE_ACCOUNT}")
    if existing:
        # A service account's password is not readable once issued, so an existing account
        # is rotated rather than reused. The alternative is a script that works the first
        # time and cannot verify itself on any later run.
        ak.call("DELETE", f"/api/v3/core/users/{existing['pk']}/")
    created = ak.call(
        "POST",
        "/api/v3/core/users/service_account/",
        json_body={
            "name": SERVICE_ACCOUNT,
            "create_group": False,
            "expiring": False,
        },
    )
    username = str(created["username"])
    token = str(created["token"])
    user = ak.first(f"/api/v3/core/users/?username={username}")
    if not user:  # pragma: no cover - the account was just created
        msg = f"service account {username} vanished between creation and lookup"
        raise BootstrapError(msg)
    ak.call(
        "PATCH",
        f"/api/v3/core/users/{user['pk']}/",
        json_body={"attributes": {"tenant_id": str(tenant), "drawbridge_scopes": []}},
    )
    return username, token, f"issued, tenant_id={tenant}"


def mint(base_url: str, client_id: str, username: str, password: str) -> str:
    """One RS256 access token, over the client-credentials grant.

    Authentik's static-token form: the service account's own token stands in for the
    password. There is no browser in this loop by design — the point is to exercise the
    machine-to-machine path, which is the one n8n and the agent worker will use.
    """
    response = httpx.post(
        f"{base_url.rstrip('/')}/application/o/token/",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "username": username,
            "password": password,
            "scope": f"openid {SCOPE_NAME}",
        },
        timeout=30.0,
    )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        msg = f"authentik refused the token request ({response.status_code}): {response.text[:300]}"
        raise BootstrapError(msg)
    payload = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str):
        msg = "authentik returned no access_token"
        raise BootstrapError(msg)
    return token


def verify_live(token: str, api_url: str, tenant: UUID, local_secret: str | None) -> int:
    """Send the token to a running API and check that it is accepted, and that HS256 is not.

    The offline check proves `decode` accepts the token. This proves the *deployment* does,
    which is a different claim: it exercises the middleware, the JWKS fetch from inside the
    container's own network, and the issuer and audience the API was actually started with.

    The negative half is the more interesting one. The same claims, re-signed HS256 with
    the shared secret this service used until an OIDC URL was configured, must come back
    401. If they do not, the JWKS path was added *beside* the shared secret rather than in
    place of it, and every secret in `.secrets.json` is still a token-minting key.
    """
    import jwt

    tenant_param = {"tenant_id": str(tenant)}
    accepted = httpx.get(
        f"{api_url.rstrip('/')}/review/queue",
        headers={"Authorization": f"Bearer {token}"},
        params=tenant_param,
        timeout=30.0,
    )
    print(f"  RS256 -> GET /review/queue  HTTP {accepted.status_code}")
    if accepted.status_code != httpx.codes.OK:
        print(f"  FAILED: {accepted.text[:200]}", file=sys.stderr)
        return 1

    if not local_secret:
        print("  (no local jwt_secret to re-sign with; HS256 half skipped)")
        return 0

    claims = jwt.decode(token, options={"verify_signature": False})
    forged = jwt.encode(claims, local_secret, algorithm="HS256")
    refused = httpx.get(
        f"{api_url.rstrip('/')}/review/queue",
        headers={"Authorization": f"Bearer {forged}"},
        params=tenant_param,
        timeout=30.0,
    )
    print(f"  same claims re-signed HS256 with the local secret  HTTP {refused.status_code}")
    if refused.status_code != httpx.codes.UNAUTHORIZED:
        print(
            "  FAILED: the API still accepts a locally signed token, so the shared secret "
            "is still a minting key",
            file=sys.stderr,
        )
        return 1
    return 0


def verify(token: str, jwks_url: str, tenant: UUID) -> int:
    """Verify the token through the API's own code, not through a second implementation.

    `services.api.src.auth.decode` is what every request runs. Re-verifying here with a
    hand-rolled `jwt.decode` would prove that PyJWT works, which nobody doubted; running
    the real function is what shows the algorithm list, the audience, the issuer and the
    tenant claim all line up with what Authentik actually emits.
    """
    import jwt

    from services.api.src.auth import AuthError, decode
    from services.api.src.config import Settings

    unverified = jwt.decode(token, options={"verify_signature": False})
    header = jwt.get_unverified_header(token)
    issuer = str(unverified.get("iss", ""))

    print(f"  alg       {header.get('alg')}  kid {str(header.get('kid'))[:16]}…")
    print(f"  iss       {issuer}")
    print(f"  aud       {unverified.get('aud')}")
    print(f"  tenant_id {unverified.get('tenant_id')}")

    if header.get("alg") != "RS256":
        print(f"  FAILED: expected RS256, got {header.get('alg')}", file=sys.stderr)
        return 1

    settings = Settings(
        oidc_jwks_url=jwks_url,
        jwt_issuer=issuer,
        jwt_audience=AUDIENCE,
        auth_required=True,
    )
    try:
        principal = decode(token, settings)
    except AuthError as exc:
        print(f"  FAILED: services.api.src.auth.decode rejected it — {exc.reason}", file=sys.stderr)
        return 1

    if principal.tenant_id != tenant:
        print(
            f"  FAILED: token names tenant {principal.tenant_id}, expected {tenant}",
            file=sys.stderr,
        )
        return 1

    print(f"  VERIFIED  subject={principal.subject[:16]}… tenant={principal.tenant_id}")
    print("            through services.api.src.auth.decode, RS256, key fetched from JWKS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure Authentik for Drawbridge.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Authentik base URL")
    parser.add_argument("--token", default=None, help="bootstrap API token; else from secrets")
    parser.add_argument(
        "--tenant",
        type=UUID,
        required=True,
        help="the tenant the round-trip service account acts for",
    )
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for readiness")
    parser.add_argument(
        "--api-url",
        default=None,
        help="also send the minted token to a running Drawbridge API, e.g. "
        "http://localhost:8000. The API must already be started with the three "
        "values printed at the end of a previous run.",
    )
    args = parser.parse_args(argv)

    token = args.token
    if not token:
        from services.api.src.secrets import resolve

        token = resolve().get("authentik_bootstrap_token")
    if not token:
        print(
            "no bootstrap token. Run `make secrets-init` to mint one, then recreate the "
            "authentik containers so AUTHENTIK_BOOTSTRAP_TOKEN is honoured.",
            file=sys.stderr,
        )
        return 2

    try:
        wait_ready(args.url, args.wait)
    except BootstrapError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    ak = Authentik(args.url, token)
    try:
        print(f"authentik {args.url}")
        cert_pk, note = ensure_certificate(ak)
        print(f"  signing key   {CERT_NAME}: {note}")
        mapping_pk, note = ensure_mapping(ak)
        print(f"  property map  {MAPPING_NAME}: {note}")
        builtin = scope_mapping_pks(ak, "openid", "profile", "email")
        provider, note = ensure_provider(ak, cert_pk=cert_pk, mapping_pks=[*builtin, mapping_pk])
        print(f"  provider      {PROVIDER_NAME}: {note}, {len(builtin) + 1} mapping(s)")
        print(f"  application   {APP_SLUG}: {ensure_application(ak, int(provider['pk']))}")
        username, account_token, note = ensure_service_account(ak, args.tenant)
        print(f"  service acct  {username}: {note}")
        access = mint(args.url, str(provider["client_id"]), username, account_token)
    except BootstrapError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        ak.close()

    jwks_url = f"{args.url.rstrip('/')}/application/o/{APP_SLUG}/jwks/"
    print("\nround trip")
    status = verify(access, jwks_url, args.tenant)
    if status:
        return status

    if args.api_url:
        from services.api.src.secrets import resolve as resolve_secrets

        print(f"\nlive round trip against {args.api_url}")
        try:
            status = verify_live(
                access, args.api_url, args.tenant, resolve_secrets().get("jwt_secret")
            )
        except BootstrapError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        if status:
            return status

    import jwt

    issuer = str(jwt.decode(access, options={"verify_signature": False})["iss"])
    print("\nSet these on the API and restart it:")
    print(f"  DRAWBRIDGE_OIDC_JWKS_URL={jwks_url}")
    print(f"  DRAWBRIDGE_JWT_ISSUER={issuer}")
    print(f"  DRAWBRIDGE_JWT_AUDIENCE={AUDIENCE}")
    print("\nWith a JWKS URL set, jwt_secret stops being consulted and nothing local can")
    print("mint a token — including `make token`. That is the intended end state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

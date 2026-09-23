"""Where secrets come from, and what this does and does not buy.

Until now the JWT secret, the service token and the application role's database password
were plain entries in `.env`, which meant they were also environment variables in every
container. That is worse than it sounds, and the reasons are specific rather than
atmospheric:

- `docker inspect` prints them, to anyone in the docker group, with no audit trail.
- They are inherited by every child process. A `subprocess` call in the extraction path
  hands the JWT secret to whatever it spawns.
- They land in crash dumps and in the environment blocks that error reporters collect.
- On Linux they are readable from `/proc/<pid>/environ` for the process's uid.
- `docker compose config` renders them, and that output is what people paste into issues.

Reading them from a file at startup removes all five. It does **not** make them secret
from anyone who can read the file, and this module does not pretend otherwise: a
`.secrets.json` on a developer's laptop is exactly as exposed as that laptop. What the
file buys is one place to rotate and a narrower blast radius. What it does not buy is
anything a deployment can rely on, which is why it is no longer the only backend.

**Three backends, chosen by `DRAWBRIDGE_SECRETS_PROVIDER`.**

| Value | Provider | What holds the secret |
|---|---|---|
| `file` (default) | `FileSecretProvider` | a gitignored JSON file. Local work only |
| `vault` | `VaultSecretProvider` | HashiCorp Vault KV v2, over AppRole or a token |
| `aws` | `AwsSecretsManagerProvider` | one Secrets Manager secret holding a JSON object |

The two managers are implemented, not sketched. Both encrypt at rest, both audit every
read, both can rotate without a redeploy, and neither leaves the plaintext anywhere on the
host between the response and the process that asked for it. That is the difference the
file cannot make up: `.secrets.json` is a durable plaintext artifact, and a manager's
answer is a value in one process's heap.

**Naming a manager disables the file.** `default_providers` returns the environment and
*one* backend, never a manager with the file behind it. A chain that falls back to disk
when Vault is unreachable is strictly worse than one that refuses: it starts, it works,
and it is running on whatever stale secret was last checked out — which is the failure
this module exists to remove, reintroduced as a convenience.

**Precedence** is init > environment > backend > `.env`. Environment beats the backend
because an orchestrator that injects a secret is making a deliberate statement and should
win; `.env` loses to everything because moving secrets out of it is the point.

**A dev default in production is a startup failure.** `check_secret_posture` refuses,
rather than warns, when `environment` is not development and a known placeholder is in
play — the same posture `rls_bootstrap` takes toward a role that can bypass RLS, and for
the same reason: a control that is present and inert is worse than one that is absent,
because it looks finished.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _stdlib_secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

SECRETS_FILE_ENV = "DRAWBRIDGE_SECRETS_FILE"
DEFAULT_SECRETS_FILE = ".secrets.json"

#: Which backend holds the secrets. `file` is the default because the test suite and
#: `make token` must work on a laptop with no manager running; every other value names
#: something that has to be reachable, and startup fails loudly when it is not.
SECRETS_PROVIDER_ENV = "DRAWBRIDGE_SECRETS_PROVIDER"
DEFAULT_PROVIDER = "file"

#: Vault. `VAULT_ADDR` and `VAULT_TOKEN` keep their conventional spellings so an operator
#: who has already run `vault login` in this shell does not have to re-export anything.
VAULT_ADDR_ENV = "VAULT_ADDR"
VAULT_TOKEN_ENV = "VAULT_TOKEN"
VAULT_MOUNT_ENV = "DRAWBRIDGE_VAULT_MOUNT"
VAULT_PATH_ENV = "DRAWBRIDGE_VAULT_PATH"
VAULT_ROLE_ID_ENV = "DRAWBRIDGE_VAULT_ROLE_ID"
VAULT_SECRET_ID_ENV = "DRAWBRIDGE_VAULT_SECRET_ID"
DEFAULT_VAULT_MOUNT = "secret"
DEFAULT_VAULT_PATH = "drawbridge"

#: AWS Secrets Manager. `DRAWBRIDGE_AWS_ENDPOINT_URL` exists so the same code path can be
#: pointed at LocalStack — an integration nobody has run once is not an integration.
AWS_SECRET_ID_ENV = "DRAWBRIDGE_AWS_SECRET_ID"
AWS_REGION_ENV = "DRAWBRIDGE_AWS_REGION"
AWS_ENDPOINT_ENV = "DRAWBRIDGE_AWS_ENDPOINT_URL"

#: Symmetric secrets whose only requirement is that they be long and unpredictable. These
#: are the ones `make secrets-init` can mint, because nothing outside this deployment has
#: an opinion about their value.
GENERATED_FIELDS: frozenset[str] = frozenset(
    {
        "jwt_secret",
        "app_db_password",
        "s3_secret_key",
        "n8n_encryption_key",
        "authentik_secret_key",
        # The API token Authentik mints for akadmin on first start, and the credential
        # `infra/authentik_bootstrap.py` authenticates with. Generated here rather than
        # left to Authentik's own default so the provider, application and property
        # mapping can be created by a script instead of by twelve screens of clicking.
        "authentik_bootstrap_token",
        # The client credentials registered API-client agents exchange at `/auth/token`
        # for a fifteen-minute access token. Long-lived, but only ever exchangeable:
        # presented to any data route directly they are not a bearer token at all.
        "pipeline_client_secret",
        "e2e_client_secret",
    }
)

#: The shortest HS256 key accepted outside development. RFC 7518 §3.2 requires a key at
#: least as long as the hash output; 32 bytes of URL-safe base64 is 43 characters, and a
#: deployment below that is signing every token with something guessable.
MIN_JWT_SECRET_CHARS = 32

#: An Ed25519 seed, and therefore 32 bytes of *hex* rather than base64. Generated, but not
#: by the same call as the rest: `tenant_offboard.py` parses this with `bytes.fromhex`, and
#: a URL-safe token in this field is a signing key that fails at the moment a tenant is
#: being offboarded — the one moment the signature is the whole point.
HEX_FIELDS: frozenset[str] = frozenset({"offboard_signing_key"})

#: Secrets this deployment cannot mint, and must not pretend to.
#:
#: (`service_token` is retired as of v1.1.0 and kept only so older files still load.)
#:
#: `anthropic_api_key` is issued by Anthropic. `service_token` is a JWT that
#: `scripts/mint_token.py` signs with `jwt_secret`, so it cannot exist before that key
#: does and is not random in any case. Generating either would be worse than leaving them
#: empty: `secrets-show` would report a configured credential, and the failure would move
#: from startup to the first request that used it.
SUPPLIED_FIELDS: frozenset[str] = frozenset({"anthropic_api_key", "service_token"})

#: Setting names this module is responsible for. Anything here must not appear in
#: `.env.example` with a real value.
SECRET_FIELDS: frozenset[str] = GENERATED_FIELDS | HEX_FIELDS | SUPPLIED_FIELDS

#: Values that were fine while the only reader was a developer and are a defect the moment
#: `environment` is anything else. Compared case-sensitively and exactly: a password that
#: merely *contains* "dev" is not necessarily a placeholder, and guessing would produce a
#: refusal nobody can act on.
DEV_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "dev-only-change-me",
        "drawbridge",
        "drawbridge-app-local",
        "changeme",
        "change-me",
        "secret",
        "password",
    }
)

_DEVELOPMENT = "development"


class SecretsError(RuntimeError):
    """Configuration is unsafe to start with. Never carries the secret in its message."""


class SecretProvider(Protocol):
    """One source of secrets.

    `load` returns every secret the provider holds, keyed by setting name without the
    `DRAWBRIDGE_` prefix. Returning a whole mapping rather than answering one key at a
    time is deliberate: a provider that is queried per key turns startup into N network
    calls and makes a partial outage look like a partially configured deployment.
    """

    def load(self) -> Mapping[str, str]: ...

    @property
    def describe(self) -> str:
        """Human-readable source, safe to log. Never includes a value."""
        ...


class FileSecretProvider:
    """A JSON object on disk, excluded from git.

    Keys may be given in either `jwt_secret` or `DRAWBRIDGE_JWT_SECRET` form; both are
    normalised, because the file is written by hand as often as by `make secrets-init` and
    a silently ignored key is the worst possible failure here — the service starts, and
    the secret it uses is the one you were trying to replace.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def describe(self) -> str:
        return f"file:{self.path}"

    def load(self) -> Mapping[str, str]:
        if not self.path.is_file():
            return {}
        self._warn_if_world_readable()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = f"{self.path} is not valid JSON: {exc.msg} at line {exc.lineno}"
            raise SecretsError(msg) from exc
        if not isinstance(payload, dict):
            msg = f"{self.path} must contain a JSON object mapping secret names to strings"
            raise SecretsError(msg)
        return _normalise_keys(payload, source=str(self.path))

    def _warn_if_world_readable(self) -> None:
        """Report a permissive mode, and do not refuse to start over it.

        POSIX only — Windows ACLs do not map onto a mode bit, and a check that reported
        every Windows file as insecure would train people to ignore it. Warn rather than
        raise because a container bind-mount frequently arrives 0644 and refusing would
        make the secure path the one people disable.
        """
        if os.name != "posix":
            return
        mode = self.path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning(
                "secrets file %s is readable beyond its owner (mode %o); chmod 600 it",
                self.path,
                stat.S_IMODE(mode),
            )


class EnvSecretProvider:
    """`DRAWBRIDGE_`-prefixed environment variables.

    Kept as a provider rather than left to pydantic-settings so that `describe` can say
    where a value came from when a posture check refuses.
    """

    prefix = "DRAWBRIDGE_"

    @property
    def describe(self) -> str:
        return "environment"

    def load(self) -> Mapping[str, str]:
        found = {
            key: value
            for key, value in os.environ.items()
            if key.startswith(self.prefix) and _strip_prefix(key) in SECRET_FIELDS and value
        }
        return _normalise_keys(found, source="environment")


class VaultSecretProvider:
    """HashiCorp Vault, KV version 2.

    One read of one path holding one JSON object, for the reason `SecretProvider.load`
    gives: a provider queried per key turns startup into N round trips and makes a partial
    outage indistinguishable from a partially configured deployment.

    **Authentication is AppRole by preference, a token only as a fallback.** A long-lived
    root token in `VAULT_TOKEN` is the same problem as a secret in `.env`, moved one layer
    along and given a better name. AppRole splits the credential in two: a `role_id` that
    is deployment configuration and may sit in the compose file, and a `secret_id` that is
    short-lived and delivered at start. What this process ends up holding is a token Vault
    issued to it, with that token's own TTL and its own audit trail — so a leak is bounded
    in time and visible after the fact, neither of which is true of a file.

    Talks to Vault over `httpx` rather than through `hvac`. The KV v2 read is one GET and
    the AppRole login is one POST, `httpx` is already a dependency, and a client library
    for two endpoints is a supply-chain edge bought for nothing.
    """

    def __init__(
        self,
        *,
        address: str,
        path: str = DEFAULT_VAULT_PATH,
        mount: str = DEFAULT_VAULT_MOUNT,
        token: str | None = None,
        role_id: str | None = None,
        secret_id: str | None = None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.address = address.rstrip("/")
        self.path = path.strip("/")
        self.mount = mount.strip("/")
        self.token = token
        self.role_id = role_id
        self.secret_id = secret_id
        self.timeout = timeout
        self._client = client

    @property
    def describe(self) -> str:
        auth = "approle" if self.role_id else "token"
        return f"vault:{self.address}/{self.mount}/{self.path} ({auth})"

    def load(self) -> Mapping[str, str]:
        client = self._client or httpx.Client(base_url=self.address, timeout=self.timeout)
        try:
            token = self._authenticate(client)
            payload = self._read(client, token)
        finally:
            if self._client is None:
                client.close()
        return _normalise_keys(payload, source=self.describe)

    def _authenticate(self, client: httpx.Client) -> str:
        """An AppRole login, or the token we were handed.

        AppRole wins when both are configured. Someone who set a `role_id` has done the
        deliberate thing, and silently preferring a stale `VAULT_TOKEN` left over from an
        operator's `vault login` would make the deployment depend on a human's shell.
        """
        if not (self.role_id and self.secret_id):
            if not self.token:
                msg = (
                    f"vault at {self.address} needs credentials: set "
                    f"{VAULT_ROLE_ID_ENV} and {VAULT_SECRET_ID_ENV} for AppRole, "
                    f"or {VAULT_TOKEN_ENV} for a token."
                )
                raise SecretsError(msg)
            return self.token

        response = self._request(
            client,
            "POST",
            "/v1/auth/approle/login",
            json={"role_id": self.role_id, "secret_id": self.secret_id},
        )
        auth = response.get("auth")
        if not isinstance(auth, dict) or not isinstance(auth.get("client_token"), str):
            msg = f"vault AppRole login at {self.address} returned no client_token"
            raise SecretsError(msg)
        return str(auth["client_token"])

    def _read(self, client: httpx.Client, token: str) -> Mapping[str, Any]:
        """The KV v2 payload at `mount/data/path`.

        `data.data` rather than `data`: KV v2 wraps the secret in a version envelope, and
        reading the outer object would produce a mapping whose keys are `data` and
        `metadata` — both rejected by `_normalise_keys` as unknown secrets, which is the
        right failure but an unhelpful one to have to diagnose.
        """
        body = self._request(
            client,
            "GET",
            f"/v1/{self.mount}/data/{self.path}",
            headers={"X-Vault-Token": token},
        )
        outer = body.get("data")
        if not isinstance(outer, dict):
            msg = f"vault path {self.mount}/{self.path} holds no data"
            raise SecretsError(msg)
        inner = outer.get("data")
        if not isinstance(inner, dict):
            msg = (
                f"vault path {self.mount}/{self.path} is not a KV v2 secret "
                f"(no data.data); check the mount is version 2"
            )
            raise SecretsError(msg)
        return inner

    def _request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> Mapping[str, Any]:
        """One Vault call, with the response body kept out of the error message.

        Vault echoes a good deal in its errors and a 403 body can name paths and policies.
        The status and the URL are enough to act on, and neither is a secret.
        """
        try:
            response = client.request(
                method, url, headers=dict(headers or {}), json=json, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            msg = f"vault at {self.address} is unreachable: {type(exc).__name__}"
            raise SecretsError(msg) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            msg = f"vault has nothing at {url}; write the secret before starting"
            raise SecretsError(msg)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            msg = f"vault refused {method} {url} with HTTP {response.status_code}"
            raise SecretsError(msg)
        parsed = response.json()
        if not isinstance(parsed, dict):
            msg = f"vault returned a non-object body for {url}"
            raise SecretsError(msg)
        return parsed


class AwsSecretsManagerProvider:
    """AWS Secrets Manager — one secret holding a JSON object of every field.

    One secret rather than one per field, and one `GetSecretValue` rather than N. Secrets
    Manager bills per secret per month and per API call, but that is the smaller reason:
    N calls at startup means a partial failure leaves the process holding a partially
    configured deployment, which is precisely the state `check_secret_posture` exists to
    make impossible.

    `endpoint_url` is a first-class constructor argument so this exact code path can be
    pointed at LocalStack and exercised. An integration that has never authenticated once
    is a liability dressed as progress, and the way to stop it being one is to run it.

    Credentials are boto3's own resolution chain — instance role, task role, profile,
    environment — and deliberately not configured here. Putting an access key in this
    file's own configuration would mean holding a secret in order to fetch secrets.
    """

    def __init__(
        self,
        secret_id: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
    ) -> None:
        self.secret_id = secret_id
        self.region = region
        self.endpoint_url = endpoint_url
        self._client = client

    @property
    def describe(self) -> str:
        where = self.endpoint_url or self.region or "default region"
        return f"aws-secrets-manager:{self.secret_id} ({where})"

    def build_client(self) -> Any:
        """The boto3 client this provider would use.

        Public because `manage_secrets.py push` writes through the same client the reader
        builds. Two places constructing a client from two readings of the same three
        environment variables is how a push lands in a different account from the read.
        """
        try:
            # Imported here rather than at module scope: the file and vault backends must
            # not need the AWS SDK importable to start.
            import boto3
        except ImportError as exc:  # pragma: no cover - boto3 is a hard dependency
            msg = "the aws secrets backend needs boto3 installed"
            raise SecretsError(msg) from exc
        return boto3.client(
            "secretsmanager", region_name=self.region, endpoint_url=self.endpoint_url
        )

    def load(self) -> Mapping[str, str]:
        try:
            # Construction is inside the try because it is not free of failure:
            # `boto3.client` raises `NoRegionError` before any network call when neither
            # a region nor a profile is configured, and letting a botocore exception out
            # of this module would break the one promise `SecretProvider` makes — that a
            # failure to load is a `SecretsError` and never an empty mapping.
            client = self._client if self._client is not None else self.build_client()
            response = client.get_secret_value(SecretId=self.secret_id)
        except SecretsError:
            raise
        except Exception as exc:
            # Every botocore failure funnels here on purpose. `ResourceNotFoundException`,
            # `AccessDeniedException` and an expired instance role are one situation from
            # this module's point of view — the secret did not arrive — and the exception
            # type is preserved in the chain for whoever reads the traceback.
            msg = (
                f"AWS Secrets Manager could not return {self.secret_id!r}: "
                f"{type(exc).__name__}. Check the secret exists and the role may read it."
            )
            raise SecretsError(msg) from exc

        payload = response.get("SecretString")
        if not isinstance(payload, str):
            # A binary secret is a deliberate choice by whoever wrote it, and guessing an
            # encoding for it would be the kind of silent reinterpretation this module
            # refuses everywhere else.
            msg = f"secret {self.secret_id!r} holds binary, not a JSON object of secrets"
            raise SecretsError(msg)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            msg = f"secret {self.secret_id!r} is not valid JSON: {exc.msg}"
            raise SecretsError(msg) from exc
        if not isinstance(parsed, dict):
            msg = f"secret {self.secret_id!r} must be a JSON object mapping names to strings"
            raise SecretsError(msg)
        return _normalise_keys(parsed, source=self.describe)


def _strip_prefix(key: str) -> str:
    without = key[len("DRAWBRIDGE_") :] if key.startswith("DRAWBRIDGE_") else key
    return without.lower()


def _normalise_keys(payload: Mapping[str, Any], *, source: str) -> dict[str, str]:
    """Lower-case, unprefixed keys with string values, refusing anything unrecognised.

    An unknown key is an error rather than a shrug. The common cause is a typo, and a
    typo'd secret name is indistinguishable at runtime from a secret that was never set.
    """
    resolved: dict[str, str] = {}
    for raw_key, raw_value in payload.items():
        key = _strip_prefix(str(raw_key))
        if key.startswith("_"):
            # Comment keys. The generated file carries a "_about" block.
            continue
        if key not in SECRET_FIELDS:
            known = ", ".join(sorted(SECRET_FIELDS))
            msg = f"unknown secret {key!r} in {source}; known secrets are {known}"
            raise SecretsError(msg)
        if raw_value is None or raw_value == "":
            continue
        if not isinstance(raw_value, str):
            msg = f"secret {key!r} in {source} must be a string"
            raise SecretsError(msg)
        resolved[key] = raw_value
    return resolved


def secrets_path() -> Path:
    return Path(os.environ.get(SECRETS_FILE_ENV, DEFAULT_SECRETS_FILE))


def _vault_provider() -> VaultSecretProvider:
    address = os.environ.get(VAULT_ADDR_ENV, "").strip()
    if not address:
        msg = f"{SECRETS_PROVIDER_ENV}=vault but {VAULT_ADDR_ENV} is unset"
        raise SecretsError(msg)
    return VaultSecretProvider(
        address=address,
        mount=os.environ.get(VAULT_MOUNT_ENV, DEFAULT_VAULT_MOUNT),
        path=os.environ.get(VAULT_PATH_ENV, DEFAULT_VAULT_PATH),
        token=os.environ.get(VAULT_TOKEN_ENV) or None,
        role_id=os.environ.get(VAULT_ROLE_ID_ENV) or None,
        secret_id=os.environ.get(VAULT_SECRET_ID_ENV) or None,
    )


def _aws_provider() -> AwsSecretsManagerProvider:
    secret_id = os.environ.get(AWS_SECRET_ID_ENV, "").strip()
    if not secret_id:
        msg = f"{SECRETS_PROVIDER_ENV}=aws but {AWS_SECRET_ID_ENV} is unset"
        raise SecretsError(msg)
    return AwsSecretsManagerProvider(
        secret_id,
        region=os.environ.get(AWS_REGION_ENV) or None,
        endpoint_url=os.environ.get(AWS_ENDPOINT_ENV) or None,
    )


#: Backend name to constructor. Adding one is a line here and a class above; nothing at a
#: call site changes, which is what the `SecretProvider` protocol was for.
BACKENDS: dict[str, Callable[[], SecretProvider]] = {
    "file": lambda: FileSecretProvider(secrets_path()),
    "vault": _vault_provider,
    "aws": _aws_provider,
}


def backend_name() -> str:
    """Which backend is configured, validated.

    An unrecognised value is fatal rather than a fall back to `file`. `PROVIDER=valut` is
    a typo, and quietly reading the local file instead would produce a deployment that
    starts, works, and is not using the manager anyone believes it is using.
    """
    name = os.environ.get(SECRETS_PROVIDER_ENV, DEFAULT_PROVIDER).strip().lower()
    if name not in BACKENDS:
        known = ", ".join(sorted(BACKENDS))
        msg = f"unknown {SECRETS_PROVIDER_ENV}={name!r}; known backends are {known}"
        raise SecretsError(msg)
    return name


def default_providers() -> tuple[SecretProvider, ...]:
    """The environment, then exactly one backend. Earlier wins.

    An orchestrator injecting a value is making a deliberate statement; the backend is the
    standing configuration. Reversing them would mean a deployment could not override a
    stale secret, which is the situation people hit at 3am.

    There is deliberately no chain past the backend. `vault` does not fall back to the
    file: a manager that is unreachable must stop the deployment, not hand it whatever
    plaintext happens to be on the disk.
    """
    return (EnvSecretProvider(), BACKENDS[backend_name()]())


def resolve(providers: Iterable[SecretProvider] | None = None) -> dict[str, str]:
    """Merge every provider into one mapping, first writer wins.

    Missing secrets are simply absent. Deciding whether an absence is fatal belongs to
    `check_secret_posture`, which knows the environment; a loader that raised on a missing
    Anthropic key would stop the test suite from running.
    """
    merged: dict[str, str] = {}
    for provider in providers if providers is not None else default_providers():
        for key, value in provider.load().items():
            merged.setdefault(key, value)
    return merged


def generate() -> dict[str, str]:
    """Fresh values for every secret this deployment is entitled to mint.

    32 bytes each. The JWT secret in particular must clear 32 bytes or PyJWT warns that an
    HMAC key is shorter than the digest it feeds, and a warning people learn to ignore is
    how a short key survives to production.

    `SUPPLIED_FIELDS` are deliberately absent from the result. See their definition.
    """
    minted = {field: _stdlib_secrets.token_urlsafe(32) for field in sorted(GENERATED_FIELDS)}
    minted.update({field: _stdlib_secrets.token_hex(32) for field in sorted(HEX_FIELDS)})
    return minted


def redact(value: str | None) -> str:
    """A stable, non-reversing hint for logs.

    Length and a four-character prefix: enough to tell two secrets apart when diagnosing
    "which key is this service using", not enough to shorten a search meaningfully. A
    secret shorter than eight characters is shown as its length alone, because a prefix
    of a short secret is most of it.
    """
    if not value:
        return "<unset>"
    if len(value) < 8:
        return f"<{len(value)} chars>"
    return f"{value[:4]}… ({len(value)} chars)"


def is_placeholder(value: str | None) -> bool:
    return bool(value) and value in DEV_PLACEHOLDERS


def check_secret_posture(settings: Any) -> None:
    """Refuse to start when a placeholder secret would be load-bearing.

    Called from the API lifespan next to `check_auth_configuration`. Two rules:

    1. Outside development, a known placeholder in any secret is fatal. It is not a
       warning: the whole failure mode here is that the deployment works.
    2. Inside development, the same finding is logged once per field, named but never
       valued, so `make token` and the test suite keep working.

    The DSN is checked as a whole rather than by field because the password lives inside
    it, and a deployment that moved the password out of `DRAWBRIDGE_APP_DB_PASSWORD` and
    left it inline in `DRAWBRIDGE_DATABASE_URL` has not moved it at all.
    """
    production = getattr(settings, "environment", _DEVELOPMENT) != _DEVELOPMENT
    offenders: list[str] = []

    for field in sorted(SECRET_FIELDS):
        if is_placeholder(getattr(settings, field, None)):
            offenders.append(field)

    if _dsn_carries_placeholder(getattr(settings, "database_url", "")):
        offenders.append("database_url (password inline)")

    jwt_secret = getattr(settings, "jwt_secret", None)
    if jwt_secret and len(jwt_secret) < MIN_JWT_SECRET_CHARS:
        offenders.append(f"jwt_secret (shorter than {MIN_JWT_SECRET_CHARS} characters)")

    if not offenders:
        return

    if production:
        msg = (
            f"refusing to start in environment={settings.environment!r}: "
            f"development placeholder values are configured for {', '.join(offenders)}. "
            f"Run `make secrets-init` and deploy the generated secrets."
        )
        raise SecretsError(msg)

    logger.warning(
        "development placeholder secrets in use for %s — never deploy this configuration",
        ", ".join(offenders),
    )


def _dsn_carries_placeholder(dsn: str) -> bool:
    """Whether a DSN's inline password is one of the known placeholders."""
    if "://" not in dsn or "@" not in dsn:
        return False
    authority = dsn.split("://", 1)[1].rsplit("@", 1)[0]
    if ":" not in authority:
        return False
    return is_placeholder(authority.split(":", 1)[1])


def inject_password(dsn: str, password: str | None) -> str:
    """Put a resolved password into a DSN that was configured without one.

    `postgresql+asyncpg://drawbridge_app@postgres:5432/drawbridge` plus a secret becomes
    a usable DSN. A DSN that already carries a password is returned untouched — an
    explicit inline password wins, because someone wrote it there on purpose and silently
    overriding it would be the more surprising behaviour.

    Only the authority section is touched. Passwords are percent-encoded by the caller if
    they need to be; `make secrets-init` emits URL-safe values precisely so they do not.
    """
    if not password or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    authority, tail = rest.rsplit("@", 1)
    if ":" in authority:
        return dsn
    return f"{scheme}://{authority}:{password}@{tail}"

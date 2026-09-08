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
`.secrets.json` on a developer's laptop is exactly as exposed as that laptop. What it
provides is one place to rotate, a narrower blast radius, and — the actual point — a seam.
`SecretProvider` is the interface a real manager implements. `AwsSecretsManagerProvider`
is written here as a working shape with an explicit refusal in place of a network call,
so that adopting Secrets Manager or Vault is a provider swap rather than an edit to every
call site.

**Precedence** is init > environment > secrets file > `.env`. Environment beats the file
because an orchestrator that injects a secret is making a deliberate statement and should
win; `.env` loses to the file because that is the migration this module exists to perform.

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

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

SECRETS_FILE_ENV = "DRAWBRIDGE_SECRETS_FILE"
DEFAULT_SECRETS_FILE = ".secrets.json"

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
    }
)

#: An Ed25519 seed, and therefore 32 bytes of *hex* rather than base64. Generated, but not
#: by the same call as the rest: `tenant_offboard.py` parses this with `bytes.fromhex`, and
#: a URL-safe token in this field is a signing key that fails at the moment a tenant is
#: being offboarded — the one moment the signature is the whole point.
HEX_FIELDS: frozenset[str] = frozenset({"offboard_signing_key"})

#: Secrets this deployment cannot mint, and must not pretend to.
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


class AwsSecretsManagerProvider:
    """The shape a real manager plugs into, with the network call left out.

    This is not a stub that returns fake values — it refuses, loudly, naming what it would
    need. A provider that quietly returned empty would let a deployment start with every
    secret missing and only fail later at the first request, which is exactly the failure
    mode this module exists to prevent.

    Implementing it is one `boto3` call to `get_secret_value(SecretId=self.secret_id)` and
    a `json.loads` of the `SecretString`. It is deliberately not implemented here: there
    is no AWS account behind this project yet, and an untested integration that has never
    authenticated once is a liability dressed as progress.
    """

    def __init__(self, secret_id: str, *, region: str | None = None) -> None:
        self.secret_id = secret_id
        self.region = region

    @property
    def describe(self) -> str:
        return f"aws-secrets-manager:{self.secret_id}"

    def load(self) -> Mapping[str, str]:
        msg = (
            f"AWS Secrets Manager provider is a seam, not an implementation: "
            f"resolving {self.secret_id!r} needs boto3, credentials and a region "
            f"(got {self.region!r}). Use a secrets file until that exists."
        )
        raise SecretsError(msg)


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


def default_providers() -> tuple[SecretProvider, ...]:
    """Environment first, then the file. Earlier wins.

    An orchestrator injecting a value is making a deliberate statement; a file on disk is
    the standing configuration. Reversing these would mean a deployment could not override
    a stale checked-out secrets file, which is the situation people hit at 3am.
    """
    return (EnvSecretProvider(), FileSecretProvider(secrets_path()))


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

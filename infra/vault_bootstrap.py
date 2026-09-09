"""Configure Vault so Drawbridge can read its secrets, and prove that it can.

    docker compose --profile secrets up -d vault
    python infra/vault_bootstrap.py                # configure, push, verify
    python infra/vault_bootstrap.py --verify-only  # read back through the provider

Declarative in the sense that matters: every step checks the current state and makes only
the change that is missing, so running it twice is not an error and running it against a
half-configured Vault finishes the job. Nothing here is destructive except `--rotate`,
which asks for a new AppRole `secret_id` and says what that invalidates.

**What it builds.**

1. A KV version 2 mount. Version 2 rather than 1 for two properties that matter to a
   credential store: every write keeps the previous version, so a rotation that breaks
   the deployment is one API call from being undone, and a delete is a tombstone rather
   than an erasure, so an accidental `vault kv delete` is recoverable.
2. A policy granting **read on exactly one path** and nothing else. Not `secret/*`, and
   not the default `root` token everybody reaches for: the API's credential should be
   able to answer one question, and a policy that can list the mount turns a leaked token
   into a map of the deployment.
3. An AppRole with that policy. AppRole is the whole reason this is worth doing — see
   `VaultSecretProvider` for why a long-lived token in an environment variable is the
   original problem wearing a better name.
4. The secrets themselves, written from `.secrets.json` in one object.

**What it does not build.** Any of this in a production shape. `docker compose` runs Vault
in dev mode: unsealed automatically, storage in memory, a root token supplied on the
command line and printed to the logs. Everything above is real and none of it survives a
restart. A production Vault is sealed, has Shamir or auto-unseal keys held by people who
are not this script, and persists to disk — and this file's job is to make the
*application* side of that indistinguishable, so the only thing left to do for real is
operate Vault properly.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as _stdlib_secrets
import sys
from pathlib import Path
from typing import Any

import httpx

from services.api.src.secrets import (
    DEFAULT_VAULT_MOUNT,
    DEFAULT_VAULT_PATH,
    SECRET_FIELDS,
    SecretsError,
    VaultSecretProvider,
    redact,
    secrets_path,
)

DEFAULT_ADDR = "http://localhost:8200"
DEFAULT_ROOT_TOKEN = "drawbridge-dev-root"
POLICY_NAME = "drawbridge-api"
ROLE_NAME = "drawbridge-api"

#: Where the credentials that Postgres, MinIO and n8n read live, and the role that may
#: read them. Deliberately a different path and a different AppRole from the API's.
#:
#: Before week 15 these six were plaintext files under ./secrets, and the separation was
#: accidental but real: the API process could not read the Postgres *owner* password or
#: the n8n encryption key, because they were not in `.secrets.json`. Moving them into the
#: same Vault path as the application's secrets would have handed the API everything and
#: called it an improvement. The split is the part worth keeping; Vault is just where it
#: now lives.
INFRA_PATH = "drawbridge-infra"
INFRA_POLICY_NAME = "drawbridge-infra"
INFRA_ROLE_NAME = "drawbridge-infra"

#: What vault-agent renders. Five are generated here; `service_token` is a JWT the
#: identity provider signs and cannot be minted from random bytes, so it is supplied.
INFRA_GENERATED: tuple[str, ...] = (
    "postgres_password",
    "minio_root_user",
    "minio_root_password",
    "authentik_secret_key",
    "n8n_encryption_key",
)
INFRA_SUPPLIED: tuple[str, ...] = ("service_token",)
INFRA_FIELDS: tuple[str, ...] = INFRA_GENERATED + INFRA_SUPPLIED

#: The AppRole token's lifetime. Twenty minutes is longer than a container start and
#: shorter than a shift: the token this process ends up holding is useless to anyone who
#: extracts it from a core dump tomorrow. It is not renewed, because secrets are read once
#: at startup and a credential that stays valid for the life of the process would be a
#: long-lived token with extra steps.
TOKEN_TTL = "20m"


class BootstrapError(RuntimeError):
    """A step could not complete. The message names the step, never a credential."""


class Vault:
    """The handful of Vault endpoints this needs, over `httpx`.

    A client for four endpoints would be a dependency bought for nothing; see
    `VaultSecretProvider` for the same reasoning applied to the read path.
    """

    def __init__(self, address: str, token: str, *, timeout: float = 15.0) -> None:
        self.address = address.rstrip("/")
        self._client = httpx.Client(
            base_url=self.address,
            headers={"X-Vault-Token": token},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def call(
        self, method: str, path: str, *, json_body: Any = None, allow: tuple[int, ...] = ()
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            msg = f"vault at {self.address} is unreachable: {type(exc).__name__}"
            raise BootstrapError(msg) from exc
        if response.status_code in allow:
            return {}
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # Vault's error bodies name paths and policies. The status and the path are
            # enough to act on and neither is a secret.
            msg = f"vault refused {method} {path} with HTTP {response.status_code}"
            raise BootstrapError(msg)
        if not response.content:
            return {}
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}


def ensure_kv_v2(vault: Vault, mount: str) -> str:
    """Mount KV v2 at `mount`, or confirm what is already there is version 2.

    A version 1 mount already holding data is left alone and reported rather than
    upgraded: `VaultSecretProvider` reads `data.data` and would fail loudly against v1,
    which is a better outcome than this script silently migrating somebody's store.
    """
    mounts = vault.call("GET", "/v1/sys/mounts")
    existing = mounts.get(f"{mount}/")
    if isinstance(existing, dict):
        version = str(existing.get("options", {}).get("version", "1"))
        if version != "2":
            msg = (
                f"{mount}/ is mounted as KV version {version}; this deployment reads v2. "
                f"Mount a v2 path and set DRAWBRIDGE_VAULT_MOUNT to it."
            )
            raise BootstrapError(msg)
        return "already a KV v2 mount"
    vault.call(
        "POST",
        f"/v1/sys/mounts/{mount}",
        json_body={"type": "kv", "options": {"version": "2"}},
    )
    return "mounted KV v2"


def ensure_policy(vault: Vault, mount: str, path: str, *, name: str = POLICY_NAME) -> str:
    """Read on one path. No list, no write, no metadata.

    `list` is withheld on purpose. It reads as harmless and it is not: a token that can
    enumerate a mount tells whoever holds it what else exists to go after, which converts
    one leaked credential into a plan.
    """
    document = (
        f'path "{mount}/data/{path}" {{\n'
        f'  capabilities = ["read"]\n'
        f"}}\n"
        f'path "{mount}/metadata/{path}" {{\n'
        f'  capabilities = ["read"]\n'
        f"}}\n"
    )
    current = vault.call("GET", f"/v1/sys/policies/acl/{name}", allow=(404,))
    if current.get("data", {}).get("policy") == document:
        return "already current"
    vault.call("PUT", f"/v1/sys/policies/acl/{name}", json_body={"policy": document})
    return f"read-only on {mount}/{path}"


def ensure_approle(
    vault: Vault,
    *,
    rotate: bool,
    name: str = ROLE_NAME,
    policy: str = POLICY_NAME,
    token_ttl: str = TOKEN_TTL,
) -> tuple[str, str, str]:
    """The AppRole, its `role_id`, and a `secret_id`. Returns (note, role_id, secret_id).

    `role_id` is stable and is deployment configuration — it may sit in a compose file.
    `secret_id` is the credential, is generated fresh here, and is shown exactly once,
    which is why this function returns it rather than logging it.
    """
    auth = vault.call("GET", "/v1/sys/auth")
    if "approle/" not in auth:
        vault.call("POST", "/v1/sys/auth/approle", json_body={"type": "approle"})

    vault.call(
        "POST",
        f"/v1/auth/approle/role/{name}",
        json_body={
            "token_policies": [policy],
            "token_ttl": token_ttl,
            "token_max_ttl": token_ttl,
            # The number of times one secret_id may be used to log in. Zero is unlimited,
            # which is what a container that restarts needs; the bound that matters here
            # is the token's TTL, not the login count.
            "secret_id_num_uses": 0,
            "secret_id_ttl": "0",
        },
    )
    role_id = str(vault.call("GET", f"/v1/auth/approle/role/{name}/role-id")["data"]["role_id"])

    if rotate:
        vault.call("POST", f"/v1/auth/approle/role/{name}/secret-id/destroy", allow=(404,))
    issued = vault.call("POST", f"/v1/auth/approle/role/{name}/secret-id")
    secret_id = str(issued["data"]["secret_id"])
    note = "rotated" if rotate else "issued"
    return note, role_id, secret_id


def push_secrets(vault: Vault, mount: str, path: str, source: Path) -> str:
    """Write `.secrets.json` into the KV path as one object.

    One write of a complete object rather than a write per field: KV v2's version history
    is only useful as a rollback if each version is a coherent configuration, and a
    field-at-a-time push leaves versions that are half of one rotation and half of
    another.
    """
    if not source.is_file():
        msg = f"{source} does not exist; run `make secrets-init` first"
        raise BootstrapError(msg)
    payload = json.loads(source.read_text(encoding="utf-8"))
    values = {k: v for k, v in payload.items() if not k.startswith("_") and v}
    unknown = sorted(set(values) - set(SECRET_FIELDS))
    if unknown:
        msg = (
            f"{source} carries unknown secrets {', '.join(unknown)}; "
            f"Vault would end up holding keys nothing can read"
        )
        raise BootstrapError(msg)
    written = vault.call("POST", f"/v1/{mount}/data/{path}", json_body={"data": values})
    version = written.get("data", {}).get("version", "?")
    return f"{len(values)} secret(s) at {mount}/{path}, version {version}"


def verify(address: str, mount: str, path: str, role_id: str, secret_id: str) -> int:
    """Read the secrets back through the provider the API actually uses.

    Through `VaultSecretProvider` rather than a direct HTTP call, because a bootstrap that
    verifies itself with its own client proves the bootstrap works and nothing about
    whether the application can start. This logs in with the AppRole, reads the path, and
    reports names and redactions — which is the same code path `Settings` runs at boot.
    """
    provider = VaultSecretProvider(
        address=address, mount=mount, path=path, role_id=role_id, secret_id=secret_id
    )
    try:
        resolved = provider.load()
    except SecretsError as exc:
        print(f"  verify FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"  read back through {provider.describe}")
    for name in sorted(resolved):
        print(f"    {name:<24} {redact(resolved[name])}")
    missing = sorted(SECRET_FIELDS - set(resolved))
    if missing:
        print(f"    not in vault: {', '.join(missing)}")
    return 0


def ensure_infra_secrets(vault: Vault, mount: str, path: str, *, service_token: str) -> str:
    """Put the six credentials vault-agent renders into Vault, generating what it can.

    **Read-modify-write, and every existing value wins.** Regenerating a credential that
    is already in force is not a rotation, it is an outage: `postgres_password` is the
    owner role's password and Postgres set it at initdb, `authentik_secret_key` decrypts
    every stored session, and `n8n_encryption_key` decrypts every stored workflow
    credential. A bootstrap that is safe to re-run has to be a bootstrap that cannot
    quietly replace any of the three. Rotation is a separate, deliberate act — write the
    new value, then restart the consumer — and `secrets/README.md` says which of these
    survive it.

    `service_token` is the exception in the other direction: it is a JWT the identity
    provider signs, so it cannot be generated from random bytes and has to be supplied.
    Passing nothing leaves whatever is already there, which is what a re-run wants.
    """
    current = vault.call("GET", f"/v1/{mount}/data/{path}", allow=(404,))
    existing = current.get("data", {}).get("data") or {}

    values = dict(existing)
    minted: list[str] = []
    for field in INFRA_GENERATED:
        if not values.get(field):
            # url-safe: these reach Postgres and MinIO through a DSN and a shell, and a
            # password carrying '@' or '/' is a connection string bug waiting to be
            # diagnosed as a wrong password.
            values[field] = _stdlib_secrets.token_urlsafe(32)
            minted.append(field)
    if service_token:
        values["service_token"] = service_token

    unknown = sorted(set(values) - set(INFRA_FIELDS))
    if unknown:
        msg = (
            f"{mount}/{path} carries {', '.join(unknown)}, which vault-agent has no "
            f"template for; those values would be unreachable"
        )
        raise BootstrapError(msg)

    if values == existing:
        return f"{len(values)} credential(s) already at {mount}/{path}, unchanged"
    written = vault.call("POST", f"/v1/{mount}/data/{path}", json_body={"data": values})
    version = written.get("data", {}).get("version", "?")
    note = f"generated {', '.join(minted)}" if minted else "updated"
    missing = [f for f in INFRA_FIELDS if not values.get(f)]
    tail = f"; still missing {', '.join(missing)}" if missing else ""
    return f"{note} at {mount}/{path}, version {version}{tail}"


def bootstrap_infra(vault: Vault, mount: str, *, rotate: bool, service_token: str) -> str:
    """Configure the infra path, its policy, its role, and return the secret_id.

    Prints nothing. The caller owns the one place a credential reaches stdout.
    """
    ensure_kv_v2(vault, mount)
    ensure_policy(vault, mount, INFRA_PATH, name=INFRA_POLICY_NAME)
    print(
        f"  infra     {ensure_infra_secrets(vault, mount, INFRA_PATH, service_token=service_token)}"
    )
    # Longer than the API's twenty minutes because this process is long-lived and renews
    # on its own schedule; a token that expired between renewals would leave the rendered
    # files stale with nothing saying so.
    _, role_id, secret_id = ensure_approle(
        vault,
        rotate=rotate,
        name=INFRA_ROLE_NAME,
        policy=INFRA_POLICY_NAME,
        token_ttl="1h",
    )
    print(f"  approle   {INFRA_ROLE_NAME}: read on {mount}/{INFRA_PATH} only")
    return f"{role_id}\n{secret_id}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure Vault for Drawbridge.")
    parser.add_argument("--addr", default=os.environ.get("VAULT_ADDR") or DEFAULT_ADDR)
    parser.add_argument(
        "--token",
        default=os.environ.get("VAULT_TOKEN") or DEFAULT_ROOT_TOKEN,
        help="an operator token with sys/ access; the dev root token by default",
    )
    parser.add_argument("--mount", default=DEFAULT_VAULT_MOUNT)
    parser.add_argument("--path", default=DEFAULT_VAULT_PATH)
    parser.add_argument("--secrets-file", type=Path, default=None)
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="destroy existing secret_ids before issuing; every running service must be "
        "restarted with the new one",
    )
    parser.add_argument(
        "--infra",
        action="store_true",
        help="configure secret/drawbridge-infra and the vault-agent AppRole instead of "
        "the application's. These are the credentials Postgres, MinIO and n8n read, and "
        "they are a separate path and role on purpose: the API has no business being "
        "able to read the database owner's password.",
    )
    parser.add_argument(
        "--service-token",
        default=os.environ.get("DRAWBRIDGE_SERVICE_TOKEN", ""),
        help="--infra only: the JWT n8n carries. Cannot be generated; mint it with "
        "scripts/mint_token.py --service. Omitting it leaves whatever Vault already has.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="skip configuration; read back with DRAWBRIDGE_VAULT_ROLE_ID / _SECRET_ID",
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        role_id = os.environ.get("DRAWBRIDGE_VAULT_ROLE_ID", "")
        secret_id = os.environ.get("DRAWBRIDGE_VAULT_SECRET_ID", "")
        if not (role_id and secret_id):
            print(
                "--verify-only needs DRAWBRIDGE_VAULT_ROLE_ID and DRAWBRIDGE_VAULT_SECRET_ID",
                file=sys.stderr,
            )
            return 2
        return verify(args.addr, args.mount, args.path, role_id, secret_id)

    if args.infra:
        vault = Vault(args.addr, args.token)
        try:
            print(f"vault {args.addr}")
            role_id, secret_id = bootstrap_infra(
                vault, args.mount, rotate=args.rotate, service_token=args.service_token
            ).split("\n")
        except BootstrapError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        finally:
            vault.close()
        print("\nPut these in .env.onprem. ./secrets is not needed and should not exist:")
        print(f"  DRAWBRIDGE_VAULT_AGENT_ROLE_ID={role_id}")
        print(f"  DRAWBRIDGE_VAULT_AGENT_SECRET_ID={secret_id}")
        print(f"  DRAWBRIDGE_VAULT_INFRA_PATH={INFRA_PATH}")
        return 0

    vault = Vault(args.addr, args.token)
    try:
        print(f"vault {args.addr}")
        print(f"  mount     {ensure_kv_v2(vault, args.mount)}")
        print(f"  policy    {POLICY_NAME}: {ensure_policy(vault, args.mount, args.path)}")
        source = args.secrets_file or secrets_path()
        print(f"  secrets   {push_secrets(vault, args.mount, args.path, source)}")
        note, role_id, secret_id = ensure_approle(vault, rotate=args.rotate)
        print(f"  approle   {ROLE_NAME}: secret_id {note}, token ttl {TOKEN_TTL}")
    except BootstrapError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        vault.close()

    status = verify(args.addr, args.mount, args.path, role_id, secret_id)
    if status:
        return status

    # Printed to stdout on purpose, and it is the one place this script emits a credential.
    # The alternative is writing it to a file, which would recreate the artifact the whole
    # exercise exists to remove.
    print("\nPut these in the environment of the services that read secrets:")
    print("  DRAWBRIDGE_SECRETS_PROVIDER=vault")
    print(f"  VAULT_ADDR={args.addr}")
    print(f"  DRAWBRIDGE_VAULT_ROLE_ID={role_id}")
    print(f"  DRAWBRIDGE_VAULT_SECRET_ID={secret_id}")
    print("\nrole_id is configuration and may be committed. secret_id is a credential,")
    print("is shown once, and is not recoverable from Vault — rerun with --rotate for a new one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

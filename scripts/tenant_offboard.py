"""Offboard a tenant: sign the ledger, archive it, tombstone the row.

Week 10 made `audit_ledger.tenant_id` RESTRICT and said so out loud — a tenant with ledger
rows cannot be deleted, because a retention obligation a `DELETE` satisfies is not a
retention obligation. That left offboarding with no procedure at all, which is not a
posture so much as a gap: the first departing client would have been handled by whatever
someone typed into psql that afternoon.

This is the procedure, and it is three steps in one transaction-shaped sequence.

**Export.** The tenant's whole chain, oldest first, as JSON lines — one entry per line,
canonical JSON, the same serialisation `ledger._digest` hashes. Alongside it a manifest
carrying the tenant's identity, the sequence range, the result of `verify_chain` at the
moment of export, and the SHA-256 of the body.

**Sign.** Ed25519 over the manifest's canonical bytes, which commit to the body digest. The
signature is what makes the artifact evidence rather than a file: years later, a customs
authority is being shown records produced by the party they are auditing, and "this is what
our database said" is a weaker statement than "this is what our database said, and here is
a signature made before the relationship ended". The verifying key goes on the tenant row
and into the manifest; the private key never touches this repository and is read from
`DRAWBRIDGE_OFFBOARD_SIGNING_KEY`.

**Tombstone.** `tenants.offboarded_at` is set and the artifact key, signature and public key
are recorded on the row. `tenancy.app_current_tenant()` stops resolving that tenant from
that moment, so every RLS-scoped query sees no documents, no claims, no ledger and no
queue — while the rows themselves stay exactly where §163 and GCC Art. 175 require them.
The CHECK constraint on `tenants` refuses the tombstone if any of the three evidence
columns is missing, so a tenant cannot be marked gone without an export behind it.

This script connects as the **owner** role, not `drawbridge_app`. It reads across a tenant
that is about to become invisible and writes a column that no policy would let the tenant's
own connection write, so it is deliberately outside the isolation boundary — which is why
it is a script an operator runs and not a route the API exposes.

Usage:

    uv run python scripts/tenant_offboard.py --tenant <uuid>
    uv run python scripts/tenant_offboard.py --tenant <uuid> --dry-run
    uv run python scripts/tenant_offboard.py --generate-key
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from services.api.src.config import get_settings
from services.api.src.ledger import verify_chain
from services.api.src.models import AuditLedger, Claim, Tenant

if TYPE_CHECKING:
    from collections.abc import Sequence

SIGNING_KEY_ENV = "DRAWBRIDGE_OFFBOARD_SIGNING_KEY"

# States a claim may be in when its tenant leaves. Anything else is work in flight, and
# archiving a chain that is still growing produces an artifact that is wrong the moment it
# is signed.
#
# `packaged` and `handed_off` are deliberately not here. A packet that has been rendered or
# given to a broker has not been filed, and the events that would close it — the filing, the
# refund — are still to come. Offboarding there is a decision someone should have to make
# with `--force`.
TERMINAL_STATES = frozenset({"filed", "paid", "rejected", "expired"})


class OffboardError(RuntimeError):
    """The tenant is not in a state that can be offboarded."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """What the export produced, before anything is written anywhere."""

    body: bytes
    manifest: dict[str, Any]
    signature: str
    public_key: str

    @property
    def key(self) -> str:
        return str(self.manifest["artifact_key"])


# --------------------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------------------


def load_signing_key() -> Ed25519PrivateKey:
    """The offboarding key, from the environment.

    Refuses to invent one. A signature made with a key that existed for the duration of a
    single process proves nothing, and generating a key silently would produce artifacts
    that look signed and cannot be verified against anything.
    """
    raw = os.environ.get(SIGNING_KEY_ENV, "").strip()
    if not raw:
        raise OffboardError(
            f"{SIGNING_KEY_ENV} is not set. Generate one with --generate-key and store it "
            "in the secret manager; it is the only thing that makes the export evidence."
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))
    except ValueError as exc:
        raise OffboardError(f"{SIGNING_KEY_ENV} is not a 32-byte hex seed: {exc}") from exc


def public_key_hex(key: Ed25519PublicKey) -> str:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def canonical(payload: dict[str, Any]) -> bytes:
    """The same canonicalisation `ledger._digest` uses: sorted keys, no incidental space."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_artifact(manifest: dict[str, Any], body: bytes, signature: str) -> bool:
    """Check an archived export against its signature. The auditor's half of this script.

    Recomputes the body digest before checking the signature, so a manifest that was
    re-signed over different content fails on the digest rather than passing on the
    signature.
    """
    if hashlib.sha256(body).hexdigest() != manifest.get("body_sha256"):
        return False
    signed = {key: value for key, value in manifest.items() if key != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(str(manifest["public_key"]))).verify(
            bytes.fromhex(signature), canonical(signed)
        )
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------


def _entry_row(entry: AuditLedger) -> dict[str, Any]:
    """One ledger row as it appears in the archive.

    Every column, including `entry_hash` and `prev_hash`. The chain is the point of the
    artifact: an auditor with this file can recompute it without our database, which is
    what distinguishes an export from a report.
    """
    return {
        "sequence": entry.sequence,
        "ledger_id": str(entry.ledger_id),
        "tenant_id": str(entry.tenant_id),
        "claim_id": str(entry.claim_id) if entry.claim_id else None,
        "event_type": entry.event_type,
        "actor": entry.actor,
        "subject": entry.subject,
        "document_sha256": entry.document_sha256,
        "payload": entry.payload or {},
        "prev_hash": entry.prev_hash,
        "entry_hash": entry.entry_hash,
        "trace_id": entry.trace_id,
        "recorded_at": entry.recorded_at.isoformat() if entry.recorded_at else None,
    }


def build_artifact(
    session: Session, tenant: Tenant, *, key: Ed25519PrivateKey, now: datetime
) -> Artifact:
    """Serialise, hash and sign the tenant's chain. Writes nothing."""
    entries: Sequence[AuditLedger] = (
        session.execute(
            select(AuditLedger)
            .where(AuditLedger.tenant_id == tenant.tenant_id)
            .order_by(AuditLedger.sequence)
        )
        .scalars()
        .all()
    )
    body = b"\n".join(canonical(_entry_row(entry)) for entry in entries)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    manifest = {
        "artifact_key": f"tenants/{tenant.tenant_id}/offboarding/{stamp}-audit-ledger.jsonl",
        "schema": "drawbridge.offboarding.v1",
        "tenant_id": str(tenant.tenant_id),
        "tenant_name": tenant.name,
        "default_jurisdiction": tenant.default_jurisdiction,
        "exported_at": now.isoformat(),
        "entries": len(entries),
        "first_sequence": entries[0].sequence if entries else None,
        "last_sequence": entries[-1].sequence if entries else None,
        "chain": verify_chain(session, tenant.tenant_id),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "retention": "19 CFR 163 (US, 5y from liquidation); GCC Art. 175 / ZATCA Res. 28624",
        "algorithm": "ed25519",
        "public_key": public_key_hex(key.public_key()),
    }
    return Artifact(
        body=body,
        manifest=manifest,
        signature=key.sign(canonical(manifest)).hex(),
        public_key=str(manifest["public_key"]),
    )


# --------------------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------------------


def check_offboardable(session: Session, tenant: Tenant, *, force: bool) -> list[str]:
    """Everything that should stop an offboarding, gathered before anything is written.

    All of them are overridable with `--force` except an already-tombstoned tenant, which
    is not a judgement call: signing a second artifact over the same chain would leave two
    valid signatures and no way to tell which one the tenant left under.
    """
    if tenant.offboarded_at is not None:
        raise OffboardError(
            f"{tenant.tenant_id} was already offboarded at {tenant.offboarded_at.isoformat()} "
            f"— artifact {tenant.offboard_artifact_key}"
        )

    warnings: list[str] = []

    open_claims = (
        session.execute(
            select(Claim.claim_id, Claim.state).where(
                Claim.tenant_id == tenant.tenant_id,
                Claim.state.notin_(tuple(TERMINAL_STATES)),
            )
        )
        .tuples()
        .all()
    )
    if open_claims:
        states = sorted({state for _, state in open_claims})
        warnings.append(f"{len(open_claims)} claim(s) still in flight: {', '.join(states)}")

    chain = verify_chain(session, tenant.tenant_id)
    if not chain["ok"]:
        warnings.append(
            f"ledger chain is broken at sequence {chain['broken_at']}: {chain['detail']}"
        )

    if warnings and not force:
        raise OffboardError(
            "refusing to offboard:\n  - " + "\n  - ".join(warnings) + "\nRe-run with --force "
            "to proceed anyway; the manifest records the chain's state either way."
        )
    return warnings


# --------------------------------------------------------------------------------------
# Archive
# --------------------------------------------------------------------------------------


def archive(artifact: Artifact, *, bucket: str) -> None:
    """Put the body and its manifest into cold storage.

    A bucket of its own, separate from the documents. The document bucket is on the hot
    path of every extraction run and its lifecycle rules are written for that; an archive
    that has to outlive the tenant by five years wants different rules and a different
    blast radius.
    """
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)

    signed = {**artifact.manifest, "signature": artifact.signature}
    client.put_object(
        Bucket=bucket,
        Key=artifact.key,
        Body=artifact.body,
        ContentType="application/x-ndjson",
        Metadata={"sha256": str(artifact.manifest["body_sha256"])},
    )
    client.put_object(
        Bucket=bucket,
        Key=f"{artifact.key}.manifest.json",
        Body=json.dumps(signed, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def tombstone(session: Session, tenant: Tenant, artifact: Artifact, *, now: datetime) -> None:
    """Mark the tenant gone. The row stays; the tenant stops being resolvable."""
    tenant.offboarded_at = now
    tenant.offboard_artifact_key = artifact.key
    tenant.offboard_signature = artifact.signature
    tenant.offboard_public_key = artifact.public_key
    session.flush()


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def offboard(
    session: Session,
    tenant_id: UUID,
    *,
    key: Ed25519PrivateKey,
    bucket: str,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> Artifact:
    """The whole procedure. Returns the artifact whether or not it was written."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise OffboardError(f"no tenant {tenant_id}")

    warnings = check_offboardable(session, tenant, force=force)
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)

    artifact = build_artifact(session, tenant, key=key, now=now or datetime.now(UTC))
    if dry_run:
        return artifact

    # Archive before tombstone, deliberately. A failed upload leaves a live tenant and no
    # artifact, which is recoverable by running this again; the other order leaves an
    # invisible tenant whose ledger was never exported, which is the state the whole
    # mechanism exists to prevent.
    archive(artifact, bucket=bucket)
    tombstone(session, tenant, artifact, now=now or datetime.now(UTC))
    return artifact


def _owner_engine() -> Any:
    """The owner DSN, not the app role's.

    `DRAWBRIDGE_DATABASE_URL` points at `drawbridge_app` in the deployed stack, and this
    script has to read a tenant it is about to make invisible. `DRAWBRIDGE_OWNER_DATABASE_URL`
    overrides it for exactly that reason.
    """
    url = os.environ.get("DRAWBRIDGE_OWNER_DATABASE_URL") or get_settings().sync_database_url
    return create_engine(url.replace("+asyncpg", "+psycopg"), pool_pre_ping=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tenant", help="Tenant UUID to offboard")
    parser.add_argument(
        "--bucket", default=get_settings().s3_bucket_archive, help="Cold-storage bucket"
    )
    parser.add_argument("--dry-run", action="store_true", help="Build and sign; write nothing")
    parser.add_argument("--force", action="store_true", help="Proceed despite open claims")
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="Print a fresh Ed25519 seed for the secret manager and exit",
    )
    args = parser.parse_args(argv)

    if args.generate_key:
        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes_raw().hex()
        print(f"{SIGNING_KEY_ENV}={seed}")
        print(f"public_key={public_key_hex(private.public_key())}", file=sys.stderr)
        return 0

    if not args.tenant:
        parser.error("--tenant is required unless --generate-key is given")

    try:
        key = load_signing_key()
        engine = _owner_engine()
        with Session(engine) as session:
            # An advisory lock on the tenant, so two operators offboarding the same tenant
            # cannot both read a live row and both sign a chain.
            tenant_id = UUID(args.tenant)
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": tenant_id.int % 2**63},
            )
            artifact = offboard(
                session,
                tenant_id,
                key=key,
                bucket=args.bucket,
                dry_run=args.dry_run,
                force=args.force,
            )
            if args.dry_run:
                session.rollback()
                print(json.dumps(artifact.manifest, indent=2, sort_keys=True))
                print("dry run — nothing written", file=sys.stderr)
                return 0
            session.commit()
    except OffboardError as exc:
        print(f"offboard failed: {exc}", file=sys.stderr)
        return 1

    print(f"archived {artifact.manifest['entries']} ledger entries to {artifact.key}")
    print(f"signature {artifact.signature[:32]}... key {artifact.public_key[:16]}...")
    print(f"tenant {args.tenant} tombstoned; rows retained, no longer resolvable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

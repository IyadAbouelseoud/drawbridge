"""Writing to the append-only ledger.

One function does the writing (`record`) and one checks the result (`verify_chain`).
Everything that changes a claim, reads a figure off a document, or overrides a machine
output calls the first; `mcp-ledger` exposes the second.

Why a chain rather than a table with a timestamp: the recordkeeping obligation in
19 CFR §163 and GCC Art. 175 is not "keep a log", it is to be able to produce the records
supporting a claim when asked, years later, to someone who has no reason to take our word
for their completeness. A log answers "what do you say happened". A chain lets them check
that the answer has not changed since.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import select, text

from services.api.src.models import AuditLedger
from services.api.src.telemetry import current_trace_id

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Event vocabulary. Mirrors the CHECK constraint in migration f7a3c9d2e814 — a value
# added here without adding it there is rejected by the database, which is the right way
# round: the constraint is the record's contract, this tuple is a convenience.
EVENT_TYPES = (
    "document_ingested",
    "extraction_run",
    "figure_traced",
    "claim_persisted",
    "claim_transition",
    "review_opened",
    "review_resolved",
    "valuation_override",
    "packet_built",
)


class LedgerError(RuntimeError):
    """A ledger write or verification that must not be swallowed."""


def _digest(
    *,
    tenant_id: UUID,
    claim_id: UUID | None,
    event_type: str,
    actor: str,
    subject: str | None,
    document_sha256: str | None,
    payload: dict[str, Any],
    prev_hash: str | None,
) -> str:
    """SHA-256 over the row's content plus the previous row's hash.

    Serialised with sorted keys and no whitespace so the digest depends on the content
    and not on how Python happened to order a dict. `recorded_at` and `sequence` are
    excluded because the database assigns them after the hash is computed; `sequence`
    orders the chain and the chain authenticates the content, which are different jobs.
    """
    material = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "claim_id": str(claim_id) if claim_id else None,
            "event_type": event_type,
            "actor": actor,
            "subject": subject,
            "document_sha256": document_sha256,
            "payload": payload,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _lock_key(tenant_id: UUID) -> int:
    """A stable 63-bit advisory-lock key for one tenant.

    Serialising the read-then-write of `prev_hash` per tenant. Without it two concurrent
    events read the same tail and produce a fork — two rows claiming the same predecessor
    — which `verify_chain` would report as tampering when it is only a race. The lock is
    transaction-scoped and per tenant, so it costs nothing across tenants and the
    pipeline's parallel claims do not queue behind each other.
    """
    return tenant_id.int % (2**63)


def record(
    session: Session,
    *,
    tenant_id: UUID,
    event_type: str,
    actor: str,
    claim_id: UUID | None = None,
    subject: str | None = None,
    document_sha256: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLedger:
    """Append one event. There is no update and no delete — see the migration.

    Raises rather than returning a failure value. A silent ledger write failure would
    leave the state change recorded and the reason for it not, which is the exact shape
    of the gap an audit is looking for.
    """
    if event_type not in EVENT_TYPES:
        msg = f"unknown ledger event type {event_type!r}; expected one of {list(EVENT_TYPES)}"
        raise LedgerError(msg)

    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(tenant_id)})

    prev_hash = session.execute(
        select(AuditLedger.entry_hash)
        .where(AuditLedger.tenant_id == tenant_id)
        .order_by(AuditLedger.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()

    body = payload or {}
    row = AuditLedger(
        ledger_id=uuid4(),
        tenant_id=tenant_id,
        claim_id=claim_id,
        event_type=event_type,
        actor=actor,
        subject=subject,
        document_sha256=document_sha256,
        payload=body,
        prev_hash=prev_hash,
        # Stamped, not hashed — see the column's note on the model. This is what ties a
        # row an auditor is reading four years from now to the run that wrote it.
        trace_id=current_trace_id(),
        entry_hash=_digest(
            tenant_id=tenant_id,
            claim_id=claim_id,
            event_type=event_type,
            actor=actor,
            subject=subject,
            document_sha256=document_sha256,
            payload=body,
            prev_hash=prev_hash,
        ),
    )
    session.add(row)
    session.flush()
    return row


def verify_chain(session: Session, tenant_id: UUID) -> dict[str, Any]:
    """Recompute every hash in a tenant's chain and report the first break.

    Reports rather than raises: a broken chain is a finding to be investigated, not an
    exception to be caught by whatever happened to call this. The `sequence` of the first
    bad row is what an investigation starts from, so it is returned even though the
    boolean is what most callers check.
    """
    rows = (
        session.execute(
            select(AuditLedger)
            .where(AuditLedger.tenant_id == tenant_id)
            .order_by(AuditLedger.sequence)
        )
        .scalars()
        .all()
    )

    expected_prev: str | None = None
    for row in rows:
        recomputed = _digest(
            tenant_id=row.tenant_id,
            claim_id=row.claim_id,
            event_type=row.event_type,
            actor=row.actor,
            subject=row.subject,
            document_sha256=row.document_sha256,
            payload=row.payload or {},
            prev_hash=row.prev_hash,
        )
        if row.prev_hash != expected_prev:
            return {
                "ok": False,
                "entries": len(rows),
                "broken_at": row.sequence,
                "detail": "prev_hash does not match the preceding entry — a row is missing",
            }
        if recomputed != row.entry_hash:
            return {
                "ok": False,
                "entries": len(rows),
                "broken_at": row.sequence,
                "detail": "entry_hash does not match the row content — a row was altered",
            }
        expected_prev = row.entry_hash

    return {"ok": True, "entries": len(rows), "broken_at": None, "detail": "chain intact"}


def entries_for_claim(session: Session, claim_id: UUID) -> list[AuditLedger]:
    """Every ledger row touching one claim, oldest first."""
    return list(
        session.execute(
            select(AuditLedger)
            .where(AuditLedger.claim_id == claim_id)
            .order_by(AuditLedger.sequence)
        )
        .scalars()
        .all()
    )

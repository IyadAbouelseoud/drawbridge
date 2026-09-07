"""Analyst operations over the review queue and the claim state machine.

Shared by the MCP tools (`mcp_servers/mcp_claims`) and the REST routes, so an analyst
working in Claude Code and an analyst working through the UI take exactly the same code
path — and leave exactly the same audit trail. Divergence between the two would mean two
sets of rules for the same decision.

Everything here is synchronous and takes a session, because the MCP servers run their own
short-lived connections rather than sharing the API's pool.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from drawbridge_schemas.claim import ClaimState
from services.api.src.models import Claim, ClaimTransition, ReviewQueue

if TYPE_CHECKING:
    from collections.abc import Sequence

# An analyst decision that changes money must say why. This is the floor on that
# reasoning — short enough not to be obstructive, long enough that "ok" fails.
MIN_REASONING_CHARS = 20

RESOLUTIONS = frozenset({"approved", "rejected", "corrected", "deferred"})

# Resolutions that let the suspended workflow continue. `deferred` deliberately does not:
# deferring is a decision to look again later, not a decision to proceed.
RESUMING_RESOLUTIONS = frozenset({"approved", "corrected"})


class AnalystError(RuntimeError):
    """A refused analyst operation, with the reason stated for the caller."""


class InsufficientReasoningError(AnalystError):
    """Audit reasoning missing or too thin.

    Refused rather than defaulted. A decision that moves a customs figure without a
    recorded reason is indistinguishable from an unexamined one four years later, which
    is exactly when it will be asked about.
    """


def _require_reasoning(reasoning: str, action: str) -> str:
    text = (reasoning or "").strip()
    if len(text) < MIN_REASONING_CHARS:
        msg = (
            f"{action} requires audit reasoning of at least {MIN_REASONING_CHARS} "
            f"characters; got {len(text)}. State what you verified and against which "
            "document — this text is the audit record."
        )
        raise InsufficientReasoningError(msg)
    return text


# --------------------------------------------------------------------------------- read


def list_queue(
    session: Session,
    *,
    tenant_id: UUID | None = None,
    state: str = "open",
    severity: str | None = None,
    reason: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Open exceptions, most urgent first.

    Ordered by severity then age, not age alone: a blocking item raised this morning
    outranks a low-severity item from last week.
    """
    stmt = select(ReviewQueue).where(ReviewQueue.state == state)
    if tenant_id is not None:
        stmt = stmt.where(ReviewQueue.tenant_id == tenant_id)
    if severity is not None:
        stmt = stmt.where(ReviewQueue.severity == severity)
    if reason is not None:
        stmt = stmt.where(ReviewQueue.reason == reason)

    rows = session.execute(stmt.limit(500)).scalars().all()
    order = {"blocking": 0, "high": 1, "normal": 2, "low": 3}
    rows = sorted(rows, key=lambda r: (order.get(r.severity, 9), r.created_at))[:limit]
    return [_queue_row(r) for r in rows]


def get_exception(session: Session, review_id: UUID) -> dict[str, Any]:
    row = session.get(ReviewQueue, review_id)
    if row is None:
        msg = f"no review queue row {review_id}"
        raise AnalystError(msg)
    return _queue_row(row, include_payload=True)


def _queue_row(row: ReviewQueue, *, include_payload: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "review_id": str(row.review_id),
        "tenant_id": str(row.tenant_id),
        "claim_id": str(row.claim_id) if row.claim_id else None,
        "reason": row.reason,
        "severity": row.severity,
        "summary": row.summary,
        "citation": row.citation,
        "state": row.state,
        "resolution": row.resolution,
        "assigned_to": row.assigned_to,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resume_token": row.resume_token,
        "workflow_run_id": row.workflow_run_id,
    }
    if include_payload:
        out["payload"] = row.payload
        out["resolution_note"] = row.resolution_note
    return out


# -------------------------------------------------------------------------------- write


def resolve_exception(
    session: Session,
    *,
    review_id: UUID,
    resolution: str,
    reasoning: str,
    analyst: str,
) -> dict[str, Any]:
    """Record an analyst's decision on one queued exception.

    Reasoning is mandatory and enforced here rather than at the edge, so the MCP tool and
    the REST route cannot diverge on it.
    """
    if resolution not in RESOLUTIONS:
        msg = f"resolution must be one of {sorted(RESOLUTIONS)}; got {resolution!r}"
        raise AnalystError(msg)
    note = _require_reasoning(reasoning, "resolve_exception")

    row = session.get(ReviewQueue, review_id)
    if row is None:
        msg = f"no review queue row {review_id}"
        raise AnalystError(msg)
    if row.state == "resolved":
        msg = (
            f"review {review_id} was already resolved as {row.resolution!r} by "
            f"{row.assigned_to!r}; reopen it rather than resolving twice"
        )
        raise AnalystError(msg)

    row.state = "resolved"
    row.resolution = resolution
    row.resolution_note = note
    row.assigned_to = analyst
    row.resolved_at = datetime.now(UTC)

    # The workflow may resume only once every exception on the claim has been decided.
    # Resolving one reason does not resolve the others — that is why suspend writes one
    # row per reason.
    outstanding = _outstanding_for_claim(session, row.claim_id) if row.claim_id else 0

    session.flush()
    return {
        "review_id": str(review_id),
        "claim_id": str(row.claim_id) if row.claim_id else None,
        "resolution": resolution,
        "resume_token": row.resume_token,
        "workflow_run_id": row.workflow_run_id,
        "may_resume": resolution in RESUMING_RESOLUTIONS and outstanding == 0,
        "outstanding_exceptions": outstanding,
        "analyst": analyst,
    }


def _outstanding_for_claim(session: Session, claim_id: UUID | None) -> int:
    if claim_id is None:
        return 0
    rows = (
        session.execute(
            select(ReviewQueue.review_id).where(
                ReviewQueue.claim_id == claim_id, ReviewQueue.state != "resolved"
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


def override_valuation(
    session: Session,
    *,
    review_id: UUID,
    corrected_value: Decimal,
    currency: str,
    valuation_basis: str,
    reasoning: str,
    analyst: str,
) -> dict[str, Any]:
    """Correct a declared value an analyst has verified against source documents.

    The commonest reason a GCC claim sits in the queue is a near miss on the Art. 16 §2
    threshold, and the commonest cause is a figure captured on the wrong valuation basis
    — a CIF number where Art. 28 wants declared value plus costs to the customs office
    (docs/COMPLIANCE-GCC.md §8.1).

    The override does not re-run the match. It records the corrected figure and its basis
    on the exception, resolves it as `corrected`, and lets the workflow re-run matching
    with the corrected input. Silently patching a refund figure without re-deriving it
    would break the provenance chain that makes the claim defensible.
    """
    note = _require_reasoning(reasoning, "override_valuation")
    if corrected_value <= 0:
        msg = f"corrected value must be positive; got {corrected_value}"
        raise AnalystError(msg)

    row = session.get(ReviewQueue, review_id)
    if row is None:
        msg = f"no review queue row {review_id}"
        raise AnalystError(msg)
    if row.state == "resolved":
        msg = f"review {review_id} is already resolved as {row.resolution!r}"
        raise AnalystError(msg)

    payload = dict(row.payload or {})
    payload["valuation_override"] = {
        "corrected_value": str(corrected_value),
        "currency": currency,
        "valuation_basis": valuation_basis,
        "analyst": analyst,
        "at": datetime.now(UTC).isoformat(),
        "citation": "GCC Common Customs Law Art. 28; Rules of Implementation Art. 16 §2",
    }
    row.payload = payload
    row.state = "resolved"
    row.resolution = "corrected"
    row.resolution_note = note
    row.assigned_to = analyst
    row.resolved_at = datetime.now(UTC)
    session.flush()

    return {
        "review_id": str(review_id),
        "claim_id": str(row.claim_id) if row.claim_id else None,
        "corrected_value": str(corrected_value),
        "currency": currency,
        "valuation_basis": valuation_basis,
        "resolution": "corrected",
        "requires_rematch": True,
        "resume_token": row.resume_token,
        "workflow_run_id": row.workflow_run_id,
    }


def approve_claim(
    session: Session,
    *,
    claim_id: UUID,
    reasoning: str,
    analyst: str,
) -> dict[str, Any]:
    """Move a claim from ANALYST_REVIEW to APPROVED.

    Refuses while any exception on the claim is still open. The state machine already
    forbids reaching APPROVED without passing through ANALYST_REVIEW; this adds the
    complementary guard, that review actually happened rather than merely being entered.
    """
    note = _require_reasoning(reasoning, "approve_claim")

    claim = session.get(Claim, claim_id)
    if claim is None:
        msg = f"no claim {claim_id}"
        raise AnalystError(msg)

    current = ClaimState(claim.state)
    if not current.can_move_to(ClaimState.APPROVED):
        msg = (
            f"claim {claim_id} is in {current}, which cannot move to approved. "
            "A claim reaches approval only from analyst_review."
        )
        raise AnalystError(msg)

    outstanding = _outstanding_for_claim(session, claim_id)
    if outstanding:
        msg = (
            f"claim {claim_id} still has {outstanding} unresolved exception(s); "
            "resolve them before approving"
        )
        raise AnalystError(msg)

    claim.state = ClaimState.APPROVED.value
    session.add(
        ClaimTransition(
            transition_id=uuid4(),
            claim_id=claim_id,
            from_state=current.value,
            to_state=ClaimState.APPROVED.value,
            actor=analyst,
            reason=note,
        )
    )
    session.flush()

    return {
        "claim_id": str(claim_id),
        "from_state": current.value,
        "state": ClaimState.APPROVED.value,
        "analyst": analyst,
        "total_refund": str(claim.total_refund),
        "currency": claim.currency,
    }


def transition_claim(
    session: Session,
    *,
    claim_id: UUID,
    to_state: str,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Move a claim to an arbitrary permitted state, recording the transition.

    Validated against the same transition table the schema package uses, so the database
    and the domain model cannot drift apart on what is reachable from where.
    """
    claim = session.get(Claim, claim_id)
    if claim is None:
        msg = f"no claim {claim_id}"
        raise AnalystError(msg)

    try:
        target = ClaimState(to_state)
    except ValueError as exc:
        msg = f"unknown claim state {to_state!r}"
        raise AnalystError(msg) from exc

    current = ClaimState(claim.state)
    if not current.can_move_to(target):
        msg = f"{current} cannot move to {target}"
        raise AnalystError(msg)

    claim.state = target.value
    session.add(
        ClaimTransition(
            transition_id=uuid4(),
            claim_id=claim_id,
            from_state=current.value,
            to_state=target.value,
            actor=actor,
            reason=reason,
        )
    )
    session.flush()
    return {"claim_id": str(claim_id), "from_state": current.value, "state": target.value}


def claim_history(session: Session, claim_id: UUID) -> list[dict[str, Any]]:
    """Every state transition on a claim, oldest first.

    Append-only: this is the record that answers an audit years later, so nothing here
    updates or deletes.
    """
    rows: Sequence[ClaimTransition] = (
        session.execute(
            select(ClaimTransition)
            .where(ClaimTransition.claim_id == claim_id)
            .order_by(ClaimTransition.occurred_at)
        )
        .scalars()
        .all()
    )
    return [
        {
            "transition_id": str(r.transition_id),
            "from_state": r.from_state,
            "to_state": r.to_state,
            "actor": r.actor,
            "reason": r.reason,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        }
        for r in rows
    ]


def claim_summary(session: Session, claim_id: UUID) -> dict[str, Any]:
    claim = session.get(Claim, claim_id)
    if claim is None:
        msg = f"no claim {claim_id}"
        raise AnalystError(msg)
    return {
        "claim_id": str(claim.claim_id),
        "tenant_id": str(claim.tenant_id),
        "state": claim.state,
        "jurisdiction": claim.jurisdiction,
        "currency": claim.currency,
        "lane": claim.lane,
        "drawback_type": claim.drawback_type,
        "period_start": claim.period_start.isoformat(),
        "period_end": claim.period_end.isoformat(),
        "filing_deadline": claim.filing_deadline.isoformat(),
        "absolute_bar_date": (
            claim.absolute_bar_date.isoformat() if claim.absolute_bar_date else None
        ),
        "total_refund": str(claim.total_refund),
        "open_exceptions": _outstanding_for_claim(session, claim.claim_id),
    }


def reopen_exception(
    session: Session, *, review_id: UUID, reasoning: str, analyst: str
) -> dict[str, Any]:
    """Reopen a resolved exception.

    The resolution history is not erased — `resolution_note` accumulates rather than
    being overwritten, because "this was approved then reopened" is materially different
    from "this was always open" and an auditor will want to see which happened.
    """
    note = _require_reasoning(reasoning, "reopen_exception")
    row = session.get(ReviewQueue, review_id)
    if row is None:
        msg = f"no review queue row {review_id}"
        raise AnalystError(msg)
    if row.state != "resolved":
        msg = f"review {review_id} is {row.state}, not resolved"
        raise AnalystError(msg)

    prior = row.resolution
    row.resolution_note = f"{row.resolution_note or ''}\n[reopened by {analyst}: {note}]".strip()
    session.execute(
        update(ReviewQueue)
        .where(ReviewQueue.review_id == review_id)
        .values(
            state="open",
            resolution=None,
            resolved_at=None,
            resolution_note=row.resolution_note,
        )
    )
    session.flush()
    return {"review_id": str(review_id), "state": "open", "prior_resolution": prior}

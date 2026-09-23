"""Analyst operations over the review queue and the claim state machine.

Shared by the MCP tools (`mcp_servers/mcp_claims`) and the REST routes, so an analyst
working in Claude Code and an analyst working through the UI take exactly the same code
path — and leave exactly the same audit trail. Divergence between the two would mean two
sets of rules for the same decision.

Everything here is synchronous and takes a session, because the MCP servers run their own
short-lived connections rather than sharing the API's pool.

**v1.1.0: who is deciding.** Every write here asks `gates.current_actor()` who is acting —
the verified token subject on a network path — and records *that*, not the `analyst` or
`actor` argument. The argument survives for in-process callers (CLI, tests) that have no
principal. Each decision then passes the gate in `services/api/src/gates.py`, so the
REST route, the MCP tool and the n8n node that reach the same function meet the same rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from drawbridge_schemas.agents import Scope
from drawbridge_schemas.claim import ClaimState
from services.api.src import gates
from services.api.src.ledger import record
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


def _review_facts(session: Session, claim_id: UUID | None) -> list[gates.ReviewFact]:
    if claim_id is None:
        return []
    rows = session.execute(
        select(
            ReviewQueue.reason,
            ReviewQueue.state,
            ReviewQueue.resolution,
            ReviewQueue.assigned_to,
        ).where(ReviewQueue.claim_id == claim_id)
    ).all()
    return [
        gates.ReviewFact(reason=r[0], state=r[1], resolution=r[2], resolved_by=r[3]) for r in rows
    ]


def _who(actor: gates.Actor) -> dict[str, Any]:
    """The acting principal, as a ledger payload records it."""
    return {"actor_kind": actor.kind.value, "agent_id": actor.agent_id}


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

    # Ordered and limited in the database. This used to fetch up to 500 rows, sort them
    # in Python and keep `limit` — so a queue deeper than 500 could silently omit its most
    # urgent row, and every call paid for 500 rows to return 50.
    rank = case(
        (ReviewQueue.severity == "blocking", 0),
        (ReviewQueue.severity == "high", 1),
        (ReviewQueue.severity == "normal", 2),
        (ReviewQueue.severity == "low", 3),
        else_=9,
    )
    rows = session.execute(stmt.order_by(rank, ReviewQueue.created_at).limit(limit)).scalars()
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
    # Advertised on every row, not just the detailed one, so an analyst scanning the
    # queue can see which items already have pre-analysis waiting.
    out["has_agent_memo"] = row.agent_memo is not None
    if include_payload:
        out["payload"] = row.payload
        out["resolution_note"] = row.resolution_note
        out["agent_memo"] = row.agent_memo
        out["agent_model"] = row.agent_model
        out["agent_drafted_at"] = row.agent_drafted_at.isoformat() if row.agent_drafted_at else None
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
    actor = gates.current_actor(analyst)

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

    siblings = [
        fact
        for fact in _review_facts(session, row.claim_id)
        if not (fact.reason == row.reason and fact.state != "resolved")
    ]
    gates.check_resolution(actor, reason=row.reason, resolution=resolution, siblings=siblings)
    analyst = actor.name

    row.state = "resolved"
    row.resolution = resolution
    row.resolution_note = note
    row.assigned_to = analyst
    row.resolved_at = datetime.now(UTC)

    # The workflow may resume only once every exception on the claim has been decided.
    # Resolving one reason does not resolve the others — that is why suspend writes one
    # row per reason.
    outstanding = _outstanding_for_claim(session, row.claim_id) if row.claim_id else 0

    # A human decided something a machine could not. The reasoning is recorded verbatim
    # because it is the decision — a resolution code without it is indistinguishable
    # from a row someone clicked through.
    record(
        session,
        tenant_id=row.tenant_id,
        claim_id=row.claim_id,
        event_type="review_resolved",
        actor=analyst,
        subject=row.reason,
        payload={
            "review_id": str(review_id),
            "resolution": resolution,
            "reasoning": note,
            "outstanding_after": outstanding,
            **_who(actor),
        },
    )

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
    return int(
        session.execute(
            select(func.count()).where(
                ReviewQueue.claim_id == claim_id, ReviewQueue.state != "resolved"
            )
        ).scalar_one()
    )


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
    actor = gates.current_actor(analyst)
    gates.require_human(actor, Scope.VALUATION_OVERRIDE, "overriding a declared valuation")
    analyst = actor.name
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

    # The manual override the recordkeeping rules care about most: a human replaced a
    # figure the machine extracted. Both figures go in the ledger, so the trace shows
    # what was read off the document and what was filed instead of it.
    record(
        session,
        tenant_id=row.tenant_id,
        claim_id=row.claim_id,
        event_type="valuation_override",
        actor=analyst,
        subject="declared_value",
        payload={
            "review_id": str(review_id),
            "corrected_value": str(corrected_value),
            "currency": currency,
            "valuation_basis": valuation_basis,
            "reasoning": note,
            "citation": "GCC Common Customs Law Art. 28; Rules of Implementation Art. 16 §2",
            **_who(actor),
        },
    )
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

    Refuses while any exception on the claim is still open — the same guard
    `transition_claim` applies, repeated here because this path also demands a reasoning
    note and should fail on the cheaper condition first.

    This is the analyst's route specifically: it requires ANALYST_REVIEW as the origin,
    where `transition_claim` also permits the automated QUANTIFIED -> APPROVED lane. A
    claim that stopped for a human is approved by a human.
    """
    note = _require_reasoning(reasoning, "approve_claim")
    actor = gates.current_actor(analyst)
    if actor.kind is gates.ActorKind.MACHINE:
        # The analyst's route specifically. The pipeline reaches APPROVED through
        # `transition_claim` and its own gate; it does not get to call itself an analyst.
        gates.require_human(actor, Scope.CLAIMS_APPROVE, "approving a claim for review")
    analyst = actor.name

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

    gates.check_transition(
        actor,
        current=current,
        target=ClaimState.APPROVED,
        amount=claim.total_refund,
        currency=claim.currency,
        reviews=_review_facts(session, claim_id),
    )

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
    # Until v1.1.0 an analyst's approval reached `claim_transitions` and not the ledger,
    # so the one transition most worth auditing was the one the hash chain did not cover.
    record(
        session,
        tenant_id=claim.tenant_id,
        claim_id=claim_id,
        event_type="claim_transition",
        actor=analyst,
        subject=ClaimState.APPROVED.value,
        payload={
            "from_state": current.value,
            "to_state": ClaimState.APPROVED.value,
            "reason": note,
            "via": "approve_claim",
            **_who(actor),
        },
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

    who = gates.current_actor(actor)
    actor = who.name

    # The guard that used to be the state machine's. Since week 9 a clean claim may reach
    # APPROVED straight from QUANTIFIED without a human, so "approval implies review
    # happened" is no longer structural and has to be checked here — for every caller,
    # not only for `approve_claim`, because the pipeline is now one of the callers.
    if target is ClaimState.APPROVED:
        outstanding = _outstanding_for_claim(session, claim_id)
        if outstanding:
            msg = (
                f"claim {claim_id} still has {outstanding} unresolved exception(s); "
                "it cannot be approved until they are resolved"
            )
            raise AnalystError(msg)

    gates.check_transition(
        who,
        current=current,
        target=target,
        amount=claim.total_refund,
        currency=claim.currency,
        reviews=_review_facts(session, claim_id),
    )

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
    # The transition row is the state machine's record; the ledger row is the audit's.
    # Both, rather than one: `claim_transitions` is scoped to a claim and answers "how
    # did this claim get here", where the ledger is scoped to a tenant and answers "what
    # was done, in what order" across claims, documents, and analysts alike.
    record(
        session,
        tenant_id=claim.tenant_id,
        claim_id=claim_id,
        event_type="claim_transition",
        actor=actor,
        subject=target.value,
        payload={
            "from_state": current.value,
            "to_state": target.value,
            "reason": reason,
            **_who(who),
        },
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
    actor = gates.current_actor(analyst)
    gates.require_human(actor, Scope.REVIEW_RESOLVE, "reopening an exception")
    analyst = actor.name
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
    # Reopening un-decides something a claim may already have moved on. Until v1.1.0 it
    # was recorded only by appending to a free-text column the ledger does not cover.
    record(
        session,
        tenant_id=row.tenant_id,
        claim_id=row.claim_id,
        event_type="review_reopened",
        actor=analyst,
        subject=row.reason,
        payload={
            "review_id": str(review_id),
            "prior_resolution": prior,
            "reasoning": note,
            **_who(actor),
        },
    )
    session.flush()
    return {"review_id": str(review_id), "state": "open", "prior_resolution": prior}

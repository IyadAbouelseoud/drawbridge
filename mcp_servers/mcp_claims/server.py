"""Claim assembly, validation, state transitions — and the analyst's working surface.

This is the (b) case from docs/ARCHITECTURE.md §4: the ~15% of claims the pipeline
cannot close alone get closed by a human trade analyst working through these tools in
Claude Code. They share `services/api/src/analyst.py` with the REST routes, so an
analyst working here and one working through the UI take the same code path and leave
the same audit trail.

Two things are enforced rather than encouraged:

- **Reasoning is mandatory** on any decision that moves money. A resolution recorded
  without a reason is indistinguishable from an unexamined one four years later, which
  is exactly when CBP or ZATCA will ask about it.
- **Decisions commit before notifications.** The analyst's decision is the durable fact;
  waking the workflow is disposable. A failed webhook must never roll back a recorded
  decision.

Exposed over streamable-HTTP on port 8104. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_servers.mcp_claims.db import session_scope
from services.agent.src.client import AgentRefusedError, AgentUnavailableError
from services.agent.src.grounding import UngroundedFigureError
from services.agent.src.queue import draft_one
from services.api.src.analyst import (
    AnalystError,
    approve_claim,
    claim_history,
    claim_summary,
    get_exception,
    list_queue,
    override_valuation,
    reopen_exception,
    resolve_exception,
    transition_claim,
)
from services.api.src.resume import resume_workflow

server = MCPServer("mcp-claims")


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        msg = f"{field} is not a UUID: {value!r}"
        raise AnalystError(msg) from exc


def _fail(exc: Exception) -> dict[str, Any]:
    """Return refusals as data rather than raising.

    An analyst asking a question in Claude Code should get the reason back in the
    conversation, not a stack trace that ends the turn.
    """
    return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}


@server.tool()
def ping() -> str:
    """Liveness probe."""
    return "mcp-claims ok"


@server.tool()
def list_review_queue(
    tenant_id: Annotated[str | None, Field(description="Tenant UUID; omit for all")] = None,
    state: Annotated[str, Field(description="open | claimed | resolved")] = "open",
    severity: Annotated[str | None, Field(description="low|normal|high|blocking")] = None,
    reason: Annotated[str | None, Field(description="Filter by review reason")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Exceptions awaiting an analyst, most urgent first.

    Ordered by severity then age rather than age alone: a blocking item raised this
    morning outranks a low-severity item from last week, because the thing that kills a
    claim is a deadline, not a queue position.
    """
    try:
        with session_scope() as session:
            items = list_queue(
                session,
                tenant_id=_uuid(tenant_id, "tenant_id") if tenant_id else None,
                state=state,
                severity=severity,
                reason=reason,
                limit=limit,
            )
        return {"ok": True, "count": len(items), "items": items}
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


@server.tool()
def inspect_exception(
    review_id: Annotated[str, Field(description="Review queue row UUID")],
) -> dict[str, Any]:
    """One exception in full, including the matcher payload that produced it.

    The payload carries the rejections and solver metadata verbatim, so the provisions
    that failed and the figures involved are visible without re-running the match.

    `agent_memo`, when present, is pre-analysis drafted before you opened this row. It is
    advisory: it says what the drafter would do and what it could not settle, and it has
    moved nothing. Every figure in it was checked against the payload above — the drafter
    cannot originate a number — but its *judgment* is unverified, and `blocking_unknowns`
    is the part worth reading first.
    """
    try:
        with session_scope() as session:
            return {"ok": True, "exception": get_exception(session, _uuid(review_id, "review_id"))}
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


@server.tool()
def draft_exception_memo(
    review_id: Annotated[str, Field(description="Review queue row UUID")],
    overwrite: Annotated[
        bool, Field(description="Redraft even if a memo is already attached")
    ] = False,
) -> dict[str, Any]:
    """Draft pre-analysis for one exception now, rather than waiting for the worker.

    Useful when you have just opened a fresh exception and want the summary before
    reading the payload. Returns the memo without resolving anything — `recommendation`
    is a suggestion, and this claim does not move until you call
    `resolve_review_exception` yourself.

    Refuses to overwrite an existing memo unless asked. A memo an analyst has already
    read is part of the record of how the decision was reached, and silently replacing it
    would make that record unreconstructable.
    """
    try:
        with session_scope() as session:
            return {
                "ok": True,
                **draft_one(session, _uuid(review_id, "review_id"), overwrite=overwrite),
            }
    except (
        AnalystError,
        AgentUnavailableError,
        AgentRefusedError,
        UngroundedFigureError,
        ValueError,
    ) as exc:
        return _fail(exc)


@server.tool()
def resolve_review_exception(
    review_id: Annotated[str, Field(description="Review queue row UUID")],
    resolution: Annotated[str, Field(description="approved | rejected | corrected | deferred")],
    reasoning: Annotated[
        str,
        Field(
            description=(
                "Audit reasoning, at least 20 characters. State what you verified and "
                "against which document — this text becomes the audit record."
            )
        ),
    ],
    analyst: Annotated[str, Field(description="Analyst identifier")],
) -> dict[str, Any]:
    """Record a decision on one exception and wake the workflow if nothing else blocks it.

    The workflow resumes only once *every* exception on the claim is decided. Suspend
    writes one row per reason precisely so resolving a threshold near miss does not
    silently clear a low-confidence extraction sitting behind it.

    `deferred` does not resume: deferring is a decision to look again later.
    """
    try:
        with session_scope() as session:
            outcome = resolve_exception(
                session,
                review_id=_uuid(review_id, "review_id"),
                resolution=resolution,
                reasoning=reasoning,
                analyst=analyst,
            )
    except (AnalystError, ValueError) as exc:
        return _fail(exc)

    # Committed. Notification is best-effort from here.
    resumed = None
    if outcome["may_resume"]:
        resumed = resume_workflow(
            workflow_run_id=outcome["workflow_run_id"],
            resume_token=outcome["resume_token"],
            payload={
                "resolution": outcome["resolution"],
                "claim_id": outcome["claim_id"],
                "analyst": analyst,
            },
        ).as_dict()

    return {"ok": True, **outcome, "resume": resumed}


@server.tool()
def override_declared_valuation(
    review_id: Annotated[str, Field(description="Review queue row UUID")],
    corrected_value: Annotated[str, Field(description="Corrected value, decimal string")],
    currency: Annotated[str, Field(description="Currency of the corrected value")],
    valuation_basis: Annotated[
        str,
        Field(
            description=(
                "art_28_export | fob | cif | unknown. The Art. 16 §2 threshold wants the "
                "Art. 28 basis: declared value plus costs to the customs office."
            )
        ),
    ],
    reasoning: Annotated[str, Field(description="Audit reasoning, min 20 characters")],
    analyst: Annotated[str, Field(description="Analyst identifier")],
) -> dict[str, Any]:
    """Correct a declared value verified against source documents.

    The commonest cause of a GCC threshold near miss is a figure captured on the wrong
    valuation basis — a CIF number where Art. 28 wants declared value plus costs to the
    customs office (docs/COMPLIANCE-GCC.md §8.1).

    This records the corrected figure and resolves the exception as `corrected`; it does
    **not** patch the refund. The claim re-runs matching with the corrected input,
    because silently rewriting a refund figure without re-deriving it would break the
    provenance chain that makes the claim defensible.
    """
    try:
        value = Decimal(corrected_value)
    except (InvalidOperation, ValueError):
        return _fail(ValueError(f"corrected_value is not a decimal: {corrected_value!r}"))

    try:
        with session_scope() as session:
            outcome = override_valuation(
                session,
                review_id=_uuid(review_id, "review_id"),
                corrected_value=value,
                currency=currency,
                valuation_basis=valuation_basis,
                reasoning=reasoning,
                analyst=analyst,
            )
        return {"ok": True, **outcome}
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


@server.tool()
def approve(
    claim_id: Annotated[str, Field(description="Claim UUID")],
    reasoning: Annotated[str, Field(description="Audit reasoning, min 20 characters")],
    analyst: Annotated[str, Field(description="Analyst identifier")],
) -> dict[str, Any]:
    """Move a claim from analyst_review to approved.

    Refused while any exception on the claim is still open. The state machine already
    forbids reaching approved without passing through analyst_review; this adds the
    complementary guard that review actually happened rather than merely being entered.
    """
    try:
        with session_scope() as session:
            return {
                "ok": True,
                **approve_claim(
                    session,
                    claim_id=_uuid(claim_id, "claim_id"),
                    reasoning=reasoning,
                    analyst=analyst,
                ),
            }
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


@server.tool()
def transition(
    claim_id: Annotated[str, Field(description="Claim UUID")],
    to_state: Annotated[str, Field(description="Target ClaimState value")],
    actor: Annotated[str, Field(description="Who is moving it")],
    reason: Annotated[str | None, Field(description="Why")] = None,
) -> dict[str, Any]:
    """Move a claim to a permitted state, recording the transition.

    Validated against the same transition table the schema package uses, so the database
    and the domain model cannot drift on what is reachable from where.
    """
    try:
        with session_scope() as session:
            return {
                "ok": True,
                **transition_claim(
                    session,
                    claim_id=_uuid(claim_id, "claim_id"),
                    to_state=to_state,
                    actor=actor,
                    reason=reason,
                ),
            }
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


@server.tool()
def describe_claim(
    claim_id: Annotated[str, Field(description="Claim UUID")],
) -> dict[str, Any]:
    """A claim's current state, deadlines and outstanding exception count."""
    try:
        with session_scope() as session:
            return {"ok": True, "claim": claim_summary(session, _uuid(claim_id, "claim_id"))}
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


@server.tool()
def claim_transitions(
    claim_id: Annotated[str, Field(description="Claim UUID")],
) -> dict[str, Any]:
    """Every state transition on a claim, oldest first. Append-only."""
    try:
        with session_scope() as session:
            history = claim_history(session, _uuid(claim_id, "claim_id"))
        return {"ok": True, "count": len(history), "transitions": history}
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


@server.tool()
def reopen(
    review_id: Annotated[str, Field(description="Review queue row UUID")],
    reasoning: Annotated[str, Field(description="Audit reasoning, min 20 characters")],
    analyst: Annotated[str, Field(description="Analyst identifier")],
) -> dict[str, Any]:
    """Reopen a resolved exception.

    The prior resolution is appended to rather than overwritten: "approved then reopened"
    is materially different from "always open", and an auditor will want to see which.
    """
    try:
        with session_scope() as session:
            return {
                "ok": True,
                **reopen_exception(
                    session,
                    review_id=_uuid(review_id, "review_id"),
                    reasoning=reasoning,
                    analyst=analyst,
                ),
            }
    except (AnalystError, ValueError) as exc:
        return _fail(exc)


def main() -> None:
    # MCP SDK 2.x takes the bind address on run(), not on the constructor.
    server.run(transport="streamable-http", host="0.0.0.0", port=8104)


if __name__ == "__main__":
    main()

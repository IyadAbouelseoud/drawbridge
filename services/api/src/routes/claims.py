"""Claim persistence and state transitions — the half of the pipeline that writes.

n8n calls `/claims/persist` once the matcher and triage have run, and `/claims/transition`
to advance the claim as each stage completes. Neither endpoint decides anything: the
target state is validated against the same transition table the schema package holds, so
n8n cannot move a claim somewhere the domain model does not allow.

`docs/ARCHITECTURE.md` §4: the Postgres state machine is authoritative and n8n holds no
business state. That is what makes a crashed workflow run recoverable — the claim is
wherever the database says, and a re-run reads it rather than reconstructing it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from drawbridge_schemas.jurisdiction import Jurisdiction
from drawbridge_schemas.trade import EntryLine, ExportLine, LineMatch
from services.api.src.analyst import AnalystError, claim_history, claim_summary, transition_claim
from services.api.src.auth import authorise_tenant
from services.api.src.persistence import PersistenceError, persist_claim
from services.api.src.sync_db import in_thread, in_thread_for_claim
from services.api.src.tenancy import TenantScopeError

router = APIRouter(prefix="/claims", tags=["claims"])


class PersistRequest(BaseModel):
    """What n8n forwards after `/matching/run` and `/triage/evaluate`.

    The lines are sent again rather than looked up by id, because at this point they may
    never have been written — the pipeline extracts, matches and only then persists, so
    the first write of a line and the first write of its claim are one transaction.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    jurisdiction: Jurisdiction
    imports: list[EntryLine] = Field(min_length=1)
    exports: list[ExportLine] = Field(min_length=1)
    matches: list[LineMatch] = Field(min_length=1)
    total_refund: Decimal
    requires_review: bool = False
    actor: str = "pipeline"


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: UUID
    to_state: str
    actor: str = "n8n"
    reason: str | None = None


class ClaimOut(BaseModel):
    claim_id: UUID
    state: str
    jurisdiction: str
    currency: str
    lane: str
    drawback_type: str | None
    period_start: date
    period_end: date
    filing_deadline: date
    absolute_bar_date: date | None
    total_refund: Decimal
    open_exceptions: int


@router.post("/persist", status_code=status.HTTP_201_CREATED)
async def persist(body: PersistRequest) -> dict[str, Any]:
    """Write the matched claim, its lines and its refund components.

    A claim carrying open exceptions is persisted in `analyst_review` rather than held
    back. Withholding it would leave the analyst reviewing a workflow variable instead of
    a claim, and nothing to point `mcp-claims` at.
    """
    # The verified tenant, not the posted one. Before this the row-level policies
    # compared each written row against a value the caller supplied.
    tenant_id = authorise_tenant(body.tenant_id)
    try:
        return await in_thread(
            lambda session: persist_claim(
                session,
                tenant_id=tenant_id,
                jurisdiction=body.jurisdiction,
                imports=body.imports,
                exports=body.exports,
                matches=body.matches,
                total_refund=body.total_refund,
                requires_review=body.requires_review,
                actor=body.actor,
            ),
            tenant_id,
        )
    except PersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "cannot_persist", "message": str(exc)},
        ) from exc


@router.post("/transition")
async def transition(body: TransitionRequest) -> dict[str, Any]:
    """Advance a claim, recording who moved it and why.

    A rejected transition is a 409 rather than a 422: the request is well-formed and the
    claim is simply not where the caller thought. That distinction matters to n8n, which
    retries one and stops on the other.
    """
    try:
        return await in_thread_for_claim(
            body.claim_id,
            lambda session: transition_claim(
                session,
                claim_id=body.claim_id,
                to_state=body.to_state,
                actor=body.actor,
                reason=body.reason,
            ),
        )
    except TenantScopeError as exc:
        # No such claim, rather than a claim in the wrong state. A 409 here would tell
        # n8n to stop and an operator to go looking for a state machine problem.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "claim_id": str(body.claim_id)},
        ) from exc
    except AnalystError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "transition_refused", "message": str(exc)},
        ) from exc


@router.get("/{claim_id}", response_model=ClaimOut)
async def get_claim(claim_id: UUID) -> Any:
    try:
        return await in_thread_for_claim(claim_id, lambda session: claim_summary(session, claim_id))
    except (AnalystError, TenantScopeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "claim_id": str(claim_id)},
        ) from exc


@router.get("/{claim_id}/history")
async def get_history(
    claim_id: UUID, limit: Annotated[int, Field(ge=1, le=500)] = 100
) -> dict[str, Any]:
    """The append-only transition trail. What an audit reads instead of a workflow log."""
    try:
        rows = await in_thread_for_claim(claim_id, lambda session: claim_history(session, claim_id))
    except TenantScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "claim_id": str(claim_id)},
        ) from exc
    return {"claim_id": str(claim_id), "transitions": rows[:limit], "count": len(rows)}

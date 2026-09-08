"""Review queue — the human-in-the-loop boundary.

n8n suspends a workflow by posting here and waits on `resume_token`. An analyst resolves
the row, and the workflow resumes with the resolution attached.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from services.api.src.models import ReviewQueue
from services.api.src.tenancy import (
    TenantScopeError,
    scope_to_resume_token_async,
    scope_to_review_async,
    set_tenant_async,
)
from services.rules.src.triage import ReviewReason, Severity

router = APIRouter(prefix="/review", tags=["review"])


class SuspendItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReviewReason
    severity: Severity = Severity.NORMAL
    summary: str
    citation: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SuspendRequest(BaseModel):
    """n8n posts this when triage says a claim cannot proceed."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    claim_id: UUID | None = None
    workflow_run_id: str | None = None
    items: list[SuspendItem] = Field(min_length=1)


class QueuedItem(BaseModel):
    review_id: UUID
    claim_id: UUID | None
    reason: str
    severity: str
    summary: str
    citation: str | None
    resume_token: str
    state: str
    created_at: datetime
    has_agent_memo: bool = False
    """Whether pre-analysis is already attached. Surfaced in the list so an analyst can
    see it before opening the row; the memo itself comes back from `inspect_exception`."""


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Annotated[str, Field(pattern="^(approved|rejected|corrected|deferred)$")]
    note: str | None = None
    analyst: str


def _session(request: Request) -> AsyncSession:
    """A session from the app-scoped maker.

    `app.state` is untyped by construction, so the maker comes back as `Any` and the
    annotation here is what re-establishes the type for every caller.
    """
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return maker()


@router.post("/suspend", status_code=status.HTTP_201_CREATED)
async def suspend(body: SuspendRequest, request: Request) -> dict[str, Any]:
    """Queue a claim for analyst attention and return the tokens n8n waits on.

    One row per reason rather than one per claim: an analyst resolving a threshold near
    miss has not thereby resolved a low-confidence extraction, and collapsing them would
    let the second disappear behind the first.
    """
    created: list[dict[str, Any]] = []
    async with _session(request) as session, session.begin():
        await set_tenant_async(session, body.tenant_id)
        for item in body.items:
            row = ReviewQueue(
                review_id=uuid4(),
                tenant_id=body.tenant_id,
                claim_id=body.claim_id,
                reason=item.reason.value,
                severity=item.severity.value,
                summary=item.summary,
                citation=item.citation,
                payload=item.payload,
                resume_token=secrets.token_urlsafe(24),
                workflow_run_id=body.workflow_run_id,
                state="open",
            )
            session.add(row)
            created.append(
                {
                    "review_id": str(row.review_id),
                    "reason": row.reason,
                    "severity": row.severity,
                    "resume_token": row.resume_token,
                }
            )

    blocking = any(i.severity is Severity.BLOCKING for i in body.items)
    return {
        "suspended": True,
        "blocking": blocking,
        "items": created,
    }


@router.get("/queue", response_model=list[QueuedItem])
async def list_queue(
    request: Request,
    tenant_id: UUID,
    state: Annotated[str, Field(pattern="^(open|claimed|resolved)$")] = "open",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> list[QueuedItem]:
    """Open exceptions for a tenant, oldest first.

    Oldest first because the deadline clock is the thing that kills a claim, and the
    oldest item is the closest to it.
    """
    async with _session(request) as session:
        await set_tenant_async(session, tenant_id)
        rows = (
            await session.execute(
                select(ReviewQueue)
                .where(
                    ReviewQueue.tenant_id == tenant_id,
                    ReviewQueue.state == state,
                )
                .order_by(ReviewQueue.created_at)
                .limit(limit)
            )
        ).scalars()
        return [
            QueuedItem(
                review_id=r.review_id,
                claim_id=r.claim_id,
                reason=r.reason,
                severity=r.severity,
                summary=r.summary,
                citation=r.citation,
                resume_token=r.resume_token,
                state=r.state,
                created_at=r.created_at,
                has_agent_memo=r.agent_memo is not None,
            )
            for r in rows
        ]


@router.post("/{review_id}/resolve")
async def resolve(review_id: UUID, body: ResolveRequest, request: Request) -> dict[str, Any]:
    """Record an analyst's decision and release the workflow.

    The CHECK constraint refuses a resolved row without a resolution and a timestamp, so
    the queue cannot empty without a record of what was actually decided.
    """
    async with _session(request) as session, session.begin():
        try:
            await scope_to_review_async(session, review_id)
        except TenantScopeError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found_or_already_resolved", "review_id": str(review_id)},
            ) from exc
        result = await session.execute(
            update(ReviewQueue)
            .where(ReviewQueue.review_id == review_id, ReviewQueue.state != "resolved")
            .values(
                state="resolved",
                resolution=body.resolution,
                resolution_note=body.note,
                assigned_to=body.analyst,
                resolved_at=datetime.now(UTC),
            )
            .returning(ReviewQueue.resume_token, ReviewQueue.claim_id)
        )
        row = result.first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found_or_already_resolved", "review_id": str(review_id)},
        )

    return {
        "review_id": str(review_id),
        "claim_id": str(row.claim_id) if row.claim_id else None,
        "resolution": body.resolution,
        "resume_token": row.resume_token,
        "resumed": body.resolution in {"approved", "corrected"},
    }


@router.post("/draft")
async def draft_memos(tenant_id: UUID, limit: int = 20) -> dict[str, Any]:
    """Draft agent pre-analysis for open, undrafted rows.

    Deliberately *not* called from `/suspend`. n8n suspends by posting there and waits on
    the token it gets back; making that path depend on a model call would couple workflow
    suspension to an unrelated service being reachable, and would add seconds to a request
    whose job is to record a decision durably. The memo is wanted when the analyst opens
    the row, so it is drafted on this separate, retryable call.

    Synchronous SQLAlchemy inside a threadpool rather than the async session, because the
    Anthropic SDK call in the middle is blocking and the agent worker is shared with the
    CLI entry point. Wrapping it here keeps one implementation instead of two.
    """
    from services.agent.src.queue import draft_pending
    from services.api.src.sync_db import in_thread

    def _run(session: Session) -> dict[str, Any]:
        report = draft_pending(session, tenant_id=tenant_id, limit=limit)
        return {
            "drafted": report.drafted,
            "skipped": report.skipped,
            "unavailable": report.unavailable,
        }

    return await in_thread(_run, tenant_id)


@router.get("/pending/{resume_token}")
async def poll(resume_token: str, request: Request) -> dict[str, Any]:
    """What n8n polls while suspended."""
    async with _session(request) as session:
        try:
            await scope_to_resume_token_async(session, resume_token)
        except TenantScopeError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="unknown token"
            ) from exc
        row = (
            await session.execute(
                select(ReviewQueue).where(ReviewQueue.resume_token == resume_token)
            )
        ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown token")

    return {
        "review_id": str(row.review_id),
        "state": row.state,
        "resolution": row.resolution,
        "resolved": row.state == "resolved",
        "may_continue": row.resolution in {"approved", "corrected"},
    }

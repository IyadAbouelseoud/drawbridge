"""Review queue — the human-in-the-loop boundary.

n8n suspends a workflow by posting here and waits on `resume_token`. An analyst resolves
the row, and the workflow resumes with the resolution attached.

**v1.1.0.** `POST /review/{id}/resolve` used to be a bare `UPDATE`: no reasoning required,
no ledger row, the analyst named by the request body, and callable by the pipeline's own
service token — so the component a review exists to check could clear its own review. It
now calls `analyst.resolve_exception`, the function `mcp-claims` has always used, so both
surfaces require reasoning, pass the human-only gate and write the same ledger event.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from drawbridge_schemas.agents import Scope
from services.api.src.analyst import AnalystError, resolve_exception
from services.api.src.auth import authorise_tenant, expected_tenant, require
from services.api.src.models import ReviewQueue
from services.api.src.tenancy import (
    TenantScopeError,
    scope_to_resume_token_async,
    scope_to_review,
    set_tenant_async,
)
from services.rules.src.triage import ReviewReason, Severity

router = APIRouter(prefix="/review", tags=["review"])


class SuspendItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReviewReason
    severity: Severity = Severity.NORMAL
    # Bounded: the summary is what an analyst reads in the queue list and what the drafter
    # quotes, and neither is served by an unbounded string from a caller.
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    citation: Annotated[str, Field(max_length=500)] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SuspendRequest(BaseModel):
    """n8n posts this when triage says a claim cannot proceed."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    claim_id: UUID | None = None
    workflow_run_id: Annotated[str, Field(max_length=128)] | None = None
    items: list[SuspendItem] = Field(min_length=1, max_length=20)


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
    # The audit record. Required and at least `analyst.MIN_REASONING_CHARS` long, the same
    # rule `mcp-claims` has always applied: this route was the one way to resolve a row
    # without saying why.
    note: Annotated[str, Field(max_length=4000)]
    # Ignored when a principal is present — the verified subject is recorded instead. Kept
    # so an existing client's payload still validates.
    analyst: str | None = None


def _session(request: Request) -> AsyncSession:
    """A session from the app-scoped maker.

    `app.state` is untyped by construction, so the maker comes back as `Any` and the
    annotation here is what re-establishes the type for every caller.
    """
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return maker()


@router.post(
    "/suspend",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Scope.REVIEW_SUSPEND))],
)
async def suspend(body: SuspendRequest) -> dict[str, Any]:
    """Queue a claim for analyst attention and return the tokens n8n waits on.

    One row per reason rather than one per claim: an analyst resolving a threshold near
    miss has not thereby resolved a low-confidence extraction, and collapsing them would
    let the second disappear behind the first.

    Each row is also a `review_opened` ledger event. The event type has existed since
    week 10 and nothing wrote it, so the hash chain recorded how every exception was
    *resolved* and never that it had been raised — or why the claim stopped.
    """
    from services.api.src.auth import actor_name
    from services.api.src.ledger import record
    from services.api.src.sync_db import in_thread

    tenant_id = authorise_tenant(body.tenant_id)
    actor = actor_name("pipeline")

    def _write(session: Session) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for item in body.items:
            row = ReviewQueue(
                review_id=uuid4(),
                tenant_id=tenant_id,
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
            session.flush()
            record(
                session,
                tenant_id=tenant_id,
                claim_id=body.claim_id,
                event_type="review_opened",
                actor=actor,
                subject=row.reason,
                payload={
                    "review_id": str(row.review_id),
                    "severity": row.severity,
                    "summary": row.summary,
                    "citation": row.citation,
                    "workflow_run_id": body.workflow_run_id,
                },
            )
            created.append(
                {
                    "review_id": str(row.review_id),
                    "reason": row.reason,
                    "severity": row.severity,
                    "resume_token": row.resume_token,
                }
            )
        return created

    created = await in_thread(_write, tenant_id)

    blocking = any(i.severity is Severity.BLOCKING for i in body.items)
    return {
        "suspended": True,
        "blocking": blocking,
        "items": created,
    }


@router.get(
    "/queue",
    response_model=list[QueuedItem],
    dependencies=[Depends(require(Scope.REVIEW_READ))],
)
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
    scope = authorise_tenant(tenant_id)
    async with _session(request) as session:
        await set_tenant_async(session, scope)
        rows = (
            await session.execute(
                select(ReviewQueue)
                .where(
                    ReviewQueue.tenant_id == scope,
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


@router.post(
    "/{review_id}/resolve",
    dependencies=[Depends(require(Scope.REVIEW_RESOLVE))],
)
async def resolve(review_id: UUID, body: ResolveRequest) -> dict[str, Any]:
    """Record an analyst's decision and release the workflow.

    Delegates to `analyst.resolve_exception`, so the reasoning floor, the human-only gate,
    the high-value four-eyes rule and the ledger event are the same here as over MCP. The
    CHECK constraint still refuses a resolved row without a resolution and a timestamp.
    """
    from services.api.src.sync_db import sync_session

    expected = expected_tenant()

    def _run() -> dict[str, Any]:
        with sync_session() as session:
            scope_to_review(session, review_id, expected)
            return resolve_exception(
                session,
                review_id=review_id,
                resolution=body.resolution,
                reasoning=body.note,
                analyst=body.analyst or "unauthenticated",
            )

    from anyio import to_thread

    try:
        outcome = await to_thread.run_sync(_run)
    except TenantScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found_or_already_resolved", "review_id": str(review_id)},
        ) from exc
    except AnalystError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "not_resolvable", "message": str(exc)},
        ) from exc

    return {
        "review_id": str(review_id),
        "claim_id": outcome["claim_id"],
        "resolution": body.resolution,
        "resume_token": outcome["resume_token"],
        "resumed": outcome["may_resume"],
        "outstanding_exceptions": outcome["outstanding_exceptions"],
        "analyst": outcome["analyst"],
    }


#: Rows drafted per call. Every row is a model call; an unbounded `limit` made this route a
#: way to spend an arbitrary amount of API budget in one request.
MAX_DRAFT_BATCH = 50


@router.get("/overview", dependencies=[Depends(require(Scope.REVIEW_READ))])
async def overview(
    limit: Annotated[int, Field(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Open exceptions across every tenant the caller may see, most urgent first.

    What the review dispatcher polls. It used to call `/review/queue?tenant_id=` with a
    tenant id read from a Schedule Trigger's output — which has none — so every sweep was
    refused and the dispatcher never escalated anything. For the cross-tenant pipeline
    identity this walks each tenant with open work under its own scope; for a user it is
    their own tenant's queue.
    """
    from services.api.src.analyst import list_queue as ordered_queue
    from services.api.src.sync_db import sync_session
    from services.api.src.tenancy import tenants_with_open_reviews

    principal_tenant = expected_tenant()

    def _run() -> list[dict[str, Any]]:
        with sync_session() as session:
            tenants = (
                [principal_tenant]
                if principal_tenant is not None
                else tenants_with_open_reviews(session)
            )
        rows: list[dict[str, Any]] = []
        for tenant in tenants:
            with sync_session(tenant) as session:
                for row in ordered_queue(session, tenant_id=tenant, limit=limit):
                    row.pop("resume_token", None)
                    rows.append(row)
        return rows[:limit]

    from anyio import to_thread

    items = await to_thread.run_sync(_run)
    return {"count": len(items), "items": items}


@router.post("/draft", dependencies=[Depends(require(Scope.REVIEW_DRAFT))])
async def draft_memos(
    tenant_id: UUID | None = None,
    limit: Annotated[int, Field(ge=1, le=MAX_DRAFT_BATCH)] = 20,
) -> dict[str, Any]:
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

    # A user drafts for their own tenant. The pipeline identity may name one, or name none
    # and sweep every tenant with undrafted rows — the dispatcher's backstop.
    principal = expected_tenant()
    scope: UUID | None = (
        authorise_tenant(tenant_id) if (tenant_id is not None or principal is not None) else None
    )

    def _run(session: Session) -> dict[str, Any]:
        report = draft_pending(session, tenant_id=scope, limit=limit)
        return {
            "drafted": report.drafted,
            "skipped": report.skipped,
            "withheld": report.withheld,
            "unavailable": report.unavailable,
            "halted": report.halted,
        }

    return await in_thread(_run)


@router.get("/pending/{resume_token}", dependencies=[Depends(require(Scope.REVIEW_READ))])
async def poll(resume_token: str, request: Request) -> dict[str, Any]:
    """What n8n polls while suspended."""
    async with _session(request) as session:
        try:
            await scope_to_resume_token_async(session, resume_token, expected_tenant())
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

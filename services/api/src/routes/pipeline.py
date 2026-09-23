"""The pipeline's front door and its failure report.

**`POST /pipeline/admit` — the ingest webhook authenticates its caller.** n8n's ingest
webhook was unauthenticated, and every call the workflow made after it carried the
pipeline's cross-tenant token. So anyone able to reach the webhook could drive a run
against whichever `tenant_id` their payload named: store documents under that tenant,
persist a claim, suspend reviews — the tenant boundary §16 and §17 built was one webhook
away from irrelevant. The workflow now forwards the *caller's own* `Authorization` header
here first. The API verifies it like any other request, requires `pipeline:start`, and
checks the tenant against the token. A caller who is not a user of that tenant stops the
run at its first node, with the pipeline's credential never used on their behalf.

It also records which tenant the run belongs to, keyed by n8n's execution id. That is the
one fact the error workflow needs and cannot get from n8n.

**`POST /pipeline/failure` — the error workflow can finally say something.** Its old
request posted `tenant_id: e.tenant_id` from an Error Trigger payload that has no tenant
in it; JavaScript dropped the undefined key, the API refused the request, and a crashed run
left no review row at all. The §23 fix made runs *fail*; this makes the failure *land*.
The row is attributed through the run record admit wrote, and it is a blocking
`pipeline_failure` — previously every crash was mislabelled `solver_infeasible`, which sent
the drafter off to explain an optimisation problem that never happened.

The run record lives in Redis with a thirty-day expiry. It is operational metadata, not
business state: losing it costs attribution of a failure report, never a claim.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from drawbridge_schemas.agents import Scope
from services.api.src.auth import actor_name, authorise_tenant, current_principal, require
from services.api.src.ledger import record
from services.api.src.models import ReviewQueue
from services.api.src.sync_db import in_thread
from services.rules.src.triage import ReviewReason, Severity

log = structlog.get_logger()

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

RUN_TTL_SECONDS = 30 * 24 * 3600
_RUN_KEY = "drawbridge:run:{}"


class AdmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    run_id: Annotated[str, Field(min_length=1, max_length=128)]


class FailureReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_run_id: Annotated[str, Field(min_length=1, max_length=128)]
    node: Annotated[str, Field(max_length=200)] = ""
    message: Annotated[str, Field(max_length=2000)] = ""
    workflow: Annotated[str, Field(max_length=200)] = ""


@router.post(
    "/admit",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require(Scope.PIPELINE_START))],
)
async def admit(body: AdmitRequest, request: Request) -> dict[str, Any]:
    """Authorise a run for a tenant, on the caller's own credential, and record it."""
    tenant_id = authorise_tenant(body.tenant_id)
    principal = current_principal()
    await request.app.state.redis.set(
        _RUN_KEY.format(body.run_id), str(tenant_id), ex=RUN_TTL_SECONDS
    )
    log.info(
        "pipeline.admitted",
        run_id=body.run_id,
        tenant_id=str(tenant_id),
        principal=principal.subject if principal else None,
    )
    return {
        "admitted": True,
        "tenant_id": str(tenant_id),
        "run_id": body.run_id,
        "admitted_by": principal.subject if principal else None,
    }


@router.post(
    "/failure",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Scope.REVIEW_SUSPEND))],
)
async def failure(body: FailureReport, request: Request) -> dict[str, Any]:
    """Record a crashed run as a blocking review row against the tenant it was for."""
    raw = await request.app.state.redis.get(_RUN_KEY.format(body.workflow_run_id))
    if not raw:
        # Nothing to attribute it to — a run that failed before admission, or one older
        # than the record's expiry. Logged at error: it is a failure nobody will see in a
        # queue, and the log is the only place left to say so.
        log.error(
            "pipeline.failure_unattributed",
            run_id=body.workflow_run_id,
            node=body.node,
            message=body.message[:300],
        )
        return {"recorded": False, "reason": "run was never admitted or has expired"}

    tenant_id = authorise_tenant(UUID(str(raw)))
    actor = actor_name("pipeline")
    summary = (
        f"pipeline run {body.workflow_run_id} failed at node {body.node or 'unknown'}: "
        f"{body.message or 'no message'}"
    )[:2000]

    def _write(session: Session) -> str:
        row = ReviewQueue(
            review_id=uuid4(),
            tenant_id=tenant_id,
            claim_id=None,
            reason=ReviewReason.PIPELINE_FAILURE.value,
            severity=Severity.BLOCKING.value,
            summary=summary,
            payload={"node": body.node, "workflow": body.workflow},
            resume_token=secrets.token_urlsafe(24),
            workflow_run_id=body.workflow_run_id,
            state="open",
            # An infrastructure failure, not a claim defect: there is nothing to draft.
            agent_model="withheld:pipeline_failure",
        )
        session.add(row)
        session.flush()
        record(
            session,
            tenant_id=tenant_id,
            event_type="review_opened",
            actor=actor,
            subject=row.reason,
            payload={"review_id": str(row.review_id), "summary": summary},
        )
        return str(row.review_id)

    review_id = await in_thread(_write, tenant_id)
    return {"recorded": True, "review_id": review_id, "tenant_id": str(tenant_id)}

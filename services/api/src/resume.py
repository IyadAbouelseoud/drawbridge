"""Resuming a suspended n8n workflow after an analyst decision.

n8n's Wait node parks on a webhook. When an exception clears, something has to call that
webhook or the run sits until it times out.

**Why this is a client call and not a Postgres trigger.** A trigger firing an HTTP request
means either `pg_net`/`plpython` in the database image, or a `NOTIFY` plus a listener that
is itself a client. The first puts network egress and retry policy inside a transaction —
a failed webhook would roll back an analyst's recorded decision, which is precisely
backwards, since the decision is the durable fact and the notification is the disposable
one. The second is this module with extra steps.

So: the decision commits first, unconditionally. Notification is best-effort afterwards,
and its failure is recorded on the row rather than raised. A missed webhook costs one
polling cycle of the review dispatcher; a rolled-back decision costs the audit trail.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

# n8n's Wait node exposes its resume URL under this path, keyed by the execution id the
# workflow recorded when it suspended.
DEFAULT_N8N_BASE = "http://n8n:5678"
RESUME_PATH = "/webhook-waiting"

RESUME_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """What happened when we tried to wake the workflow.

    `delivered=False` is not an error the caller should raise on — see the module
    docstring. It is a fact to record so the dispatcher knows to pick the claim up.
    """

    delivered: bool
    status_code: int | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "status_code": self.status_code,
            "detail": self.detail,
        }


def n8n_base_url() -> str:
    return os.environ.get("DRAWBRIDGE_N8N_BASE_URL", DEFAULT_N8N_BASE).rstrip("/")


def resume_workflow(
    *,
    workflow_run_id: str | None,
    resume_token: str,
    payload: dict[str, Any],
    timeout: float = RESUME_TIMEOUT_SECONDS,
) -> ResumeOutcome:
    """Wake a suspended run. Never raises.

    Called after the analyst's decision has committed, so a failure here leaves the
    decision intact and the workflow merely waiting for the next dispatcher sweep.
    """
    if not workflow_run_id:
        return ResumeOutcome(
            delivered=False,
            status_code=None,
            detail=(
                "exception carries no workflow_run_id — it was queued outside a workflow "
                "run, so there is nothing to resume"
            ),
        )

    url = f"{n8n_base_url()}{RESUME_PATH}/{workflow_run_id}"
    body = {"resume_token": resume_token, **payload}

    try:
        response = httpx.post(url, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        log.warning(
            "resume.failed",
            workflow_run_id=workflow_run_id,
            error=type(exc).__name__,
        )
        return ResumeOutcome(
            delivered=False,
            status_code=None,
            detail=f"{type(exc).__name__} calling {url}; the review dispatcher will retry",
        )

    delivered = 200 <= response.status_code < 300
    if not delivered:
        log.warning(
            "resume.rejected",
            workflow_run_id=workflow_run_id,
            status=response.status_code,
        )
    return ResumeOutcome(
        delivered=delivered,
        status_code=response.status_code,
        detail=(
            "workflow resumed"
            if delivered
            else f"n8n returned {response.status_code}; the run may have already timed out"
        ),
    )

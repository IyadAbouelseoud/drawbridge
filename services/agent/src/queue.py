"""Drafting memos for queued exceptions, and attaching them to the row.

**Why this is a worker and not part of `POST /review/suspend`.** n8n suspends a workflow
by posting to that endpoint and waits on the token it returns. Putting a model call in
that path would make suspension depend on the API being reachable and would add seconds
to a request whose whole job is to record a decision durably and get out. The memo is
useful when the analyst arrives, which is minutes to hours later; drafting it then loses
nothing and keeps the queue write on a path that cannot fail for an unrelated reason.

**Resumable and concurrency-safe** by the same construction as the embedding pass:
`FOR UPDATE SKIP LOCKED` over rows with a NULL memo. Two workers can run against one
queue without either blocking or double-drafting, an interrupted run continues, and a
second run over a drafted queue is a no-op.

**A failed draft is left NULL, not written as an error.** The row remains exactly the row
an analyst would have read before this service existed. The alternative — an error blob
in the memo column — would mean an analyst scanning for pre-analysis finds something and
reads it. Nothing is strictly better than noise here.

**v1.1.0: a governed agent.** The drafter has a registered identity (`agent:memo-drafter`,
owner: compliance), and everything it does is on the record under that name:

- It runs as the app role and scopes to one tenant at a time. Until now it ran unscoped,
  which under the app role — the role the on-prem stack gives it — selects nothing: the
  deployed worker polled an empty queue forever and, logging only passes that did
  something, said nothing.
- Every attach is an `agent_memo_drafted` ledger event carrying the digest of the facts
  the model saw; every refusal is `agent_memo_withheld`, and a refused row is marked so the
  next pass does not pay for the same refusal again.
- A row whose source text is addressed to a model is never sent to one
  (`injection.py`): it becomes a `prompt_injection_suspected` event and an open
  `suspected_prompt_injection` exception that blocks the claim's approval.
- The kill switch is checked before every row, and three guard failures in an hour throw
  it for this agent on its own (the circuit breaker). Only a person releases it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from services.agent.src import exceptions as exception_memo
from services.agent.src.client import MODEL, AgentRefusedError, AgentUnavailableError
from services.agent.src.grounding import FabricatedCitationError, UngroundedFigureError
from services.agent.src.injection import SuspectedInjectionError
from services.agent.src.output_guard import OutputPolicyError
from services.api.src import killswitch
from services.api.src.ledger import record
from services.api.src.models import ReviewQueue
from services.api.src.tenancy import set_tenant, tenants_with_undrafted_reviews
from services.rules.src.triage import ReviewReason

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Prompt version travels with the memo. The model id alone does not identify what was
# asked; a prompt revision changes the output as surely as a model change does, and
# "which memo did the old prompt produce" is only answerable if this was recorded.
PROMPT_VERSION = "exception/v1"

_PENDING = text("""
    SELECT review_id, tenant_id, claim_id, reason, severity, summary, citation, payload
    FROM review_queue
    WHERE agent_memo IS NULL
      AND agent_model IS NULL
      AND state = 'open'
      AND tenant_id = CAST(:tenant_id AS uuid)
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
""")

_ATTACH = text("""
    UPDATE review_queue
       SET agent_memo = CAST(:memo AS jsonb),
           agent_model = :model,
           agent_drafted_at = :drafted_at
     WHERE review_id = :review_id
""")

_ATTACH_MODEL_ONLY = text("""
    UPDATE review_queue
       SET agent_model = :model,
           agent_drafted_at = :drafted_at
     WHERE review_id = :review_id
""")


@dataclass(frozen=True, slots=True)
class DraftReport:
    """What one pass did. Counts only — memo text belongs on the row, not in a log."""

    drafted: int = 0
    skipped: int = 0
    """Rows the drafter could not ground or schema-satisfy. Left NULL."""
    unavailable: int = 0
    """Rows not attempted because the API was unreachable. Also left NULL, and
    distinguished from `skipped` because it is an infrastructure fact rather than a
    statement about the row."""
    withheld: int = 0
    """Rows never sent to the model: suspected injection, or a reason that is not drafted."""
    halted: bool = False
    """The pass stopped because a kill switch covering the drafter is engaged."""

    @property
    def attempted(self) -> int:
        return self.drafted + self.skipped + self.unavailable + self.withheld


def model_tag(model: str = MODEL) -> str:
    return f"{model}+{PROMPT_VERSION}"


def draft_for_row(
    row: dict[str, Any],
    *,
    citations: tuple[str, ...] = (),
    **kwargs: Any,
) -> dict[str, Any]:
    """Draft one memo from a queue row. Returns the memo as a JSON-ready dict."""
    facts = exception_memo.build_facts(
        reason=ReviewReason(row["reason"]),
        severity=row["severity"],
        summary=row["summary"],
        payload=row.get("payload") or {},
        citation=row.get("citation"),
        citations=citations,
    )
    return exception_memo.draft(facts, **kwargs).model_dump(mode="json")


# --------------------------------------------------------------------------------------
# v1.1.0 — the drafter as a governed agent
# --------------------------------------------------------------------------------------

#: The drafter's registered identity (`drawbridge_schemas.agents`). Its ledger rows say
#: this, its kill switch is keyed on it, and its owner is the compliance role.
AGENT_ID = "agent:memo-drafter"

#: Guard failures inside `BREAKER_WINDOW_SECONDS` that trip the circuit breaker. Three in
#: an hour is not a model having an off day on one row; it is a model, a prompt or an input
#: stream that has changed, and the drafter stops itself rather than waiting for an analyst
#: to notice that the memos have started failing.
BREAKER_THRESHOLD = 3
BREAKER_WINDOW_SECONDS = 3600.0

#: Failures that count toward the breaker: the model produced something the guards
#: refused. An unreachable API and a suspicious *input* do not count — neither says
#: anything about what the model is doing.
_OUTPUT_FAILURES = (
    AgentRefusedError,
    UngroundedFigureError,
    FabricatedCitationError,
    OutputPolicyError,
)

_breaker_lock = threading.Lock()
_breaker_events: deque[float] = deque()


def _breaker_record(now: float | None = None) -> int:
    """Record one output failure; return how many sit inside the window."""
    stamp = time.monotonic() if now is None else now
    with _breaker_lock:
        _breaker_events.append(stamp)
        while _breaker_events and stamp - _breaker_events[0] > BREAKER_WINDOW_SECONDS:
            _breaker_events.popleft()
        return len(_breaker_events)


def reset_breaker() -> None:
    """For tests, and for a process that a human has just released."""
    with _breaker_lock:
        _breaker_events.clear()


def _trip_breaker(session: Session, failures: int, last: Exception) -> None:
    killswitch.engage(
        session,
        actor=AGENT_ID,
        actor_kind="machine",
        reason=(
            f"circuit breaker: {failures} memo(s) refused by the output guards within "
            f"{int(BREAKER_WINDOW_SECONDS)}s; last was {type(last).__name__}. The drafter "
            "has stopped itself and a person must release it."
        ),
        scope_kind="principal",
        scope_value=AGENT_ID,
        event_type="circuit_breaker_tripped",
    )
    session.commit()


def _facts_digest(row: dict[str, Any]) -> str:
    body = json.dumps(
        {k: row.get(k) for k in ("reason", "severity", "summary", "citation", "payload")},
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _withhold(session: Session, row: dict[str, Any], why: str, detail: dict[str, Any]) -> None:
    """Record that no memo will be drafted for this row, and why, so it is not retried.

    Until v1.1.0 a row the guards refused stayed undrafted and was selected again on the
    next pass — a model call every two minutes, forever, reaching the same refusal. The
    memo column stays NULL (no memo is better than noise, §13.5); `agent_model` records
    the refusal so the poll skips it. `draft_one(..., overwrite=True)` still redrafts it.
    """
    session.execute(
        _ATTACH_MODEL_ONLY,
        {
            "review_id": row["review_id"],
            "model": f"withheld:{why}",
            "drafted_at": datetime.now(UTC),
        },
    )
    record(
        session,
        tenant_id=row["tenant_id"],
        claim_id=row["claim_id"],
        event_type="agent_memo_withheld",
        actor=AGENT_ID,
        subject=row["reason"],
        payload={"review_id": str(row["review_id"]), "why": why, **detail},
    )


def _raise_injection(session: Session, row: dict[str, Any], exc: SuspectedInjectionError) -> None:
    """A source document spoke to the model. Record it and put it in front of a person."""
    findings = [f.as_dict() for f in exc.findings[:10]]
    record(
        session,
        tenant_id=row["tenant_id"],
        claim_id=row["claim_id"],
        event_type="prompt_injection_suspected",
        actor=AGENT_ID,
        subject=row["reason"],
        payload={"review_id": str(row["review_id"]), "findings": findings},
    )
    # An open exception on the claim, so it cannot be approved until someone has looked —
    # `analyst.transition_claim` refuses APPROVED over any unresolved row.
    review = ReviewQueue(
        review_id=uuid4(),
        tenant_id=row["tenant_id"],
        claim_id=row["claim_id"],
        reason=ReviewReason.SUSPECTED_PROMPT_INJECTION.value,
        severity="high",
        summary=(
            "Text on a source document is addressed to an automated reader rather than to "
            "a customs officer. The drafter declined to process it. Examine the document "
            "before relying on anything extracted from it."
        ),
        payload={"source_review_id": str(row["review_id"]), "findings": findings},
        resume_token=secrets.token_urlsafe(24),
        state="open",
        # Marked so the drafter never picks it up: this row is for a person.
        agent_model="withheld:suspected_prompt_injection",
        agent_drafted_at=datetime.now(UTC),
    )
    session.add(review)
    _withhold(session, row, "suspected_prompt_injection", {"findings": len(findings)})


def _attach(session: Session, row: dict[str, Any], memo: dict[str, Any], model: str) -> None:
    session.execute(
        _ATTACH,
        {
            "review_id": row["review_id"],
            "memo": _json(memo),
            "model": model_tag(model),
            "drafted_at": datetime.now(UTC),
        },
    )
    # What the model was shown (by digest) and what it recommended, so "which memo did an
    # analyst read before deciding" is answerable from the ledger alone.
    record(
        session,
        tenant_id=row["tenant_id"],
        claim_id=row["claim_id"],
        event_type="agent_memo_drafted",
        actor=AGENT_ID,
        subject=row["reason"],
        payload={
            "review_id": str(row["review_id"]),
            "model": model_tag(model),
            "facts_sha256": _facts_digest(row),
            "recommendation": memo.get("recommendation"),
        },
    )


def draft_pending(
    session: Session,
    *,
    tenant_id: UUID | None = None,
    limit: int = 20,
    model: str = MODEL,
    **kwargs: Any,
) -> DraftReport:
    """Draft memos for open, undrafted rows, one tenant at a time.

    Commits per row rather than per batch. A batch commit would discard every memo in
    flight when one row's model call fails, and these are not cheap to regenerate.

    `tenant_id=None` means every tenant with work waiting, found through the one
    SECURITY DEFINER lookup that answers with ids; each is then drafted inside its own
    row-level scope. Advisory only: nothing here resolves a row or moves a claim.
    """
    # Checked before anything else, so a halted drafter reports `halted` on every pass —
    # including the passes with no work. Checked only per row, it read as merely idle
    # whenever the queue was empty, and "stopped" and "nothing to do" looked the same.
    if killswitch.state(session).covering(principal=AGENT_ID) is not None:
        session.rollback()
        return DraftReport(halted=True)

    tenants = [tenant_id] if tenant_id is not None else tenants_with_undrafted_reviews(session)
    session.commit()

    drafted = skipped = unavailable = withheld = 0
    halted = False
    mutating = killswitch.MUTATING.set(True)
    acting = killswitch.PRINCIPAL.set(AGENT_ID)
    try:
        for tenant in tenants:
            if halted or unavailable:
                break
            while drafted + skipped + withheld < limit:
                try:
                    # The kill switch is checked here, per row: `set_tenant` enforces it
                    # inside a mutating unit of work.
                    set_tenant(session, tenant)
                except killswitch.KillSwitchEngagedError:
                    session.rollback()
                    halted = True
                    break
                found = session.execute(_PENDING, {"tenant_id": tenant}).mappings().first()
                if found is None:
                    session.commit()
                    break
                row = dict(found)
                try:
                    memo = draft_for_row(row, model=model, **kwargs)
                except AgentUnavailableError:
                    # The API is down, not this row's problem. Stop rather than burning the
                    # rest of the pass discovering the same thing once per row.
                    session.rollback()
                    unavailable += 1
                    break
                except SuspectedInjectionError as exc:
                    _raise_injection(session, row, exc)
                    session.commit()
                    withheld += 1
                    continue
                except _OUTPUT_FAILURES as exc:
                    _withhold(session, row, type(exc).__name__, {"detail": str(exc)[:300]})
                    session.commit()
                    skipped += 1
                    failures = _breaker_record()
                    if failures >= BREAKER_THRESHOLD:
                        _trip_breaker(session, failures, exc)
                        halted = True
                        break
                    continue
                except ValueError as exc:
                    _withhold(session, row, "not_draftable", {"detail": str(exc)[:300]})
                    session.commit()
                    withheld += 1
                    continue

                _attach(session, row, memo, model)
                session.commit()
                drafted += 1
    finally:
        killswitch.PRINCIPAL.reset(acting)
        killswitch.MUTATING.reset(mutating)

    session.commit()
    return DraftReport(
        drafted=drafted,
        skipped=skipped,
        unavailable=unavailable,
        withheld=withheld,
        halted=halted,
    )


_ONE = text("""
    SELECT review_id, tenant_id, claim_id, reason, severity, summary, citation, payload,
           agent_memo
    FROM review_queue
    WHERE review_id = :review_id
    FOR UPDATE
""")


def draft_one(
    session: Session,
    review_id: UUID,
    *,
    overwrite: bool = False,
    model: str = MODEL,
    **kwargs: Any,
) -> dict[str, Any]:
    """Draft for one named row, on demand. Advisory: nothing here resolves the row.

    Separate from `draft_pending` because the failure posture differs. The worker skips a
    row it cannot draft and moves on — an analyst asking for a memo interactively wants
    the reason it could not be produced, so this raises rather than swallowing. The
    refusal is still recorded first, exactly as the worker would record it.
    """
    found = session.execute(_ONE, {"review_id": review_id}).mappings().first()
    if found is None:
        msg = f"no review queue row {review_id}"
        raise ValueError(msg)
    row = dict(found)
    if row["agent_memo"] is not None and not overwrite:
        return {
            "review_id": str(review_id),
            "drafted": False,
            "reason": "a memo is already attached; pass overwrite=true to redraft",
            "memo": row["agent_memo"],
        }

    killswitch.assert_running(session, principal=AGENT_ID, tenant_id=row["tenant_id"])
    try:
        memo = draft_for_row(row, model=model, **kwargs)
    except SuspectedInjectionError as exc:
        _raise_injection(session, row, exc)
        session.commit()
        raise
    except _OUTPUT_FAILURES as exc:
        _withhold(session, row, type(exc).__name__, {"detail": str(exc)[:300]})
        session.commit()
        failures = _breaker_record()
        if failures >= BREAKER_THRESHOLD:
            _trip_breaker(session, failures, exc)
        raise

    _attach(session, row, memo, model)
    session.commit()
    return {"review_id": str(review_id), "drafted": True, "memo": memo}


def _json(memo: dict[str, Any]) -> str:
    return json.dumps(memo, ensure_ascii=False)

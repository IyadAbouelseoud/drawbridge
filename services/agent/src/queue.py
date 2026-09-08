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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from services.agent.src import exceptions as exception_memo
from services.agent.src.client import MODEL, AgentRefusedError, AgentUnavailableError
from services.agent.src.grounding import UngroundedFigureError
from services.rules.src.triage import ReviewReason

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Prompt version travels with the memo. The model id alone does not identify what was
# asked; a prompt revision changes the output as surely as a model change does, and
# "which memo did the old prompt produce" is only answerable if this was recorded.
PROMPT_VERSION = "exception/v1"

_PENDING = text("""
    SELECT review_id, reason, severity, summary, citation, payload
    FROM review_queue
    WHERE agent_memo IS NULL
      AND state = 'open'
      AND (CAST(:tenant_id AS uuid) IS NULL OR tenant_id = CAST(:tenant_id AS uuid))
    ORDER BY created_at
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
""")

_ATTACH = text("""
    UPDATE review_queue
       SET agent_memo = CAST(:memo AS jsonb),
           agent_model = :model,
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

    @property
    def attempted(self) -> int:
        return self.drafted + self.skipped + self.unavailable


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


def draft_pending(
    session: Session,
    *,
    tenant_id: UUID | None = None,
    limit: int = 20,
    model: str = MODEL,
    **kwargs: Any,
) -> DraftReport:
    """Draft memos for open, undrafted rows.

    Commits per row rather than per batch. A batch commit would discard every memo in
    flight when one row's model call fails, and these are not cheap to regenerate.
    """
    rows = session.execute(_PENDING, {"tenant_id": tenant_id, "limit": limit}).mappings().all()
    drafted = skipped = unavailable = 0

    for row in rows:
        try:
            memo = draft_for_row(dict(row), model=model, **kwargs)
        except AgentUnavailableError:
            # The API is down, not this row's problem. Stop rather than burning the rest
            # of the batch discovering the same thing once per row.
            unavailable += len(rows) - drafted - skipped
            break
        except (AgentRefusedError, UngroundedFigureError, ValueError):
            skipped += 1
            continue

        session.execute(
            _ATTACH,
            {
                "review_id": row["review_id"],
                "memo": _json(memo),
                "model": model_tag(model),
                "drafted_at": datetime.now(UTC),
            },
        )
        session.commit()
        drafted += 1

    session.commit()
    return DraftReport(drafted=drafted, skipped=skipped, unavailable=unavailable)


_ONE = text("""
    SELECT review_id, reason, severity, summary, citation, payload, agent_memo
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
    """Draft for one named row, on demand.

    Separate from `draft_pending` because the failure posture differs. The worker skips a
    row it cannot draft and moves on — an analyst asking for a memo interactively wants
    the reason it could not be produced, so this raises rather than swallowing.
    """
    row = session.execute(_ONE, {"review_id": review_id}).mappings().first()
    if row is None:
        msg = f"no review queue row {review_id}"
        raise ValueError(msg)
    if row["agent_memo"] is not None and not overwrite:
        return {
            "review_id": str(review_id),
            "drafted": False,
            "reason": "a memo is already attached; pass overwrite=true to redraft",
            "memo": row["agent_memo"],
        }

    memo = draft_for_row(dict(row), model=model, **kwargs)
    session.execute(
        _ATTACH,
        {
            "review_id": review_id,
            "memo": _json(memo),
            "model": model_tag(model),
            "drafted_at": datetime.now(UTC),
        },
    )
    session.commit()
    return {"review_id": str(review_id), "drafted": True, "memo": memo}


def _json(memo: dict[str, Any]) -> str:
    import json

    return json.dumps(memo, ensure_ascii=False)

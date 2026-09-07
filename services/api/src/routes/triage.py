"""Triage endpoint — the gate n8n consults before persisting a claim."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from drawbridge_schemas.jurisdiction import Jurisdiction
from drawbridge_schemas.provenance import Confidence
from services.matcher.src.base import (
    MatchResult,
    Rejection,
    RejectionCode,
    SolverStatus,
)
from services.rules.src.triage import confidence_summary, triage

router = APIRouter(prefix="/triage", tags=["triage"])


class RejectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    export_line_id: str
    import_line_id: str | None = None
    code: RejectionCode
    citation: str = ""
    detail: str = ""


class MatchResultIn(BaseModel):
    """The matcher's response, as n8n received it."""

    model_config = ConfigDict(extra="ignore")

    jurisdiction: Jurisdiction
    status: SolverStatus
    rejections: list[RejectionIn] = Field(default_factory=list)
    candidate_pairs: int = 0
    wall_time_seconds: float = 0.0
    detail: str = ""


class TriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_result: MatchResultIn
    confidences: list[Confidence] = Field(default_factory=list)
    filing_deadline: date | None = None
    as_of: date | None = None


class TriageResponse(BaseModel):
    requires_review: bool
    blocking: bool
    highest_severity: str
    items: list[dict[str, Any]]
    confidence: dict[str, Any]


@router.post("/evaluate", response_model=TriageResponse)
async def evaluate(body: TriageRequest) -> TriageResponse:
    """Decide whether a matched claim may proceed without an analyst.

    Returns the review items verbatim so n8n can post them straight to /review/suspend
    without reshaping — one row per reason, because resolving a threshold near miss does
    not resolve a low-confidence extraction.
    """
    result = MatchResult(
        jurisdiction=body.match_result.jurisdiction,
        status=body.match_result.status,
        rejections=tuple(
            Rejection(
                export_line_id=r.export_line_id,
                import_line_id=r.import_line_id,
                code=r.code,
                citation=r.citation,
                detail=r.detail,
            )
            for r in body.match_result.rejections
        ),
        candidate_pairs=body.match_result.candidate_pairs,
        wall_time_seconds=body.match_result.wall_time_seconds,
        detail=body.match_result.detail,
    )

    verdict = triage(
        result,
        confidences=body.confidences,
        filing_deadline=body.filing_deadline,
        as_of=body.as_of,
    )

    return TriageResponse(
        requires_review=verdict.requires_review,
        blocking=verdict.is_blocking,
        highest_severity=verdict.highest_severity.value,
        items=[item.as_row() for item in verdict.items],
        confidence=confidence_summary(body.confidences),
    )

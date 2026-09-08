"""Matching endpoints.

Selects the US solver or the GCC linker from the claim's jurisdiction. This is the only
place in the API that branches on jurisdiction; the response shape is identical either
way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from drawbridge_schemas.bom import BillOfMaterials
from drawbridge_schemas.jurisdiction import Jurisdiction, profile_for
from drawbridge_schemas.trade import EntryLine, ExportLine
from services.api.src.telemetry import tracer
from services.matcher.src.base import MatchRequest, MatchResult
from services.matcher.src.router import run_match

router = APIRouter(prefix="/matching", tags=["matching"])


class MatchRequestBody(BaseModel):
    """A matching run.

    `as_of` is the filing date deadlines are evaluated against — it defaults to today,
    but a claim being re-run for audit must be able to pin the date it was originally
    assessed on, or the result will not reproduce.
    """

    model_config = ConfigDict(extra="forbid")

    jurisdiction: Jurisdiction
    imports: list[EntryLine] = Field(min_length=1)
    exports: list[ExportLine] = Field(min_length=1)
    as_of: date | None = None
    time_limit_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    claimant_is_importer_of_record: bool = True
    proof_of_purchase: bool = False

    boms: dict[str, BillOfMaterials] = Field(
        default_factory=dict,
        description=(
            "Bills of materials keyed by finished-good HTS code. Supplying one turns "
            "that finished good into manufacturing drawback under 19 U.S.C. 1313(a)/(b), "
            "where the import/export exchange rate is the BOM multiplier rather than 1. "
            "Omit for unused-merchandise claims under 1313(j)."
        ),
    )


class RejectionOut(BaseModel):
    export_line_id: str
    import_line_id: str | None
    code: str
    citation: str
    detail: str


class MatchResponse(BaseModel):
    """Accepted matches plus the record of what was examined and found unclaimable."""

    jurisdiction: Jurisdiction
    status: str
    needs_analyst_review: bool
    total_duty_allocated: Decimal
    total_refund: Decimal
    currency: str
    matches: list[dict[str, Any]]
    rejections: list[RejectionOut]
    candidate_pairs: int
    wall_time_seconds: float
    detail: str


def _to_response(result: MatchResult) -> MatchResponse:
    profile = profile_for(result.jurisdiction)
    return MatchResponse(
        jurisdiction=result.jurisdiction,
        status=result.status.value,
        needs_analyst_review=result.needs_analyst_review,
        total_duty_allocated=result.total_duty_allocated,
        total_refund=result.total_refund,
        currency=profile.currency.value,
        matches=[m.model_dump(mode="json") for m in result.matches],
        rejections=[
            RejectionOut(
                export_line_id=r.export_line_id,
                import_line_id=r.import_line_id,
                code=r.code.value,
                citation=r.citation,
                detail=r.detail,
            )
            for r in result.rejections
        ],
        candidate_pairs=result.candidate_pairs,
        wall_time_seconds=round(result.wall_time_seconds, 4),
        detail=result.detail,
    )


@router.post("/run", response_model=MatchResponse)
async def run_matching(body: MatchRequestBody) -> MatchResponse:
    """Match import lines to export lines under the claim's own statute.

    Lines whose jurisdiction disagrees with the request are rejected rather than
    coerced: a US line silently matched under GCC rules would produce a plausible
    allocation with no statutory basis.
    """
    # Iterated as two homogeneous passes rather than one merged tuple: EntryLine and
    # ExportLine share no base beyond BaseModel, so a single loop over both would be
    # typed at BaseModel and lose the very fields being checked.
    mismatched = [
        str(line.line_id) for line in body.imports if line.jurisdiction is not body.jurisdiction
    ] + [str(line.line_id) for line in body.exports if line.jurisdiction is not body.jurisdiction]
    if mismatched:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "jurisdiction_mismatch",
                "message": (
                    f"request is for {body.jurisdiction} but "
                    f"{len(mismatched)} line(s) carry a different jurisdiction"
                ),
                "line_ids": mismatched[:20],
            },
        )

    request = MatchRequest(
        imports=body.imports,
        exports=body.exports,
        profile=profile_for(body.jurisdiction),
        as_of=body.as_of or date.today(),
        time_limit_seconds=body.time_limit_seconds,
        claimant_is_importer_of_record=body.claimant_is_importer_of_record,
        proof_of_purchase=body.proof_of_purchase,
        boms=body.boms,
    )
    # The one span worth having on this path. Everything above it is validation; this is
    # a CP-SAT solve with a time limit, and "which claim was slow" is the question a
    # trace gets asked. `line_count` rather than the lines: a span is not a record, and
    # the record is the ledger.
    with tracer("drawbridge.matching").start_as_current_span("matching.run") as span:
        span.set_attribute("drawbridge.jurisdiction", str(body.jurisdiction))
        span.set_attribute("drawbridge.import_lines", len(body.imports))
        span.set_attribute("drawbridge.export_lines", len(body.exports))
        result = run_match(request)
        span.set_attribute("drawbridge.match_status", str(result.status))
    return _to_response(result)


@router.get("/strategies")
async def list_strategies() -> dict[str, Any]:
    """What each jurisdiction's matcher does, and under which provisions."""
    return {
        "us": {
            "strategy": "UsSubstitutionMatcher",
            "algorithm": "CP-SAT combinatorial allocation",
            "shape": "many-to-many",
            "theories": [
                "direct_identity",
                "hts_substitution",
                "manufacturing_direct_identity",
                "manufacturing_substitution",
            ],
            "objective": "maximise total refundable duty",
            "statute": "19 U.S.C. §1313(j) unused; §1313(a)/(b) manufacturing",
            "manufacturing": (
                "supply `boms` keyed by finished-good HTS. Component allocation is "
                "capped at quantity_per_unit / yield x exported quantity — the quantity "
                "actually used, which is what TFTEA ties designation to."
            ),
        },
        "ksa": {
            "strategy": "GccLinkageMatcher",
            "algorithm": "deterministic declaration-linkage trace",
            "shape": "one-to-many from a single import declaration",
            "theories": ["direct_identity", "declaration_linkage"],
            "objective": "none — the link is recorded on the document, not selected",
            "statute": "GCC Common Customs Law Art. 97; Rules of Implementation Art. 16",
            "gates": [
                "Art. 15(c) declaration link",
                "Art. 16 §2 USD 5,000 minimum",
                "Art. 16 §3(a) one Gregorian year from duty payment",
                "Art. 16 §3(b) six Gregorian months from re-export",
                "Art. 174 three-year absolute bar",
                "Art. 16 §5 unused and unaltered",
                "Art. 16 §4 single consignment",
                "Art. 16 §1 importer of record or proof of purchase",
            ],
        },
    }

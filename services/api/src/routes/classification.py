"""Classification endpoint — corroborating the codes a claim was declared under.

The pipeline does not pick tariff codes. The codes arrive on the entry and export lines
from the declarations that were actually filed, and changing one after the fact is a
customs matter, not a data-cleaning matter. What this endpoint does is check them: it
searches the schedule for each goods description and reports whether the declared code is
what the schedule's own text points to.

That distinction is the whole design. A classifier that *assigned* codes would put an
embedding model between a description and a duty rate. A classifier that *corroborates*
them turns a mismatch into a review item with the schedule text attached, which is a thing
an analyst can act on and an auditor can follow.

The embedding is computed here rather than taken from the caller, because the query and
the corpus must come from the same model — `mcp-hts` takes a vector precisely so it can
stay a pure function, and something has to be the place that produces one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from services.api.src.sync_db import in_thread
from services.classifier.src.embeddings import BACKENDS, DEFAULT_BACKEND, EmbeddingError, build
from services.classifier.src.search import LEXICAL_FLOOR, search_tariff

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

router = APIRouter(prefix="/classification", tags=["classification"])


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: str
    description: str = Field(min_length=1)
    declared_code: str = Field(min_length=6, max_length=12)


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jurisdiction: Annotated[str, Field(pattern="^(us|ksa)$")] = "us"
    lines: list[LineIn] = Field(min_length=1, max_length=200)
    revision: str | None = None
    backend: str = DEFAULT_BACKEND
    limit: Annotated[int, Field(ge=1, le=25)] = 5


def _agreement(declared: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """How far up the code the schedule search agrees with the declaration.

    Reported at three depths because they mean different things. Disagreement at the
    8-digit level is what decides a §1313(j)(2) substitution pairing; disagreement at the
    6-digit level means the search and the declaration are describing different goods, and
    that is a classification problem rather than a statistical-suffix problem.
    """
    codes = [c["code"] for c in candidates]
    return {
        "exact": declared in codes,
        "subheading_8": any(c[:8] == declared[:8] for c in codes),
        "hs6": any(c[:6] == declared[:6] for c in codes),
    }


@router.post("/run")
async def run_classification(body: ClassifyRequest) -> dict[str, Any]:
    """Check every line's declared code against the schedule.

    An embedding backend that cannot load is reported per line rather than raised: the
    lexical path still works, and a pipeline that halts because a model file is missing
    would be a worse outcome than one that classifies lexically and says so.
    """
    embedder = build(body.backend)
    ceiling = BACKENDS[body.backend].vector_ceiling

    def _work(session: Session) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for line in body.lines:
            try:
                vector: list[float] | None = embedder.embed([line.description])[0]
            except EmbeddingError:
                vector = None

            hits, method = search_tariff(
                session,
                query=line.description,
                jurisdiction=body.jurisdiction,
                embedding=vector,
                revision=body.revision,
                limit=body.limit,
                vector_ceiling=ceiling,
            )
            candidates = [hit.as_dict() for hit in hits]
            agreement = _agreement(line.declared_code, candidates)
            results.append(
                {
                    "line_id": line.line_id,
                    "declared_code": line.declared_code,
                    "method": method,
                    "embedded": vector is not None,
                    "candidates": candidates,
                    "agreement": agreement,
                    # No candidate at all is not agreement and not disagreement. It means
                    # the schedule slice being searched has nothing to say, which happens
                    # on a corpus that is ingested but not embedded.
                    "corroborated": bool(candidates) and agreement["subheading_8"],
                    "unsupported": bool(candidates) and not agreement["hs6"],
                }
            )

        return {
            "jurisdiction": body.jurisdiction,
            "backend": body.backend,
            "thresholds": {"lexical_floor": LEXICAL_FLOOR, "vector_ceiling": ceiling},
            "lines": results,
            "corroborated": sum(1 for r in results if r["corroborated"]),
            "unsupported": [r["line_id"] for r in results if r["unsupported"]],
        }

    return await in_thread(_work)

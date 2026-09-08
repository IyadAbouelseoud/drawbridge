"""USITC HTS schedule, ZATCA Integrated Customs Tariff, and CBP CROSS rulings.

Backed by pgvector over the tariff corpus. Every classification answer cites the schedule
revision it came from and, where available, the rulings that support it — a heading
without a ruling behind it is an opinion, and an opinion is what gets reclassified on
audit.

Search is hybrid: trigram lexical plus vector similarity, reported separately. A hit both
paths agree on is stronger than either alone, and a vector-only hit is flagged as needing
analyst confirmation rather than returned as an answer.

Exposed over streamable-HTTP on port 8102. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field
from sqlalchemy import text

from mcp_servers.mcp_claims.db import session_scope
from services.classifier.src.embeddings import BACKENDS, DEFAULT_BACKEND
from services.classifier.src.search import (
    CONFIRMATION_LEXICAL_FLOOR,
    LEXICAL_FLOOR,
    search_rulings,
    search_tariff,
)

server = MCPServer("mcp-hts")


def _fail(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}


@server.tool()
def ping() -> str:
    """Liveness probe."""
    return "mcp-hts ok"


@server.tool()
def classify(
    description: Annotated[str, Field(description="Goods description to classify")],
    jurisdiction: Annotated[str, Field(description="us | ksa")] = "us",
    revision: Annotated[
        str | None,
        Field(description="Schedule revision; omit for the current one"),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> dict[str, Any]:
    """Find candidate tariff classifications for a goods description.

    Lexical-only unless an embedding is supplied via `classify_with_embedding`. That is a
    supported mode rather than a degraded one: when the description is already tariff
    language, trigram matching against published text is the stronger signal.

    Hits are ranked but not chosen. Classification drives the duty rate, so the tool
    returns candidates with their evidence and leaves the decision to the analyst.
    """
    try:
        with session_scope() as session:
            hits, method = search_tariff(
                session,
                query=description,
                jurisdiction=jurisdiction,
                revision=revision,
                limit=limit,
            )
        return {
            "ok": True,
            "method": method,
            "count": len(hits),
            "candidates": [h.as_dict() for h in hits],
            "note": (
                "lexical only — no embedding supplied. Paraphrased or commercial "
                "descriptions may need classify_with_embedding."
            ),
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def classify_with_embedding(
    description: Annotated[str, Field(description="Goods description to classify")],
    embedding: Annotated[
        list[float],
        Field(description="Query embedding, 384 dimensions, same model as the corpus"),
    ],
    jurisdiction: Annotated[str, Field(description="us | ksa")] = "us",
    revision: Annotated[str | None, Field(description="Schedule revision")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
    backend: Annotated[
        str,
        Field(description="Backend that produced the embedding; sets the distance ceiling"),
    ] = DEFAULT_BACKEND,
) -> dict[str, Any]:
    """Hybrid classification over lexical and vector search.

    The embedding must come from the same model the corpus was embedded with; mixing
    models produces confident nonsense, because cosine distance between two different
    embedding spaces is meaningless rather than merely inaccurate.

    `backend` names which model produced it, and is what sets the distance ceiling. A
    threshold is only meaningful inside one embedding space, so naming the wrong backend
    does not skew the results slightly — it suppresses every hit or admits every one.
    """
    try:
        ceiling = BACKENDS[backend].vector_ceiling
    except KeyError:
        return _fail(ValueError(f"unknown embedding backend {backend!r}"))

    try:
        with session_scope() as session:
            hits, method = search_tariff(
                session,
                query=description,
                jurisdiction=jurisdiction,
                embedding=embedding,
                revision=revision,
                limit=limit,
                vector_ceiling=ceiling,
            )
        return {
            "ok": True,
            "method": method,
            "count": len(hits),
            "candidates": [h.as_dict() for h in hits],
            "thresholds": {
                "lexical_floor": LEXICAL_FLOOR,
                "vector_ceiling": ceiling,
                "confirmation_lexical_floor": CONFIRMATION_LEXICAL_FLOOR,
                "backend": backend,
            },
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def lookup_code(
    code: Annotated[str, Field(description="Tariff code, digits only")],
    jurisdiction: Annotated[str, Field(description="us | ksa")] = "us",
) -> dict[str, Any]:
    """The schedule line for an exact code, across all retained revisions.

    Revisions are returned newest first rather than collapsed: a claim is classified
    against the schedule in force on its entry date, so an older revision is the right
    answer for an older entry.
    """
    try:
        with session_scope() as session:
            rows = (
                session.execute(
                    text("""
                    SELECT code, description_en, description_ar, duty_rate_general,
                           duty_rate_special, unit_of_quantity, revision,
                           effective_from, effective_to, source
                    FROM tariff_lines
                    WHERE jurisdiction = :j AND code = :c
                    ORDER BY effective_from DESC
                """),
                    {"j": jurisdiction, "c": code},
                )
                .mappings()
                .all()
            )

        if not rows:
            return {
                "ok": True,
                "found": False,
                "detail": f"no {jurisdiction} tariff line {code} in the ingested corpus",
            }
        return {
            "ok": True,
            "found": True,
            "revisions": [
                {
                    **{
                        k: v
                        for k, v in dict(r).items()
                        if k not in {"effective_from", "effective_to"}
                    },
                    "effective_from": r["effective_from"].isoformat(),
                    "effective_to": r["effective_to"].isoformat() if r["effective_to"] else None,
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def compare_across_jurisdictions(
    hs6: Annotated[str, Field(description="6-digit harmonised subheading")],
) -> dict[str, Any]:
    """The US and KSA lines beneath one HS6 subheading.

    HS6 is the only part of a tariff code comparable across jurisdictions — everything
    beyond it is national. Joining on a longer code would silently pair unrelated lines,
    so this deliberately compares at six digits and no further.
    """
    try:
        with session_scope() as session:
            rows = (
                session.execute(
                    text("""
                    SELECT jurisdiction, code, description_en, description_ar,
                           duty_rate_general, revision
                    FROM tariff_lines
                    WHERE hs6 = :hs6
                    ORDER BY jurisdiction, code
                """),
                    {"hs6": hs6},
                )
                .mappings()
                .all()
            )

        grouped: dict[str, list[dict[str, Any]]] = {"us": [], "ksa": []}
        for row in rows:
            grouped[row["jurisdiction"]].append(dict(row))
        return {
            "ok": True,
            "hs6": hs6,
            "us": grouped["us"],
            "ksa": grouped["ksa"],
            "note": (
                "HS6 is the limit of cross-jurisdiction comparability; national "
                "subheadings below it are not equivalent."
            ),
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def find_rulings(
    query: Annotated[str, Field(description="Article description or issue")],
    jurisdiction: Annotated[str, Field(description="us | ksa")] = "us",
    code: Annotated[str | None, Field(description="Narrow to a code or its HS6")] = None,
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
) -> dict[str, Any]:
    """Classification rulings supporting a position.

    Superseded rulings are returned flagged rather than hidden: a claim filed while a
    ruling was good law relied on it, and an audit years later needs that visible.
    """
    try:
        with session_scope() as session:
            rulings = search_rulings(
                session, query=query, jurisdiction=jurisdiction, code=code, limit=limit
            )
        return {
            "ok": True,
            "count": len(rulings),
            "rulings": rulings,
            "current": sum(1 for r in rulings if r["is_current"]),
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def corpus_status() -> dict[str, Any]:
    """What has been ingested, and how much of it is embedded.

    Embedding coverage matters because vector search silently skips unembedded rows: a
    corpus that is 5% embedded will answer, and will answer badly, without saying so.
    """
    try:
        with session_scope() as session:
            lines = (
                session.execute(
                    text("""
                    SELECT jurisdiction, source, revision,
                           count(*) AS lines,
                           count(embedding) AS embedded
                    FROM tariff_lines
                    GROUP BY jurisdiction, source, revision
                    ORDER BY jurisdiction, source, revision
                """)
                )
                .mappings()
                .all()
            )
            rulings = (
                session.execute(
                    text("""
                    SELECT jurisdiction, source, count(*) AS rulings,
                           count(embedding) AS embedded
                    FROM tariff_rulings
                    GROUP BY jurisdiction, source
                """)
                )
                .mappings()
                .all()
            )

        return {
            "ok": True,
            "tariff_lines": [
                {
                    **dict(r),
                    "embedded_pct": (
                        round(100 * r["embedded"] / r["lines"], 1) if r["lines"] else 0.0
                    ),
                }
                for r in lines
            ],
            "rulings": [dict(r) for r in rulings],
        }
    except Exception as exc:
        return _fail(exc)


def main() -> None:
    # MCP SDK 2.x takes the bind address on run(), not on the constructor.
    server.run(transport="streamable-http", host="0.0.0.0", port=8102)


if __name__ == "__main__":
    main()

"""Immutable append-only audit trail.

Satisfies 19 CFR §163 (US) and GCC Common Customs Law Art. 175 / ZATCA five-year original
retention (KSA). Append only — no update, no delete.

The question this answers is not "what does the claim say" but "how did it come to say
that": which analyst decided what, on what reasoning, against which document, and in what
order. A refund figure without that chain is a number; with it, it is a position.

Exposed over streamable-HTTP on port 8105. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from pydantic import Field
from sqlalchemy import text

from mcp_servers.mcp_claims.db import session_scope
from services.api.src.analyst import claim_history, claim_summary
from services.api.src.ledger import entries_for_claim, verify_chain

server = MCPServer("mcp-ledger")


def _fail(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}


@server.tool()
def ping() -> str:
    """Liveness probe."""
    return "mcp-ledger ok"


@server.tool()
def claim_audit_trail(
    claim_id: Annotated[str, Field(description="Claim UUID")],
) -> dict[str, Any]:
    """Every state transition on a claim, with actor and reason, oldest first.

    This is what an auditor is shown. Each row names who moved the claim, from what state
    to what state, and why — the reason text on an analyst transition is the reasoning
    they were required to supply at the time.
    """
    try:
        with session_scope() as session:
            claim_uuid = UUID(claim_id)
            summary = claim_summary(session, claim_uuid)
            history = claim_history(session, claim_uuid)
        return {
            "ok": True,
            "claim": summary,
            "transition_count": len(history),
            "transitions": history,
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def decision_log(
    claim_id: Annotated[str | None, Field(description="Claim UUID; omit for tenant-wide")] = None,
    tenant_id: Annotated[str | None, Field(description="Tenant UUID")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Analyst decisions on review-queue exceptions, newest first.

    Includes the reasoning text verbatim. That text is the audit record: a decision
    recorded without it would be indistinguishable from an unexamined one, which is
    exactly what a customs authority asks about four years later.
    """
    try:
        with session_scope() as session:
            rows = (
                session.execute(
                    text("""
                    SELECT review_id, claim_id, tenant_id, reason, severity, summary,
                           citation, resolution, resolution_note, assigned_to,
                           created_at, resolved_at, payload
                    FROM review_queue
                    WHERE state = 'resolved'
                      AND (CAST(:claim_id AS text) IS NULL
                           OR claim_id = CAST(:claim_id AS uuid))
                      AND (CAST(:tenant_id AS text) IS NULL
                           OR tenant_id = CAST(:tenant_id AS uuid))
                    ORDER BY resolved_at DESC
                    LIMIT :limit
                """),
                    {"claim_id": claim_id, "tenant_id": tenant_id, "limit": limit},
                )
                .mappings()
                .all()
            )

        return {
            "ok": True,
            "count": len(rows),
            "decisions": [
                {
                    "review_id": str(r["review_id"]),
                    "claim_id": str(r["claim_id"]) if r["claim_id"] else None,
                    "reason": r["reason"],
                    "severity": r["severity"],
                    "summary": r["summary"],
                    "citation": r["citation"],
                    "resolution": r["resolution"],
                    "reasoning": r["resolution_note"],
                    "analyst": r["assigned_to"],
                    "queued_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "decided_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                    "valuation_override": (r["payload"] or {}).get("valuation_override"),
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def provenance_for_claim(
    claim_id: Annotated[str, Field(description="Claim UUID")],
) -> dict[str, Any]:
    """Source documents behind a claim's refund lines.

    Every figure in a claim traces to a document span. This walks the refund lines back to
    the entry and export lines that produced them, and out to the documents those were
    extracted from — the chain that makes "we read this off the Bayan" checkable rather
    than asserted.
    """
    try:
        with session_scope() as session:
            rows = (
                session.execute(
                    text("""
                    SELECT rl.refund_line_id, rl.theory, rl.quantity, rl.duty_component,
                           rl.refund_amount, rl.linked_import_declaration,
                           el.declaration_number AS import_declaration,
                           el.hts_code AS import_hts, el.provenance AS import_provenance,
                           xl.reference AS export_reference,
                           xl.hts_code AS export_hts, xl.provenance AS export_provenance
                    FROM refund_lines rl
                    JOIN entry_lines el ON el.line_id = rl.import_line_id
                    JOIN export_lines xl ON xl.line_id = rl.export_line_id
                    WHERE rl.claim_id = CAST(:claim_id AS uuid)
                    ORDER BY rl.refund_line_id
                """),
                    {"claim_id": claim_id},
                )
                .mappings()
                .all()
            )

        return {
            "ok": True,
            "claim_id": claim_id,
            "refund_line_count": len(rows),
            "lines": [
                {
                    "refund_line_id": str(r["refund_line_id"]),
                    "theory": r["theory"],
                    "quantity": str(r["quantity"]),
                    "duty_component": str(r["duty_component"]),
                    "refund_amount": str(r["refund_amount"]),
                    "import_declaration": r["import_declaration"],
                    "import_hts": r["import_hts"],
                    "export_reference": r["export_reference"],
                    "export_hts": r["export_hts"],
                    "linked_import_declaration": r["linked_import_declaration"],
                    "import_spans": (r["import_provenance"] or {}).get("spans", []),
                    "export_spans": (r["export_provenance"] or {}).get("spans", []),
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def retention_status(
    tenant_id: Annotated[str, Field(description="Tenant UUID")],
) -> dict[str, Any]:
    """Document retention posture against the statutory minimum.

    US: 19 CFR §163, five years from liquidation. KSA: Art. 175 permits the
    administration to destroy its own records after five years, and ZATCA Resolution
    28624 requires originals retained five years — so the obligation is ours regardless
    of what the authority keeps.
    """
    try:
        with session_scope() as session:
            rows = (
                session.execute(
                    text("""
                    SELECT kind, count(*) AS documents,
                           min(ingested_at) AS oldest,
                           max(ingested_at) AS newest
                    FROM documents
                    WHERE tenant_id = CAST(:tenant_id AS uuid)
                    GROUP BY kind
                    ORDER BY kind
                """),
                    {"tenant_id": tenant_id},
                )
                .mappings()
                .all()
            )

        return {
            "ok": True,
            "tenant_id": tenant_id,
            "retention_years": 5,
            "citations": [
                "19 CFR §163 (US)",
                "GCC Common Customs Law Art. 175 (GCC)",
                "ZATCA Resolution 28624 — five-year originals (KSA)",
            ],
            "documents": [
                {
                    "kind": r["kind"],
                    "documents": r["documents"],
                    "oldest": r["oldest"].isoformat() if r["oldest"] else None,
                    "newest": r["newest"].isoformat() if r["newest"] else None,
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return _fail(exc)


@server.tool()
def trace_figure(
    claim_id: Annotated[str, Field(description="Claim UUID")],
    field: Annotated[str, Field(description="Figure name, e.g. 'duty_paid', 'entered_value'")],
    line_id: Annotated[str | None, Field(description="Restrict to one entry/export line")] = None,
) -> dict[str, Any]:
    """Where one figure on a claim came from: which document, which page, which box.

    This is the tool the recordkeeping obligation actually reduces to. 19 CFR §163 and
    GCC Art. 175 both come down to producing, on request, the record that supports a
    figure — and "it is on the *Bayan* somewhere" is not that record.

    Two sources are consulted and both are returned. `ledger` is the append-only copy
    written when the claim was persisted; `live` is the current `provenance` column on the
    line. They should agree. When they do not, `consistent` is false and both are shown
    rather than one being preferred, because which of them is wrong is the finding — a
    corrected extraction and an altered record look identical from one side.
    """
    try:
        with session_scope() as session:
            claim_uuid = UUID(claim_id)
            ledger_hits: list[dict[str, Any]] = []
            for entry in entries_for_claim(session, claim_uuid):
                if entry.event_type != "figure_traced":
                    continue
                payload = entry.payload or {}
                if line_id and payload.get("line_id") != line_id:
                    continue
                span = (payload.get("figures") or {}).get(field)
                if span is None:
                    continue
                ledger_hits.append(
                    {
                        "line_id": payload.get("line_id"),
                        "sequence": entry.sequence,
                        "recorded_at": entry.recorded_at.isoformat(),
                        "actor": entry.actor,
                        **span,
                    }
                )

            live_hits = _live_spans(session, claim_uuid, field, line_id)

        consistent = _spans_agree(ledger_hits, live_hits)
        return {
            "ok": True,
            "claim_id": claim_id,
            "field": field,
            "found": bool(ledger_hits or live_hits),
            "consistent": consistent,
            "ledger": ledger_hits,
            "live": live_hits,
            "citations": ["19 CFR §163 (US)", "GCC Common Customs Law Art. 175 (GCC)"],
        }
    except Exception as exc:
        return _fail(exc)


def _live_spans(
    session: Any, claim_id: UUID, field: str, line_id: str | None
) -> list[dict[str, Any]]:
    """The same figure read off the working line rows rather than off the ledger."""
    rows = (
        session.execute(
            text("""
            SELECT el.line_id, el.provenance
            FROM refund_lines rl JOIN entry_lines el ON el.line_id = rl.import_line_id
            WHERE rl.claim_id = CAST(:claim_id AS uuid)
            UNION ALL
            SELECT xl.line_id, xl.provenance
            FROM refund_lines rl JOIN export_lines xl ON xl.line_id = rl.export_line_id
            WHERE rl.claim_id = CAST(:claim_id AS uuid)
        """),
            {"claim_id": str(claim_id)},
        )
        .mappings()
        .all()
    )

    hits: list[dict[str, Any]] = []
    for row in rows:
        if line_id and str(row["line_id"]) != line_id:
            continue
        span = ((row["provenance"] or {}).get("figures") or {}).get(field)
        if span is None:
            continue
        hits.append(
            {
                "line_id": str(row["line_id"]),
                "document_id": span.get("document_id"),
                "document_sha256": span.get("document_sha256"),
                "page": span.get("page"),
                "bbox": [span.get("x0"), span.get("y0"), span.get("x1"), span.get("y1")],
                "raw_text": span.get("raw_text"),
                "extractor": span.get("extractor"),
            }
        )
    return hits


def _spans_agree(ledger: list[dict[str, Any]], live: list[dict[str, Any]]) -> bool:
    """Whether every box the claim currently shows is one the ledger recorded.

    Containment, not equality, and the asymmetry is the point. The ledger holds a row for
    every line that was *persisted*; the live view walks the claim's refund lines, so it
    shows only the lines that were actually claimed. A GCC claim whose second re-export
    fell under the Art. 16 §2 minimum legitimately has a ledger entry with no live
    counterpart — that entry is the record of a line considered and excluded, which is
    something an audit wants rather than a discrepancy.

    What containment still catches is the case that matters: a figure on the claim whose
    box is not the box the ledger recorded, or is not in the ledger at all.

    Compared on the tuple that identifies a location — line, document hash, page, box —
    and not on the whole record, because `raw_text` may legitimately be absent from one
    side while the location is identical.
    """

    def key(rows: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
        return {
            (r.get("line_id"), r.get("document_sha256"), r.get("page"), tuple(r.get("bbox") or ()))
            for r in rows
        }

    return key(live) <= key(ledger)


@server.tool()
def ledger_chain(
    tenant_id: Annotated[str, Field(description="Tenant UUID")],
) -> dict[str, Any]:
    """Verify the tenant's hash chain and report the first break.

    The triggers on `audit_ledger` stop the application from rewriting history. This
    checks whether history was rewritten anyway — around the application, by someone with
    database access — which is the case the triggers cannot cover and the one an auditor
    is entitled to ask about.
    """
    try:
        with session_scope() as session:
            result = verify_chain(session, UUID(tenant_id))
        return {"ok": True, "tenant_id": tenant_id, **result}
    except Exception as exc:
        return _fail(exc)


@server.tool()
def claim_ledger(
    claim_id: Annotated[str, Field(description="Claim UUID")],
) -> dict[str, Any]:
    """Every ledger event touching one claim, in the order the database assigned.

    Ordered by `sequence` rather than by timestamp: several events land inside one
    transaction and share a `recorded_at` to the microsecond, and their order is exactly
    what an audit of an override asks about.
    """
    try:
        with session_scope() as session:
            entries = entries_for_claim(session, UUID(claim_id))
            rows = [
                {
                    "sequence": e.sequence,
                    "event_type": e.event_type,
                    "actor": e.actor,
                    "subject": e.subject,
                    "document_sha256": e.document_sha256,
                    "recorded_at": e.recorded_at.isoformat(),
                    "entry_hash": e.entry_hash,
                    "payload": e.payload,
                }
                for e in entries
            ]
        return {"ok": True, "claim_id": claim_id, "count": len(rows), "events": rows}
    except Exception as exc:
        return _fail(exc)


def main() -> None:
    # MCP SDK 2.x takes the bind address on run(), not on the constructor.
    server.run(transport="streamable-http", host="0.0.0.0", port=8105)


if __name__ == "__main__":
    main()

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


def main() -> None:
    # MCP SDK 2.x takes the bind address on run(), not on the constructor.
    server.run(transport="streamable-http", host="0.0.0.0", port=8105)


if __name__ == "__main__":
    main()

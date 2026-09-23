"""Document store: upload, fetch, span-addressable retrieval.

MinIO-backed. Objects are immutable and content-addressed; a correction writes a new
object. See mcp_servers/mcp_docs/store.py for the invariants.

Exposed over streamable-HTTP on port 8103 so the pipeline, n8n, and a human analyst in
Claude Code all reach the same tools. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

import base64
import os
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from drawbridge_schemas.agents import Scope
from drawbridge_schemas.provenance import DocumentKind, DocumentRef, Language, Span
from mcp_servers.mcp_docs.store import (
    DocumentNotFoundError,
    DocumentStore,
    StoreConfig,
    tenant_owns,
)
from mcp_servers.security import READ_ONLY, WRITES, guarded, run, server_kwargs
from services.api.src.auth import authorise_tenant, current_principal
from services.api.src.config import get_settings
from services.api.src.telemetry import configure_tracing

server = MCPServer(
    "mcp-docs",
    **server_kwargs("agent:mcp-docs", get_settings().mcp_docs_url),
)


def _owned(object_key: str) -> bool:
    """Whether the caller may touch this object.

    A tenant user may touch only keys under their own prefix — until v1.1.0 any caller
    could fetch, presign or render any tenant's document by naming its key. A cross-tenant
    principal (the pipeline) and an in-process caller are not constrained here; they are
    constrained by what they can name.
    """
    principal = current_principal()
    if principal is None or principal.tenant_id is None:
        return True
    return tenant_owns(principal.tenant_id, object_key)


_NOT_YOURS = {"error": "not_found"}


@lru_cache(maxsize=1)
def _store() -> DocumentStore:
    store = DocumentStore(
        StoreConfig(
            endpoint_url=os.environ.get("DRAWBRIDGE_S3_ENDPOINT_URL", "http://minio:9000"),
            access_key=os.environ.get("DRAWBRIDGE_S3_ACCESS_KEY", "drawbridge"),
            secret_key=os.environ.get("DRAWBRIDGE_S3_SECRET_KEY", "drawbridge"),
            bucket=os.environ.get("DRAWBRIDGE_S3_BUCKET_DOCUMENTS", "drawbridge-documents"),
        )
    )
    return store


@server.tool(annotations=READ_ONLY)
@guarded(None)
def ping() -> str:
    """Liveness probe."""
    return "mcp-docs ok"


@server.tool(annotations=WRITES)
@guarded(Scope.DOCUMENTS_WRITE, write=True)
def store_document(
    tenant_id: Annotated[str, Field(description="Tenant UUID")],
    kind: Annotated[str, Field(description="DocumentKind value, e.g. 'zatca_bayan'")],
    content_base64: Annotated[str, Field(description="Base64-encoded document bytes")],
    suffix: Annotated[str, Field(description="File extension, e.g. '.pdf'")] = ".pdf",
    language: Annotated[str, Field(description="'en', 'ar', or 'mixed'")] = "en",
) -> dict[str, Any]:
    """Store a document immutably and return its reference.

    Idempotent: identical bytes resolve to the same key and document_id, so a broker
    re-sending a file does not fork its identity.
    """
    tenant = authorise_tenant(UUID(tenant_id))
    data = base64.b64decode(content_base64, validate=True)
    ref = _store().put(
        tenant_id=tenant,
        kind=DocumentKind(kind),
        data=data,
        suffix=suffix,
        language=Language(language),
    )
    return ref.model_dump(mode="json")


@server.tool(annotations=READ_ONLY)
@guarded(Scope.DOCUMENTS_READ)
def fetch_document(
    document_ref: Annotated[dict[str, Any], Field(description="DocumentRef as JSON")],
) -> dict[str, Any]:
    """Fetch a document's bytes, verifying the content hash still matches.

    A hash mismatch is surfaced as an error rather than returned as content: a span
    pointing at changed content looks like evidence and is not.
    """
    ref = DocumentRef.model_validate(document_ref)
    if not _owned(ref.object_key):
        return {**_NOT_YOURS, "object_key": ref.object_key}
    try:
        data = _store().get(ref)
    except DocumentNotFoundError:
        return {"error": "not_found", "object_key": ref.object_key}
    return {
        "object_key": ref.object_key,
        "sha256": ref.sha256,
        "size_bytes": len(data),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


@server.tool(annotations=READ_ONLY)
@guarded(Scope.DOCUMENTS_READ)
def read_span_image(
    span: Annotated[dict[str, Any], Field(description="Span as JSON")],
) -> dict[str, Any]:
    """Render the page region a span points at, as a PNG.

    This is what makes provenance checkable rather than merely recorded: an analyst
    reviewing a duty figure gets the crop it was read from, not a page number to hunt
    through.
    """
    parsed = Span.model_validate(span)
    if not _owned(parsed.document.object_key):
        return _NOT_YOURS
    try:
        png = _store().read_span(parsed)
    except (ValueError, DocumentNotFoundError) as exc:
        return {"error": str(exc)}
    return {
        "page": parsed.page,
        "bbox": parsed.bbox,
        "image_base64": base64.b64encode(png).decode("ascii"),
        "media_type": "image/png",
    }


@server.tool(annotations=READ_ONLY)
@guarded(Scope.DOCUMENTS_READ)
def verify_span_text(
    span: Annotated[dict[str, Any], Field(description="Span as JSON")],
) -> dict[str, Any]:
    """Re-read the text inside a span and compare it with what was recorded.

    Answers the question an auditor is effectively asking: does the source document
    still say what the claim says it says?
    """
    parsed = Span.model_validate(span)
    if not _owned(parsed.document.object_key):
        return _NOT_YOURS
    try:
        current = _store().span_text(parsed)
    except (ValueError, DocumentNotFoundError) as exc:
        return {"error": str(exc)}
    recorded = (parsed.raw_text or "").strip()
    return {
        "recorded": recorded,
        "current": current,
        "matches": recorded == current,
    }


@server.tool(annotations=READ_ONLY)
@guarded(Scope.DOCUMENTS_READ)
def presign_document(
    document_ref: Annotated[dict[str, Any], Field(description="DocumentRef as JSON")],
    # Fifteen minutes at most, since v1.1.0. A presigned URL is a bearer credential for one
    # object that no kill switch can revoke; a day-long one outlives any incident response.
    expires_seconds: Annotated[int, Field(ge=60, le=900)] = 300,
) -> dict[str, str]:
    """Time-limited read URL, for handing a document to an analyst's browser."""
    ref = DocumentRef.model_validate(document_ref)
    if not _owned(ref.object_key):
        return {"error": "not_found"}
    url = _store().presigned_url(ref, timedelta(seconds=expires_seconds))
    return {"url": url, "expires_seconds": str(expires_seconds)}


@server.tool(annotations=READ_ONLY)
@guarded(Scope.DOCUMENTS_READ)
def list_tenant_documents(
    tenant_id: Annotated[str, Field(description="Tenant UUID")],
    kind: Annotated[str | None, Field(description="Optional DocumentKind filter")] = None,
) -> list[dict[str, Any]]:
    """List stored documents for a tenant, optionally filtered by kind."""
    tenant = authorise_tenant(UUID(tenant_id))
    return list(_store().list_for_tenant(tenant, DocumentKind(kind) if kind is not None else None))


def main() -> None:
    # A provider per process, so a tool call made on behalf of an API request lands in
    # the same trace. Without an exporter configured the spans are created and dropped —
    # see services/api/src/telemetry.py; the server starts either way.
    configure_tracing("drawbridge-mcp-docs", endpoint=get_settings().otel_exporter_endpoint)
    run(server, name="mcp-docs", host_alias="mcp-docs", port=8103)


if __name__ == "__main__":
    main()

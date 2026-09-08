"""Document intake and extraction — the front of the pipeline.

Two endpoints, and the boundary between them is the boundary between what is stored and
what is believed. `/documents/batch` puts bytes in MinIO and registers them; nothing about
their contents is asserted. `/extraction/run` reads them back and reports what could be
read and how confidently, and it is allowed to refuse.

**What `/extraction/run` does and does not produce.** It produces field readings with
confidence and the gate verdict over them. It does not produce typed `EntryLine` /
`ExportLine` objects from a scanned document: assembling those from a *Bayan* table needs
glyph x-coordinates to decide whether an Arabic run is stored in visual or logical order,
which is the work deliberately deferred since week 3 and stubbed in
`services/extraction/src/geometry.py`. Typed lines therefore arrive on the request from
the structured source — an ERP feed, a broker export — and this endpoint attaches the
document confidence to them.

That is not a placeholder standing in for the real thing. It is the actual division of
labour: the numbers come from a system of record, and the documents are what proves them,
which is what 19 CFR §163 asks for. The gate exists so a document that cannot be read
confidently stops the claim rather than decorating it.
"""

from __future__ import annotations

import base64
import binascii
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from drawbridge_schemas.provenance import Confidence, DocumentKind, DocumentRef, Language
from mcp_servers.mcp_docs.store import (
    DocumentNotFoundError,
    DocumentStore,
    StoreConfig,
    document_id_for,
)
from services.api.src.config import get_settings
from services.api.src.models import Document
from services.api.src.sync_db import in_thread
from services.extraction.src import native
from services.rules.src.triage import EXTRACTION_CONFIDENCE_FLOOR

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

router = APIRouter(tags=["documents"])


@lru_cache(maxsize=1)
def _store() -> DocumentStore:
    settings = get_settings()
    return DocumentStore(
        StoreConfig(
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket_documents,
        )
    )


class DocumentIn(BaseModel):
    """One document, either inline or already in the bucket.

    Inline bytes are the ordinary intake path. `object_key` covers the case where a broker
    or an earlier run already stored the file, so re-ingesting does not re-upload
    megabytes to arrive at the same content-addressed key.
    """

    model_config = ConfigDict(extra="forbid")

    kind: DocumentKind
    language: Language = Language.ENGLISH
    filename: str = ""
    content_base64: str | None = None
    object_key: str | None = None
    sha256: str | None = None
    page_count: int | None = None


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    documents: list[DocumentIn] = Field(min_length=1)


def _suffix(filename: str) -> str:
    return Path(filename).suffix or ".pdf"


def _register(session: Session, tenant_id: UUID, ref: DocumentRef) -> None:
    """Record the document if it is not already recorded.

    Identity is the content hash, so re-ingesting the same file is a no-op rather than a
    duplicate row. A broker who re-sends a corrected packet containing three unchanged
    attachments should not fork three documents.
    """
    if session.get(Document, ref.document_id) is not None:
        return
    session.add(
        Document(
            document_id=ref.document_id,
            tenant_id=tenant_id,
            kind=ref.kind.value,
            sha256=ref.sha256,
            object_key=ref.object_key,
            page_count=ref.page_count,
            language=ref.language.value,
        )
    )


@router.post("/documents/batch", status_code=status.HTTP_201_CREATED)
async def store_batch(body: BatchRequest) -> dict[str, Any]:
    """Put each document in MinIO and register it against the tenant.

    Storage happens before registration and both are content-addressed, so a crash between
    them leaves an orphan object rather than a row pointing at nothing. An orphan object
    costs storage; a row pointing at a missing object breaks every later span lookup and
    is discovered during an audit.
    """
    store = _store()
    refs: list[DocumentRef] = []

    for item in body.documents:
        if item.content_base64 is not None:
            try:
                data = base64.b64decode(item.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "bad_base64", "filename": item.filename},
                ) from exc
            refs.append(
                store.put(
                    tenant_id=body.tenant_id,
                    kind=item.kind,
                    data=data,
                    suffix=_suffix(item.filename),
                    language=item.language,
                    page_count=item.page_count,
                )
            )
        elif item.object_key and item.sha256:
            refs.append(
                DocumentRef(
                    document_id=document_id_for(item.sha256),
                    kind=item.kind,
                    sha256=item.sha256,
                    object_key=item.object_key,
                    page_count=item.page_count,
                    language=item.language,
                )
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "no_content",
                    "message": "supply content_base64, or object_key with sha256",
                },
            )

    def _work(session: Session) -> None:
        for ref in refs:
            _register(session, body.tenant_id, ref)

    await in_thread(_work)

    return {
        "tenant_id": str(body.tenant_id),
        "stored": [
            {
                "document_id": str(ref.document_id),
                "kind": ref.kind.value,
                "sha256": ref.sha256,
                "object_key": ref.object_key,
                "language": ref.language.value,
            }
            for ref in refs
        ],
    }


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    documents: list[UUID] = Field(min_length=1, description="document_ids from /documents/batch")
    floor: Annotated[float, Field(gt=0, le=1)] = EXTRACTION_CONFIDENCE_FLOOR


@router.post("/extraction/run")
async def run_extraction(body: ExtractionRequest) -> dict[str, Any]:
    """Read each stored document and report how confidently it was read.

    A native PDF carries its text; a scan does not, and the difference decides whether the
    confidence attached to a figure is a fact about the file or about an OCR model. The
    response says which for every document, because "0.98 confident" means something
    entirely different in each case.

    Documents are fetched one at a time and a failure on one does not discard the batch:
    an unreadable attachment among five is a reason to review that attachment, not to
    re-run extraction over four files that were fine.
    """
    store = _store()

    def _work(session: Session) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        confidences: list[Confidence] = []

        for document_id in body.documents:
            row = session.get(Document, document_id)
            if row is None:
                results.append(
                    {"document_id": str(document_id), "readable": False, "detail": "not registered"}
                )
                continue

            ref = DocumentRef(
                document_id=row.document_id,
                kind=DocumentKind(row.kind),
                sha256=row.sha256,
                object_key=row.object_key,
                page_count=row.page_count,
                language=Language(row.language),
            )
            try:
                data = store.get(ref)
            except DocumentNotFoundError:
                results.append(
                    {
                        "document_id": str(document_id),
                        "readable": False,
                        "detail": "registered but absent from the bucket",
                    }
                )
                continue

            # pdfplumber and pymupdf both want a path. The temporary file is written and
            # removed inside the request rather than cached, because a cached copy of a
            # customs document outside MinIO is a retention-policy hole nobody declared.
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(data)
                path = Path(handle.name)
            try:
                assessment = native.assess(path)
                lines = native.extract_lines(path) if assessment.usable else []
                labelled = native.find_labelled_values(lines) if lines else {}
            finally:
                path.unlink(missing_ok=True)

            confidence = Confidence(
                score=assessment.confidence,
                method="pdfplumber-native" if assessment.usable else "native-layer-unusable",
                # A scan is not a low-confidence reading, it is an unattempted one. Marking
                # it for review says so, instead of letting 0.0 read as a bad OCR result.
                needs_review=not assessment.usable,
            )
            confidences.append(confidence)
            results.append(
                {
                    "document_id": str(document_id),
                    "kind": row.kind,
                    "readable": True,
                    "native_text_usable": assessment.usable,
                    "assessment": assessment.reason,
                    "chars_per_page": round(assessment.chars_per_page, 1),
                    "fields_found": sorted(field.value for field in labelled),
                    "confidence": {
                        "score": confidence.score,
                        "method": confidence.method,
                        "needs_review": confidence.needs_review,
                    },
                    # A scanned document is not an error and not an answer. It routes to
                    # the OCR path, and the OCR path has its own floors.
                    "needs_ocr": not assessment.usable,
                }
            )

        below = [r for r in results if r.get("readable") and r["confidence"]["score"] < body.floor]
        return {
            "tenant_id": str(body.tenant_id),
            "documents": results,
            "floor": body.floor,
            "confidence_below_floor": len(below),
            "confidence_ok": not below and all(r.get("readable") for r in results),
            # Shaped for /triage/evaluate, which takes Confidence objects verbatim.
            "confidences": [c.model_dump(mode="json") for c in confidences],
        }

    return await in_thread(_work)

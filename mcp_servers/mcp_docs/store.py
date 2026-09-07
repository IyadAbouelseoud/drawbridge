"""MinIO-backed immutable document store.

Two invariants, both load-bearing for audit defence:

1. **Objects are immutable.** A correction writes a new object; it never mutates one in
   place. The bucket has versioning enabled (see docker-compose minio-init) so even an
   accidental overwrite is recoverable.
2. **Content addressing.** The object key embeds the SHA-256 of the bytes, so the same
   document ingested twice from two brokers occupies one object and one identity. This
   is what makes "every figure traces to a source span" checkable years later: the span
   references a hash, not a filename someone can change.

Retention: 19 CFR §163 (US) and GCC Art. 175 / ZATCA five-year originals (KSA).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from drawbridge_schemas.provenance import DocumentKind, DocumentRef, Language, Span

if TYPE_CHECKING:
    from collections.abc import Iterator

# Stable namespace so the same bytes always yield the same document_id across restarts
# and across tenants' ingest runs.
_DOCUMENT_NAMESPACE = UUID("6f9f8f5e-0f1a-4a3a-9c2b-1d7c4e8a2b60")

_MAX_INLINE_BYTES = 32 * 1024 * 1024


class DocumentNotFoundError(KeyError):
    """Raised when a key or hash has no object behind it."""


@dataclass(frozen=True, slots=True)
class StoreConfig:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str = "drawbridge-documents"
    region: str = "us-east-1"


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_id_for(sha256: str) -> UUID:
    """Deterministic id derived from content. Same bytes, same id, always."""
    return uuid5(_DOCUMENT_NAMESPACE, sha256)


def object_key_for(tenant_id: UUID, kind: DocumentKind, sha256: str, suffix: str) -> str:
    """Tenant-scoped, content-addressed key.

    Tenant prefix first so bucket policies and lifecycle rules can be written per tenant
    without touching the application.
    """
    return f"tenants/{tenant_id}/{kind.value}/{sha256}{suffix}"


class DocumentStore:
    """Immutable, content-addressed document storage over S3/MinIO."""

    def __init__(self, config: StoreConfig) -> None:
        self._config = config
        self._client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    # ------------------------------------------------------------------ writing

    def put(
        self,
        *,
        tenant_id: UUID,
        kind: DocumentKind,
        data: bytes,
        suffix: str = ".pdf",
        language: Language = Language.ENGLISH,
        page_count: int | None = None,
    ) -> DocumentRef:
        """Store bytes and return their reference.

        Idempotent by construction: re-storing identical bytes resolves to the same key
        and the same document_id, so a broker re-sending a file does not fork identity.
        """
        sha256 = content_hash(data)
        key = object_key_for(tenant_id, kind, sha256, suffix)

        if not self._exists(key):
            self._client.put_object(
                Bucket=self._config.bucket,
                Key=key,
                Body=data,
                ContentType=_content_type(suffix),
                Metadata={
                    "sha256": sha256,
                    "tenant-id": str(tenant_id),
                    "kind": kind.value,
                    "language": language.value,
                },
            )

        return DocumentRef(
            document_id=document_id_for(sha256),
            kind=kind,
            sha256=sha256,
            object_key=key,
            page_count=page_count,
            language=language,
        )

    def _exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._config.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    # ------------------------------------------------------------------ reading

    def get(self, ref: DocumentRef) -> bytes:
        """Fetch bytes and verify they still hash to what the reference claims.

        The verification is not paranoia. A span that points at a document whose content
        changed is worse than a missing span: it looks like evidence and is not.
        """
        data = self._get_key(ref.object_key)
        actual = content_hash(data)
        if actual != ref.sha256:
            msg = (
                f"content hash mismatch for {ref.object_key}: reference claims "
                f"{ref.sha256}, object is {actual}"
            )
            raise ValueError(msg)
        return data

    def _get_key(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._config.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey"}:
                raise DocumentNotFoundError(key) from exc
            raise
        body: bytes = response["Body"].read()
        return body

    def presigned_url(self, ref: DocumentRef, expires: timedelta) -> str:
        """Time-limited read URL, for handing a document to an analyst's browser."""
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._config.bucket, "Key": ref.object_key},
            ExpiresIn=int(expires.total_seconds()),
        )
        return url

    # ---------------------------------------------------- span-addressable read

    def read_span(self, span: Span) -> bytes:
        """Return the rendered image of the region a span points at.

        This is the operation that makes provenance *checkable* rather than merely
        recorded. An analyst reviewing a duty figure gets back the crop of the page it
        was read from, not a page number to go hunting through.

        Spans without a bbox came from a structured feed (EDI, CSV export) where
        `field_path` is the address; there is no image to render.
        """
        if span.bbox is None or span.page is None:
            msg = (
                "span has no page geometry; it came from a structured feed. Use "
                "field_path against the source record instead."
            )
            raise ValueError(msg)

        import pymupdf

        data = self.get(span.document)
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            page = doc[span.page - 1]
            clip = pymupdf.Rect(*span.bbox)
            pixmap = page.get_pixmap(clip=clip, dpi=200)
            png: bytes = pixmap.tobytes("png")
        return png

    def span_text(self, span: Span) -> str:
        """Return the text inside a span's rectangle, straight from the document.

        Compared against `Span.raw_text` this answers "does the document still say what
        we recorded?" — the check a CBP or ZATCA auditor is effectively performing.
        """
        if span.bbox is None or span.page is None:
            return span.raw_text or ""

        import pymupdf

        data = self.get(span.document)
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            page = doc[span.page - 1]
            text: str = page.get_textbox(pymupdf.Rect(*span.bbox))
        return text.strip()

    # ------------------------------------------------------------------ listing

    def list_for_tenant(
        self, tenant_id: UUID, kind: DocumentKind | None = None
    ) -> Iterator[dict[str, Any]]:
        prefix = f"tenants/{tenant_id}/"
        if kind is not None:
            prefix += f"{kind.value}/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._config.bucket, Prefix=prefix):
            for obj in page.get("Contents", ()):
                yield {
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }

    def ensure_bucket(self) -> None:
        """Create the bucket with versioning if it is absent.

        compose runs minio-init for this in development; this exists for on-prem
        deployments where the operator brings their own object store.
        """
        try:
            self._client.head_bucket(Bucket=self._config.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._config.bucket)
        self._client.put_bucket_versioning(
            Bucket=self._config.bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )


def _content_type(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".csv": "text/csv",
        ".json": "application/json",
        ".xml": "application/xml",
    }.get(suffix.lower(), "application/octet-stream")


__all__ = [
    "_MAX_INLINE_BYTES",
    "DocumentNotFoundError",
    "DocumentStore",
    "StoreConfig",
    "content_hash",
    "document_id_for",
    "object_key_for",
]

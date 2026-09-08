"""Provenance primitives.

Architectural invariant: every figure that reaches a claim traces to a span in a source
document. The LLM writes narratives and judgment calls; it never originates a number.
That rule is enforced here in the type system, not in a prompt.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentKind(StrEnum):
    """Source document classes Drawbridge ingests."""

    # United States
    CBP_7501 = "cbp_7501"
    ACE_ENTRY_SUMMARY = "ace_entry_summary"

    # Saudi Arabia / GCC
    ZATCA_BAYAN = "zatca_bayan"
    """KSA customs declaration. The GCC-lane analogue of the CBP 7501."""

    ZATCA_REEXPORT_DECLARATION = "zatca_reexport_declaration"
    """Carries the linked import declaration number per GCC Rules of Impl. Art. 15(c)."""

    GCC_CERTIFICATE_OF_ORIGIN = "gcc_certificate_of_origin"
    GCC_NATIONAL_CERTIFICATE = "gcc_national_certificate"
    """Issued by the competent authority in the GCC country of origin (MD 3852)."""

    VALUE_ADDED_CERTIFICATE = "value_added_certificate"
    """Local value-added percentage, certified by a licensed public accountant in KSA."""

    BANK_GUARANTEE = "bank_guarantee"
    COMMERCIAL_INVOICE = "commercial_invoice"
    PACKING_LIST = "packing_list"
    BILL_OF_LADING = "bill_of_lading"
    AIR_WAYBILL = "air_waybill"
    PROOF_OF_EXPORT = "proof_of_export"
    DESTRUCTION_CERTIFICATE = "destruction_certificate"
    ERP_SKU_MASTER = "erp_sku_master"
    BILL_OF_MATERIALS = "bill_of_materials"
    EDI_350 = "edi_350"
    EDI_309 = "edi_309"


class Language(StrEnum):
    """Script/language of an extracted region.

    MIXED is the common case for a ZATCA Bayan: Arabic labels, Latin HS codes, and
    numerals that may be Arabic-Indic or ASCII within the same document.
    """

    ENGLISH = "en"
    ARABIC = "ar"
    MIXED = "mixed"


class DocumentRef(BaseModel):
    """Pointer to an immutable object in the document store."""

    model_config = ConfigDict(frozen=True)

    document_id: UUID
    kind: DocumentKind
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    object_key: str = Field(description="MinIO/S3 key; the object is never mutated in place")
    page_count: int | None = None
    language: Language = Language.ENGLISH


class Span(BaseModel):
    """Addressable region of a source document.

    `page` is 1-indexed. `bbox` is (x0, top, x1, bottom) in PDF points, origin top-left,
    matching pdfplumber's coordinate convention. Absent bbox means the value came from a
    structured feed (EDI, CSV export) where `field_path` is the address instead.
    """

    model_config = ConfigDict(frozen=True)

    document: DocumentRef
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    field_path: str | None = Field(
        default=None, description="Dotted path for structured sources, e.g. 'lines[3].hts'"
    )
    raw_text: str | None = Field(default=None, description="Verbatim text as it appears")
    language: Language | None = Field(
        default=None,
        description="Script of this span. Set on the GCC lane so an Arabic-sourced figure "
        "is distinguishable from a Latin-sourced one during review.",
    )


STRUCTURED_KINDS: frozenset[DocumentKind] = frozenset(
    {
        DocumentKind.ERP_SKU_MASTER,
        DocumentKind.BILL_OF_MATERIALS,
        DocumentKind.EDI_350,
        DocumentKind.EDI_309,
    }
)
"""Sources that have records rather than pages.

A figure from one of these is addressed by `field_path` because there is no page to point
at. Everything else is a document a human can be handed, and a figure read off it must
carry the coordinates it was read from — see `ProvenanceSpan`.
"""


class ProvenanceSpan(BaseModel):
    """Exact origin of one figure: which bytes, which page, which box.

    Distinct from `Span`, which addresses a region loosely enough to describe a whole
    record. This is the strict form and every field on it is mandatory, because it exists
    to answer one question years after the fact — *where did this number come from* — and
    a partially-populated answer to that question is not an answer.

    `document_sha256` rather than only `document_id`: the id is ours and the hash is the
    document's. An auditor holding a PDF can verify the hash without access to our
    database, which is what makes the trace checkable rather than asserted.

    Coordinates are PDF points, origin top-left, matching `Span.bbox` and
    `extraction.geometry.Cell.bbox`.
    """

    model_config = ConfigDict(frozen=True)

    document_id: UUID
    document_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page: Annotated[int, Field(ge=1)]
    x0: float
    y0: float
    x1: float
    y1: float

    field_path: str | None = Field(
        default=None, description="Canonical field this box was read as, e.g. 'duty_paid'"
    )
    raw_text: str | None = Field(default=None, description="Verbatim text inside the box")
    language: Language | None = None
    extractor: str = Field(
        default="unknown",
        description="What produced the box: 'native-labelled', 'geometry-table', 'ocr'",
    )

    @model_validator(mode="after")
    def _box_is_non_degenerate(self) -> ProvenanceSpan:
        """A zero-width or inverted box cites nothing.

        Cheap to write by accident — a swapped pair of coordinates, a cell whose glyphs
        were all dropped — and impossible to notice downstream, where it renders as a
        span that simply highlights nothing when an analyst clicks it.
        """
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            msg = (
                f"degenerate bbox ({self.x0}, {self.y0}, {self.x1}, {self.y1}): "
                "x1 must exceed x0 and y1 must exceed y0"
            )
            raise ValueError(msg)
        return self

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


class Confidence(BaseModel):
    """Extraction confidence with the method that produced it."""

    model_config = ConfigDict(frozen=True)

    score: Annotated[float, Field(ge=0.0, le=1.0)]
    method: str = Field(description="e.g. 'pdfplumber-native', 'paddleocr', 'claude-vision'")
    needs_review: bool = Field(
        default=False,
        description="True routes the containing record to ANALYST_REVIEW regardless of score",
    )


class Provenance(BaseModel):
    """Binds a value to where it came from and how sure we are.

    Attach to every extracted field. A claim that fails `has_full_provenance` must not
    reach PACKAGED.
    """

    model_config = ConfigDict(frozen=True)

    spans: tuple[Span, ...] = Field(min_length=1)
    confidence: Confidence
    derived_from: tuple[str, ...] = Field(
        default=(),
        description="Field paths this value was computed from, for derivation trails",
    )
    figures: dict[str, ProvenanceSpan] = Field(
        default_factory=dict,
        description="Field name -> the exact box the figure was read from",
    )

    @property
    def cites_a_paginated_document(self) -> bool:
        """Whether any span points at something with pages rather than records.

        This is what decides how strictly `figures` is enforced upstream: a claim built
        from an EDI feed has no boxes to cite, and a claim built from a 7501 or a *Bayan*
        has no excuse not to.
        """
        return any(span.document.kind not in STRUCTURED_KINDS for span in self.spans)

    def figure(self, name: str) -> ProvenanceSpan | None:
        """The box behind one figure, or None if it was never recorded."""
        return self.figures.get(name)

    @model_validator(mode="after")
    def _figures_agree_with_spans(self) -> Provenance:
        """Every figure box must sit on a document this provenance already cites.

        Without the check a figure could name a hash that appears nowhere else on the
        record, and `trace_figure` would happily return it — a provenance chain whose
        last link points outside the chain.
        """
        known = {span.document.sha256 for span in self.spans}
        stray = sorted(
            {name for name, span in self.figures.items() if span.document_sha256 not in known}
        )
        if stray:
            msg = (
                f"figure span(s) {stray} cite a document not among this record's spans; "
                "a figure cannot come from a document the record does not reference"
            )
            raise ValueError(msg)
        return self

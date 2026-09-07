"""Provenance primitives.

Architectural invariant: every figure that reaches a claim traces to a span in a source
document. The LLM writes narratives and judgment calls; it never originates a number.
That rule is enforced here in the type system, not in a prompt.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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

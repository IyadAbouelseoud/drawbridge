"""Shared fixtures.

`tests/golden/` holds known-answer claims. Those must reproduce to the cent — they are
the regression barrier for the matcher, and they are why refunds are Decimal, not float.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Language,
    Provenance,
    ProvenanceSpan,
    Span,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode

TENANT = UUID("00000000-0000-0000-0000-0000000000a1")

# Every figure either schema can be asked to evidence. Fixtures hand the full set rather
# than the subset a given line happens to populate: the validator only demands a box for a
# figure that is actually stated, and a fixture that tracked which those were would need
# updating every time a test changed a number.
ALL_FIGURES = (
    "quantity",
    "entered_value",
    "duty_paid",
    "mpf_paid",
    "hmf_paid",
    "section_301_duty",
    "other_duty",
    "vat_paid",
    "excise_paid",
    "declared_value",
)


def figure_spans(
    ref: DocumentRef,
    *,
    names: tuple[str, ...] = ALL_FIGURES,
    page: int = 1,
    extractor: str = "native-labelled",
) -> dict[str, ProvenanceSpan]:
    """Distinct, non-overlapping boxes — one per figure.

    Distinct on purpose. Handing every figure the same rectangle would satisfy the schema
    and defeat the point: a trace that returns the same box for the duty and the VAT has
    not located either of them, and a test built on one would pass while the mechanism it
    covers did nothing.
    """
    return {
        name: ProvenanceSpan(
            document_id=ref.document_id,
            document_sha256=ref.sha256,
            page=page,
            x0=72.0,
            y0=100.0 + index * 14.0,
            x1=240.0,
            y1=112.0 + index * 14.0,
            field_path=name,
            extractor=extractor,
        )
        for index, name in enumerate(names)
    }


@pytest.fixture
def document_ref() -> DocumentRef:
    return DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.CBP_7501,
        sha256="a" * 64,
        object_key="tenants/a1/7501/sample.pdf",
        page_count=2,
    )


@pytest.fixture
def bayan_ref() -> DocumentRef:
    return DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.ZATCA_BAYAN,
        sha256="b" * 64,
        object_key="tenants/a1/zatca_bayan/sample.pdf",
        page_count=1,
        language=Language.MIXED,
    )


@pytest.fixture
def provenance(document_ref: DocumentRef) -> Provenance:
    return Provenance(
        spans=(Span(document=document_ref, page=1, bbox=(72.0, 120.0, 240.0, 134.0)),),
        confidence=Confidence(score=0.98, method="pdfplumber-native"),
        figures=figure_spans(document_ref),
    )


@pytest.fixture
def bayan_provenance(bayan_ref: DocumentRef) -> Provenance:
    return Provenance(
        spans=(
            Span(
                document=bayan_ref,
                page=1,
                bbox=(300.0, 140.0, 470.0, 154.0),
                language=Language.ARABIC,
            ),
        ),
        confidence=Confidence(score=0.95, method="pymupdf-native"),
        figures=figure_spans(bayan_ref, extractor="geometry-table"),
    )


@pytest.fixture
def entry_line(provenance: Provenance) -> EntryLine:
    """US import line. Section 301 duty, no ad valorem — the post-2024 shape."""
    return EntryLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        declaration_number="ABC-1234567-8",
        line_number=1,
        import_date=date(2023, 3, 14),
        declaration_date=date(2023, 3, 20),
        port_of_entry="2704",
        country_of_origin="CN",
        hts=HTSCode(code="8471300100"),
        description="Portable automatic data processing machines",
        quantity=Decimal("1000"),
        unit_of_measure="NO",
        entered_value=Decimal("250000.00"),
        duty_paid=Decimal("0.00"),
        section_301_duty=Decimal("62500.00"),
        mpf_paid=Decimal("864.00"),
        provenance=provenance,
    )


@pytest.fixture
def ksa_entry_line(bayan_provenance: Provenance) -> EntryLine:
    """KSA import line from a ZATCA Bayan.

    Declaration and duty-payment dates differ by 24 days, which is the case that makes
    the GCC clock anchor matter: ZATCA permits payment postponement up to 30 days, and
    the Art. 16 §3(a) re-export window runs from payment, not from the declaration.
    """
    return EntryLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.KSA,
        currency=Currency.SAR,
        declaration_number="20240115447821",
        line_number=1,
        import_date=date(2024, 1, 15),
        declaration_date=date(2024, 1, 15),
        duty_payment_date=date(2024, 2, 8),
        port_of_entry="Jeddah Islamic Port",
        country_of_origin="CN",
        hts=HTSCode(code="84713000"),
        description="Portable data processing machines",
        quantity=Decimal("1200"),
        unit_of_measure="PCE",
        entered_value=Decimal("937500.00"),
        duty_paid=Decimal("46875.00"),
        vat_paid=Decimal("147656.25"),
        provenance=bayan_provenance,
    )


@pytest.fixture
def export_line(provenance: Provenance) -> ExportLine:
    return ExportLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.US,
        reference="MAEU123456789",
        line_number=1,
        export_date=date(2024, 1, 9),
        destination_country="DE",
        hts=HTSCode(code="8471300150"),
        description="Portable ADP machines, re-exported unused",
        quantity=Decimal("400"),
        unit_of_measure="NO",
        declared_value=Decimal("100000.00"),
        provenance=provenance,
    )


@pytest.fixture
def ksa_export_line(bayan_provenance: Provenance) -> ExportLine:
    """KSA re-export carrying its linked import declaration (Art. 15(c))."""
    return ExportLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.KSA,
        reference="RE-20240912-0031",
        line_number=1,
        export_date=date(2024, 9, 12),
        destination_country="AE",
        hts=HTSCode(code="84713000"),
        description="Portable data processing machines, re-exported unused",
        quantity=Decimal("500"),
        unit_of_measure="PCE",
        declared_value=Decimal("390625.00"),
        linked_import_declaration="20240115447821",
        consignment_id="CNS-2024-0115-A",
        unused_and_unaltered=True,
        provenance=bayan_provenance,
    )

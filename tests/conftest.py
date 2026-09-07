"""Shared fixtures.

`tests/golden/` holds known-answer claims. Those must reproduce to the cent — they are
the regression barrier for the matcher, and they are why refunds are Decimal, not float.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Provenance,
    Span,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode

TENANT = UUID("00000000-0000-0000-0000-0000000000a1")


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
def provenance(document_ref: DocumentRef) -> Provenance:
    return Provenance(
        spans=(Span(document=document_ref, page=1, bbox=(72.0, 120.0, 240.0, 134.0)),),
        confidence=Confidence(score=0.98, method="pdfplumber-native"),
    )


@pytest.fixture
def entry_line(provenance: Provenance) -> EntryLine:
    return EntryLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        entry_number="ABC-1234567-8",
        line_number=1,
        import_date=date(2023, 3, 14),
        entry_summary_date=date(2023, 3, 20),
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
def export_line(provenance: Provenance) -> ExportLine:
    return ExportLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        reference="MAEU123456789",
        line_number=1,
        export_date=date(2024, 1, 9),
        destination_country="DE",
        hts=HTSCode(code="8471300150"),
        description="Portable ADP machines, re-exported unused",
        quantity=Decimal("400"),
        unit_of_measure="NO",
        provenance=provenance,
    )

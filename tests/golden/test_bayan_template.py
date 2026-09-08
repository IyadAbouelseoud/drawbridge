"""The template engine, against the same synthetic *Bayan* the geometry suite reads.

Week 10 ended with cells and boxes and no names. These tests cover the join: a cell at a
position becomes `duty_amount`, and the box it was measured from becomes the provenance
span that `EntryLine` demands before it will state the figure at all.

Parametrised over both storage orders for the same reason `test_rtl_geometry` is — the
template sits on top of geometry, and a regression that made the reconstruction consult
the character stream would surface here as a duty of ٠٠٥٧٨٦٤ typed without complaint.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from drawbridge_schemas.provenance import DocumentKind, Language
from services.extraction.src import geometry, templates
from tests.fixtures.bayan import (
    BAYAN_DECLARATION,
    DECLARATION_PITCH,
    Storage,
    bayan_pdf,
)

if TYPE_CHECKING:
    from pathlib import Path

BAYAN_SHA = "c" * 64


def _render(rows: list[list[str]], tmp_path: Path, storage: Storage, name: str) -> Path:
    path = tmp_path / f"{name}.pdf"
    path.write_bytes(bayan_pdf(rows, storage=storage, pitch=DECLARATION_PITCH))
    return path


@pytest.fixture(params=[Storage.VISUAL, Storage.LOGICAL], ids=["visual-stream", "logical-stream"])
def declaration(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    return _render(BAYAN_DECLARATION, tmp_path, request.param, "declaration")


@pytest.fixture
def typed(declaration: Path) -> templates.TemplateResult:
    rows = geometry.extract_table(declaration, 1)
    return templates.apply_template(templates.BAYAN_LINE_TABLE, rows)


class TestACellBecomesAField:
    def test_the_duty_column_is_typed_as_duty_amount(self, typed: templates.TemplateResult) -> None:
        """The headline claim: a rectangle on a scanned Arabic form is now a named figure."""
        duty = typed.rows[0]["duty_amount"]
        assert duty.kind is templates.FieldKind.AMOUNT
        assert duty.decimal == Decimal("46875.00")
        assert duty.target == "duty_paid"

    def test_every_line_reads_to_the_cent(self, typed: templates.TemplateResult) -> None:
        assert [row["duty_amount"].decimal for row in typed.rows] == [
            Decimal("46875.00"),
            Decimal("4250.00"),
            Decimal("6250.00"),
        ]
        assert [row["declared_value"].decimal for row in typed.rows] == [
            Decimal("937500.00"),
            Decimal("85000.00"),
            Decimal("125000.00"),
        ]
        assert [row["quantity"].decimal for row in typed.rows] == [
            Decimal("1200"),
            Decimal("340"),
            Decimal("500"),
        ]

    def test_the_hs_code_survives_the_arabic_row_it_sits_in(
        self, typed: templates.TemplateResult
    ) -> None:
        """84713000 and not 00031748.

        The reversal that would produce the second is well-formed, classifiable, and
        points at a different chapter — see `geometry`'s module docstring.
        """
        assert [row["hs_code"].value for row in typed.rows] == [
            "84713000",
            "84433100",
            "85285200",
        ]

    def test_the_header_row_is_consumed_not_returned(self, typed: templates.TemplateResult) -> None:
        assert len(typed.rows) == len(BAYAN_DECLARATION) - 1
        assert typed.clean, typed.issues


class TestTheHeadingIsTheAuthority:
    def test_binding_reports_the_columns_it_actually_used(
        self, typed: templates.TemplateResult
    ) -> None:
        assert typed.binding == {
            "line_number": 0,
            "hs_code": 1,
            "description": 2,
            "quantity": 3,
            "declared_value": 4,
            "duty_amount": 5,
        }

    def test_a_reordered_form_binds_by_heading_rather_than_by_index(self, tmp_path: Path) -> None:
        """The revision case, and the reason indices alone are not enough.

        Value and duty are swapped on the page. A template that trusted its own indices
        would report a duty of 937,500.00 on a line that paid 46,875.00 — a figure that
        is well-formed, reconciles against nothing, and is claimed.
        """
        swapped = [[*row[:4], row[5], row[4]] for row in BAYAN_DECLARATION]
        path = _render(swapped, tmp_path, Storage.VISUAL, "swapped")

        result = templates.apply_template(
            templates.BAYAN_LINE_TABLE, geometry.extract_table(path, 1)
        )
        assert result.binding["duty_amount"] == 4
        assert result.binding["declared_value"] == 5
        assert result.rows[0]["duty_amount"].decimal == Decimal("46875.00")

    def test_a_missing_column_is_refused_rather_than_approximated(self, tmp_path: Path) -> None:
        without_duty = [row[:5] for row in BAYAN_DECLARATION]
        path = _render(without_duty, tmp_path, Storage.VISUAL, "no-duty")

        with pytest.raises(templates.TemplateError, match="duty_amount"):
            templates.apply_template(templates.BAYAN_LINE_TABLE, geometry.extract_table(path, 1))

    def test_without_a_header_row_the_indices_are_all_there_is(self, tmp_path: Path) -> None:
        """Explicit, because it is the weaker mode and a caller should have chosen it."""
        path = _render(BAYAN_DECLARATION[1:], tmp_path, Storage.VISUAL, "headless")
        headless = replace(templates.BAYAN_LINE_TABLE, has_header_row=False)

        result = templates.apply_template(headless, geometry.extract_table(path, 1))
        assert len(result.rows) == 3
        assert result.rows[0]["duty_amount"].decimal == Decimal("46875.00")


class TestWhatItReportsRatherThanRaises:
    def test_an_unreadable_amount_becomes_an_issue_and_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        """A dash where the duty should be.

        The line still yields its other fields, and the caller gets a reason to route the
        declaration to review. Raising here would discard four good columns because the
        fifth was struck through by hand.
        """
        struck = [list(row) for row in BAYAN_DECLARATION]
        struck[1][5] = "—"
        path = _render(struck, tmp_path, Storage.VISUAL, "struck")

        result = templates.apply_template(
            templates.BAYAN_LINE_TABLE, geometry.extract_table(path, 1)
        )
        assert not result.clean
        assert any("duty_amount" in issue for issue in result.issues)
        assert result.rows[0].get("duty_amount") is None
        assert result.rows[0]["declared_value"].decimal == Decimal("937500.00")
        assert result.rows[1]["duty_amount"].decimal == Decimal("4250.00")

    def test_a_page_with_no_table_says_so(self) -> None:
        result = templates.apply_template(templates.BAYAN_LINE_TABLE, [])
        assert result.rows == []
        assert result.issues == ["page has no table"]


class TestTheBoxTravelsWithTheField:
    def test_spans_point_at_the_cell_the_figure_was_read_from(
        self, typed: templates.TemplateResult
    ) -> None:
        document_id = uuid4()
        spans = typed.spans_for(typed.rows[0], document_id=document_id, document_sha256=BAYAN_SHA)

        assert set(spans) == {"duty_paid", "entered_value", "quantity"}
        duty_span = spans["duty_paid"]
        assert duty_span.bbox == typed.rows[0]["duty_amount"].bbox
        assert duty_span.document_sha256 == BAYAN_SHA
        assert duty_span.page == 1
        assert duty_span.raw_text == "46875.00"
        assert duty_span.extractor == "geometry-template"

    def test_each_figure_gets_its_own_rectangle(self, typed: templates.TemplateResult) -> None:
        """One box per figure, not one box per row.

        A trace that returned the row's bounding box for every field in it would satisfy
        the schema and locate nothing, which is the failure `figure_spans` in the root
        conftest is also written to avoid.
        """
        spans = typed.spans_for(typed.rows[0], document_id=uuid4(), document_sha256=BAYAN_SHA)
        boxes = [span.bbox for span in spans.values()]
        assert len(set(boxes)) == len(boxes)

        # RTL: the duty column is leftmost on the page, the quantity is to its right.
        assert spans["duty_paid"].x1 < spans["quantity"].x0

    def test_the_language_recorded_is_the_cell_s_own(self, typed: templates.TemplateResult) -> None:
        """Numerals normalise to ASCII during reconstruction, so a figure cell reads as
        English even on an Arabic form. Recording the cell's verdict rather than the
        document's keeps the trace honest about what was actually in the box."""
        spans = typed.spans_for(typed.rows[0], document_id=uuid4(), document_sha256=BAYAN_SHA)
        assert spans["duty_paid"].language is Language.ENGLISH


class TestTheRegistry:
    def test_a_bayan_resolves_to_the_bayan_template(self) -> None:
        assert templates.template_for(DocumentKind.ZATCA_BAYAN) is templates.BAYAN_LINE_TABLE

    def test_a_document_class_with_no_line_table_resolves_to_nothing(self) -> None:
        assert templates.template_for(DocumentKind.BANK_GUARANTEE) is None

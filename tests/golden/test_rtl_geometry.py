"""The RTL table reconstruction, against a *Bayan* built to be hostile.

The fixture is a synthetic ZATCA re-export declaration table with Arabic column headings
and Arabic-Indic figures — see `tests/fixtures/bayan.py` for why it is assembled at the
PDF object level rather than rendered with a font.

What these tests are for is narrow and worth stating. They do not check that Arabic can be
read out of a PDF; `services/extraction/src/arabic.py` and its suite cover that. They check
that reading a *table* — cells with no separator, where week 2's heuristic has nothing to
work with — produces the figures a reader sees, and that it does so from coordinates rather
than from the character stream, which for numerals is wrong.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pymupdf
import pytest

from services.extraction.src import geometry
from services.extraction.src.native import parse_amount
from tests.fixtures.bayan import BAYAN_TABLE, Storage, bayan_pdf, visual_order

if TYPE_CHECKING:
    from pathlib import Path

# The table as a reader sees it. Column 0 is the rightmost on the page.
EXPECTED = [
    ["البيان", "الكمية", "القيمة", "الرسوم"],
    ["حاسبات", "1200", "937500.00", "46875.00"],
    ["طابعات", "340", "85000.00", "4250.00"],
    ["شاشات", "500", "125000.00", "6250.00"],
]


@pytest.fixture(params=[Storage.VISUAL, Storage.LOGICAL], ids=["visual-stream", "logical-stream"])
def bayan(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """The same page, written into the content stream two different ways.

    Parametrised rather than picking one, because the claim being made is that the
    reconstruction does not depend on which the producer chose. A test against one storage
    order would pass just as well if the module were quietly reading the stream.
    """
    path = tmp_path / f"bayan-{request.param.value}.pdf"
    path.write_bytes(bayan_pdf(BAYAN_TABLE, storage=request.param))
    return path


class TestTheTableComesBackInReadingOrder:
    def test_every_cell_is_where_a_reader_would_find_it(self, bayan: Path) -> None:
        assert geometry.extract_table_cells(bayan, 1) == EXPECTED

    def test_the_rightmost_column_is_column_zero(self, bayan: Path) -> None:
        """RTL column order is a property of the row, not of the page.

        A Bayan's first column is its rightmost, and emitting cells left-to-right would
        line the quantity up under the heading for duty — a table that parses cleanly into
        the wrong fields.
        """
        rows = geometry.extract_table(bayan, 1)
        first, second = rows[0].cells[0], rows[0].cells[1]
        assert first.column == 0
        assert first.bbox[0] > second.bbox[0]
        assert all(row.rtl for row in rows)

    def test_the_rows_are_banded_by_overlap_not_by_equal_tops(self, bayan: Path) -> None:
        rows = geometry.extract_table(bayan, 1)
        assert len(rows) == len(BAYAN_TABLE)
        tops = [min(cell.bbox[1] for cell in row.cells) for row in rows]
        assert tops == sorted(tops)


class TestTheStreamIsNotConsulted:
    def test_the_stream_reverses_the_figures_and_the_geometry_does_not(self, bayan: Path) -> None:
        """The corruption this module exists for, demonstrated on the same file.

        MuPDF's text layer reverses Arabic-Indic numeral runs: a quantity of ١٢٠٠ comes
        out as ٠٠٢١. Normalised that is 21, from a line that imported 1200 — a figure that
        parses, reconciles against nothing, and is wrong by a factor of fifty-seven.
        """
        with pymupdf.open(bayan) as document:
            stream = document[0].get_text()

        assert "١٢٠٠" not in stream, "the correct numeral sequence should not survive"
        assert "٠٠٢١" in stream.replace(" ", ""), "expected the run to come back reversed"

        quantities = [row[1] for row in geometry.extract_table_cells(bayan, 1)[1:]]
        assert quantities == ["1200", "340", "500"]

    def test_the_figures_parse_to_the_amounts_on_the_declaration(self, bayan: Path) -> None:
        """Through `parse_amount`, because a box is only useful if a Decimal comes out.

        The Arabic decimal separator (U+066B) is a distinct code point from '.' and is
        mapped during reconstruction; left in place it would truncate the fractional part
        silently rather than raise.
        """
        duties = [parse_amount(row[3]) for row in geometry.extract_table_cells(bayan, 1)[1:]]
        assert duties == [Decimal("46875.00"), Decimal("4250.00"), Decimal("6250.00")]

    def test_the_fixture_really_does_mangle_what_it_stores(self) -> None:
        """A guard on the fixture itself.

        If `visual_order` ever became the identity the whole suite above would keep
        passing while testing nothing, because the mangled and unmangled inputs would be
        the same bytes.
        """
        assert visual_order("الكمية") == "ةيمكلا"
        assert visual_order("الكمية 500") == "500 ةيمكلا"


class TestWhatItRefusesToDo:
    def test_a_page_outside_the_document_raises(self, bayan: Path) -> None:
        with pytest.raises(ValueError, match="out of range"):
            geometry.extract_table(bayan, 9)

    def test_a_page_with_no_glyphs_is_empty_not_an_error(self, tmp_path: Path) -> None:
        """A blank page is a fact about the document, not a failure to read it."""
        path = tmp_path / "blank.pdf"
        path.write_bytes(bayan_pdf([[" "]]))
        assert geometry.extract_table(path, 1) == []

    def test_an_embedded_latin_code_keeps_its_own_direction(self, tmp_path: Path) -> None:
        """Bidi inside one cell.

        A Bayan writes its HS code inside an Arabic cell. Reversing the whole cell would
        turn 84713000 into 00031748 — a code that is well-formed, classifiable, and points
        at a different chapter of the schedule.
        """
        path = tmp_path / "mixed.pdf"
        path.write_bytes(bayan_pdf([["رمز 84713000"]]))
        assert geometry.extract_table_cells(path, 1) == [["رمز 84713000"]]


class TestOrderOfIsDiagnosticOnly:
    def test_it_reports_the_stream_the_extractor_produced(self, bayan: Path) -> None:
        """Both storage orders report LOGICAL, and that is the honest answer.

        MuPDF applies its own bidi pass to Arabic letter runs before we see them, so the
        verdict describes what the extractor handed over rather than what the producer
        wrote. Recorded on the cell for diagnosis; nothing branches on it, which is why
        the reconstruction above is identical either way.
        """
        headings = geometry.extract_table(bayan, 1)[0].cells
        assert {cell.glyph_order for cell in headings} == {geometry.GlyphOrder.LOGICAL}

    def test_a_run_too_short_to_judge_says_so(self) -> None:
        glyphs = [geometry.Glyph(char="ا", x0=10.0, y0=0.0, x1=16.0, y1=12.0, index=0)]
        assert geometry.order_of(glyphs) is geometry.GlyphOrder.UNDETERMINED

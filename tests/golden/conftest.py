"""Golden fixture builders.

Golden fixtures are hand-verified known-answer claims. They exist so a refactor that
changes a computed figure fails loudly rather than quietly filing a different number with
a customs authority. Every expected value in `tests/golden/` is derived by hand in the
fixture docstring, never by running the code and pasting the output.

Fixture construction note. Labels and values are inserted as *separate positioned text
runs*, which is both how real form PDFs are laid out and the only way to build a reliable
fixture: passing MuPDF a single string mixing Arabic letters with Arabic-Indic numerals
makes it shape the run itself, splitting `٤٦٨٧٥٫٠٠` into `٦٤…۸۷٫٥۰۰` and substituting
glyphs across two different Arabic digit blocks. That would test the fixture generator's
bidi bugs rather than the extractor.

A real scanned corpus arrives in week 3; these synthetic documents exercise the native
path and the bilingual normalisation only.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from tests.golden.fonts import SKIP_REASON, arabic_font_path

# (label, value) pairs. Arabic labels, and values that are Arabic-Indic where a real
# Bayan would carry them that way — the amounts and the quantity.
BAYAN_ROWS: list[tuple[str, str]] = [
    ("رقم البيان:", "20240115447821"),
    ("تاريخ البيان:", "2024-01-15"),
    ("تاريخ السداد:", "2024-02-08"),
    ("المنفذ:", "Jeddah Islamic Port"),
    ("اسم المستورد:", "Al Faisaliah Trading Co"),
    ("الرقم الضريبي:", "300012345600003"),
    ("بلد المنشأ:", "CN"),
    ("البند الجمركي:", "8471300000"),
    ("وصف البضاعة:", "Portable data processing machines"),
    ("الكمية:", "١٢٠٠"),
    ("الوحدة:", "PCE"),
    ("القيمة الجمركية:", "٩٣٧٥٠٠٫٠٠"),
    ("الرسوم الجمركية:", "٤٦٨٧٥٫٠٠"),
    ("ضريبة القيمة المضافة:", "١٤٧٦٥٦٫٢٥"),
    ("الإجمالي:", "1132031.25"),
]

BAYAN_HEADINGS = [
    "الهيئة العامة للزكاة والضريبة والجمارك",
    "Zakat, Tax and Customs Authority",
]

CBP_7501_ROWS: list[tuple[str, str]] = [
    ("Entry Number:", "ABC-1234567-8"),
    ("Entry Summary Date:", "2023-03-20"),
    ("Import Date:", "2023-03-14"),
    ("Port of Entry:", "2704"),
    ("Country of Origin:", "CN"),
    ("HTSUS Number:", "8471300100"),
    ("Description of Merchandise:", "Portable automatic data processing machines"),
    ("Quantity:", "1000"),
    ("Unit:", "NO"),
    ("Entered Value:", "250000.00"),
    # Both labels resolve to DUTY_AMOUNT and hold different numbers. The extractor must
    # take the more specific one; first-wins would file 0.00.
    ("Duty:", "0.00"),
    ("Customs Duty:", "62500.00"),
    ("Total:", "313364.00"),
]

CBP_7501_HEADINGS = [
    "DEPARTMENT OF HOMELAND SECURITY",
    "U.S. Customs and Border Protection",
    "ENTRY SUMMARY",
]


def _write_form_pdf(
    path: Path,
    headings: list[str],
    rows: list[tuple[str, str]],
    *,
    arabic: bool = False,
) -> Path:
    """Render a label/value form to a PDF with a real text layer.

    PyMuPDF's base-14 fonts have neither Arabic glyphs nor Arabic encoding, so Arabic
    pages embed a discovered host font. Label and value go in separate positioned runs
    so no single insertion mixes scripts — see the module docstring.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4

    fontname, fontfile = "helv", None
    if arabic:
        found = arabic_font_path()
        if found is None:
            doc.close()
            msg = "no Arabic-capable font available"
            raise RuntimeError(msg)
        fontname, fontfile = "formfont", str(found)

    y = 60.0
    for heading in headings:
        page.insert_text((60, y), heading, fontname=fontname, fontfile=fontfile, fontsize=11)
        y += 24

    y += 12
    for label, value in rows:
        page.insert_text((60, y), label, fontname=fontname, fontfile=fontfile, fontsize=10)
        page.insert_text((300, y), value, fontname=fontname, fontfile=fontfile, fontsize=10)
        y += 22

    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def golden_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden")


@pytest.fixture(scope="session")
def bayan_pdf(golden_dir: Path) -> Path:
    """Synthetic ZATCA Bayan with Arabic labels and Arabic-Indic numerals."""
    if arabic_font_path() is None:
        pytest.skip(SKIP_REASON)
    return _write_form_pdf(
        golden_dir / "bayan_20240115447821.pdf",
        BAYAN_HEADINGS,
        BAYAN_ROWS,
        arabic=True,
    )


@pytest.fixture(scope="session")
def cbp_7501_pdf(golden_dir: Path) -> Path:
    """Synthetic CBP 7501 entry summary."""
    return _write_form_pdf(golden_dir / "cbp_7501_abc1234567.pdf", CBP_7501_HEADINGS, CBP_7501_ROWS)


@pytest.fixture(scope="session")
def scanned_pdf(golden_dir: Path) -> Path:
    """A page with no text layer — the document that must route to OCR.

    Guards the ordering invariant from the other direction: `assess` must reject this,
    or scanned documents would silently return zero fields.
    """
    path = golden_dir / "scanned_no_text_layer.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(50, 50, 545, 400), color=(0, 0, 0), width=1)
    doc.save(path)
    doc.close()
    return path

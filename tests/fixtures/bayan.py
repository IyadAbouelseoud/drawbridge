"""A synthetic ZATCA *Bayan* table, built at the PDF object level.

No font in the environment carries Arabic glyphs — the base-14 set does not, and depending
on a system font would make the suite pass on one machine and skip on another. So the
document is assembled by hand: a Type0/Identity-H font with a ToUnicode CMap and no
embedded font program. MuPDF resolves each code to its Unicode character through the CMap
and positions it from the text matrix, which is exactly the pair of facts
`services.extraction.src.geometry` consumes. The page renders as empty boxes; nothing
about the test looks at how it renders.

What matters is that this builder controls the two things a real *Bayan* varies:

**Position.** Every glyph is placed individually, so the coordinates are known to the
point and a cell's box can be asserted rather than approximated.

**Storage order.** `Storage.VISUAL` emits the glyphs into the content stream in the order
they appear left to right — the mangled stream that a non-conforming producer leaves
behind, where reading the characters out in sequence gives a reversed string.
`Storage.LOGICAL` emits them in reading order, so x jumps backwards across the page. Both
are valid PDFs and both are found in the wild, which is the whole reason the geometry
module exists.
"""

from __future__ import annotations

from enum import StrEnum

from services.extraction.src.arabic import is_rtl

PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0
FONT_SIZE = 10.0
# /DW 600 in the descendant font, so every glyph advances 0.6 em.
ADVANCE = FONT_SIZE * 0.6

# Right edge of the first (rightmost) column, and the pitch between columns. The pitch
# leaves gaps far wider than `geometry.COLUMN_GAP_POINTS`, which is what a table looks
# like; two words inside one cell are one advance apart.
FIRST_COLUMN_RIGHT = 520.0
COLUMN_PITCH = 120.0
FIRST_ROW_BASELINE = 700.0
ROW_PITCH = 24.0


class Storage(StrEnum):
    """Which order the producer wrote the characters into the content stream."""

    LOGICAL = "logical"
    VISUAL = "visual"


def visual_order(text: str) -> str:
    """The left-to-right sequence a reader sees, given logical-order text.

    The inverse of what `geometry.text_from_geometry` reconstructs, which is deliberate:
    a fixture that produced its mangled input by any other route would be testing the
    geometry module against a string this file invented rather than against the layout a
    bidi renderer actually produces.
    """

    # Three classes, not two. Whitespace is its own run so it stays *between* the runs it
    # separated rather than migrating to one end — which is what a bidi renderer does with
    # a neutral between two runs, and is the difference between a cell that reads
    # "84713000 زمر" and one that reads " 84713000زمر".
    def _class(char: str) -> int:
        if char.isspace():
            return 0
        return 1 if is_rtl(char) else 2

    runs: list[tuple[int, list[str]]] = []
    for char in text:
        kind = _class(char)
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(char)
        else:
            runs.append((kind, [char]))

    out: list[str] = []
    for kind, run in reversed(runs):
        out.extend(reversed(run) if kind == 1 else run)
    return "".join(out)


def _placed(text: str, x_left: float, baseline: float) -> list[tuple[str, float, float]]:
    """Glyphs of one cell, in left-to-right visual sequence, with their positions."""
    visual = visual_order(text)
    return [
        (char, x_left + index * ADVANCE, baseline)
        for index, char in enumerate(visual)
        if not char.isspace()
    ]


def cell_glyphs(
    text: str, *, column: int, row: int, storage: Storage
) -> list[tuple[str, float, float]]:
    """One cell's glyphs in *emission* order — the order the content stream carries them.

    Under VISUAL the emission order is the visual order, so an extractor reading the
    stream gets the reversed string. Under LOGICAL the same glyphs sit at the same
    coordinates but are emitted in reading order, so x descends across an Arabic run.
    Positions are identical either way, which is the point: the coordinates decide, and
    the stream does not.
    """
    right_edge = FIRST_COLUMN_RIGHT - column * COLUMN_PITCH
    width = len([c for c in text if not c.isspace()]) * ADVANCE
    placed = _placed(text, right_edge - width, FIRST_ROW_BASELINE - row * ROW_PITCH)
    if storage is Storage.VISUAL:
        return placed

    by_char: dict[str, list[tuple[str, float, float]]] = {}
    for glyph in placed:
        by_char.setdefault(glyph[0], []).append(glyph)
    for glyphs in by_char.values():
        glyphs.sort(key=lambda g: g[1])

    emission: list[tuple[str, float, float]] = []
    taken: dict[str, int] = {}
    for char in text:
        if char.isspace():
            continue
        # Identical characters repeat within a cell; hand out their positions right to
        # left so the n-th occurrence in logical order takes the n-th position from the
        # right, which is where a reader meets it.
        candidates = by_char[char]
        index = len(candidates) - 1 - taken.get(char, 0)
        taken[char] = taken.get(char, 0) + 1
        emission.append(candidates[index])
    return emission


def bayan_pdf(rows: list[list[str]], *, storage: Storage = Storage.VISUAL) -> bytes:
    """Render a table whose cells are given in reading order — column 0 is rightmost."""
    glyphs: list[tuple[str, float, float]] = []
    for row_index, row in enumerate(rows):
        for column_index, text in enumerate(row):
            glyphs.extend(cell_glyphs(text, column=column_index, row=row_index, storage=storage))
    return _pdf(glyphs)


def _pdf(glyphs: list[tuple[str, float, float]]) -> bytes:
    """Assemble the PDF. One `Tm` per glyph, so every position is explicit."""
    alphabet = sorted({char for char, _, _ in glyphs})
    cid = {char: index + 1 for index, char in enumerate(alphabet)}

    content = "\n".join(
        f"BT /F1 {FONT_SIZE} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm <{cid[char]:04X}> Tj ET"
        for char, x, y in glyphs
    ).encode("ascii")

    bfchar = "\n".join(f"<{cid[c]:04X}> <{ord(c):04X}>" for c in alphabet)
    cmap = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin begincmap\n"
        "/CMapName /Bayan-UCS2 def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(alphabet)} beginbfchar\n{bfchar}\nendbfchar\n"
        "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode("ascii")

    objects: dict[int, bytes] = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: (
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 %d %d]"
            b"/Resources<</Font<</F1 4 0 R>>>>/Contents 7 0 R>>"
            % (int(PAGE_WIDTH), int(PAGE_HEIGHT))
        ),
        4: (
            b"<</Type/Font/Subtype/Type0/BaseFont/BayanTest/Encoding/Identity-H"
            b"/DescendantFonts[5 0 R]/ToUnicode 6 0 R>>"
        ),
        5: (
            b"<</Type/Font/Subtype/CIDFontType2/BaseFont/BayanTest"
            b"/CIDSystemInfo<</Registry(Adobe)/Ordering(Identity)/Supplement 0>>"
            b"/FontDescriptor 8 0 R/DW 600/CIDToGIDMap/Identity>>"
        ),
        6: b"<</Length %d>>stream\n" % len(cmap) + cmap + b"\nendstream",
        7: b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream",
        8: (
            b"<</Type/FontDescriptor/FontName/BayanTest/Flags 4"
            b"/FontBBox[0 -200 1000 900]/ItalicAngle 0/Ascent 900/Descent -200"
            b"/CapHeight 700/StemV 80>>"
        ),
    }

    out = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"

    start_xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for number in sorted(objects):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start_xref,
    )
    return bytes(out)


# A page of a re-export *Bayan*: a header row and three line items. Columns are given in
# reading order, so index 0 is the rightmost on the page.
#
#   البيان        goods description
#   الكمية        quantity
#   القيمة        value
#   الرسوم        duty
#
# Figures use Arabic-Indic numerals with the Arabic decimal separator (U+066B), which is
# what regional systems emit and what silently truncates if it reaches Decimal unmapped.
BAYAN_TABLE: list[list[str]] = [
    ["البيان", "الكمية", "القيمة", "الرسوم"],
    ["حاسبات", "١٢٠٠", "٩٣٧٥٠٠٫٠٠", "٤٦٨٧٥٫٠٠"],
    ["طابعات", "٣٤٠", "٨٥٠٠٠٫٠٠", "٤٢٥٠٫٠٠"],
    ["شاشات", "٥٠٠", "١٢٥٠٠٠٫٠٠", "٦٢٥٠٫٠٠"],
]

"""Glyph-geometry reconstruction of RTL table cells.

**Status: implemented in week 10. Deferred since week 2 — see docs/ROADMAP.md.**

Week 2's `arabic.looks_visually_ordered` decides visual-vs-logical order from a heuristic:
a field label never begins with its separator. That works for `label: value` lines and
*cannot* work for table cells, which carry no separator at all. The character stream
genuinely does not determine the answer; both orderings are valid Unicode, and reordering
unconditionally corrupts conforming documents exactly as badly as never reordering
corrupts non-conforming ones.

Glyph x-coordinates do determine it, and this module reads them. `page.get_text("rawdict")`
carries a bbox for every character, so where a character *is* becomes available alongside
what it is — and once the coordinates are in hand the stored order stops mattering.
`extract_table` never reads the character sequence at all: it clusters glyphs into rows by
vertical overlap and into cells by horizontal proximity, then emits each cell's text in the
order the coordinates say a reader encounters it — right to left for an Arabic cell, left
to right for the Latin and numeric runs embedded in one.

That the character stream needs bypassing is not hypothetical. MuPDF applies its own bidi
pass to Arabic *letter* runs, so words usually arrive readable; it does not get Arabic-Indic
*numerals* right, and a quantity of ١٢٠٠ comes out of the text layer as ٠٠٢١ — which
normalises to 21 and parses without complaint. A reversed word is noticed by whoever reads
it. A reversed quantity is filed.

`order_of` reports which way a run advances in the stream, but it is diagnostic only:
MuPDF's own reordering means the verdict describes what the extractor produced rather than
what the producer wrote, and nothing here branches on it.

What this module does not do is decide *which* cell is which field. Column semantics come
from the caller: a Bayan's table header, or a template. Guessing them from position would
put a layout heuristic between a document and a duty figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from statistics import median

import pymupdf

from services.extraction.src.arabic import detect_language, is_rtl, normalise_digits

# A new row starts when a glyph's vertical extent overlaps the current band by less than
# this fraction of its own height. Superscripts and the small print on a Bayan sit within
# a line's band; the next table row does not.
ROW_OVERLAP_RATIO = 0.5

# Minimum horizontal gap, in points, that separates two cells rather than two words. The
# effective threshold also scales with the glyph width on the row, because a table set in
# 6pt has proportionally tighter columns than one set in 12pt.
COLUMN_GAP_POINTS = 12.0
COLUMN_GAP_WIDTHS = 1.8

# Within a cell, a horizontal gap wider than this fraction of a glyph is a word space.
# Space characters are dropped on the way in — a space has no position worth citing — so
# the gap they leave is the only remaining evidence that a cell holds two words rather
# than one. Losing it would run a label into its value and break field matching.
WORD_GAP_WIDTHS = 0.4

# Fewer than this many glyphs in a run and the advance direction is not evidence — two
# characters can be adjacent in either order for reasons that have nothing to do with
# storage order.
MIN_GLYPHS_FOR_ORDER = 3


class GlyphOrder(StrEnum):
    """What the geometry says about a run's storage order."""

    LOGICAL = "logical"
    VISUAL = "visual"
    UNDETERMINED = "undetermined"
    """Too few glyphs, or x-coordinates not monotonic — fall back to the heuristic."""


@dataclass(frozen=True, slots=True)
class Glyph:
    """One positioned character from the PDF content stream.

    `bbox` convention matches `native.TextSpan`: origin top-left, PDF points.
    """

    char: str
    x0: float
    y0: float
    x1: float
    y1: float
    index: int = 0
    """Position in the page's extracted character stream.

    Kept because every other step here sorts by coordinate, and the one question that
    needs the original sequence — which direction the run advances in — cannot be asked
    afterwards.
    """

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def y(self) -> float:
        """Vertical midpoint — what row banding compares."""
        return (self.y0 + self.y1) / 2.0


@dataclass(frozen=True, slots=True)
class Cell:
    """One table cell, with the text in logical order and the box it was read from.

    `bbox` is the union of the cell's glyph boxes, which is what a provenance span for a
    figure in this cell must carry. It is measured, not inferred from a grid, so a cell
    that a template says should exist but that carries no glyphs produces no `Cell` at
    all rather than an empty box pointing at nothing.
    """

    page: int
    row: int
    column: int
    text: str
    bbox: tuple[float, float, float, float]
    language: str
    glyph_order: GlyphOrder


@dataclass(frozen=True, slots=True)
class TableRow:
    """Cells of one row, ordered as a reader encounters them."""

    index: int
    cells: tuple[Cell, ...]
    rtl: bool

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(cell.text for cell in self.cells)


# --------------------------------------------------------------------------------------
# Reading glyphs
# --------------------------------------------------------------------------------------


def glyphs_on_page(pdf_path: Path, page: int) -> list[Glyph]:
    """Every positioned character on a 1-indexed page.

    `rawdict` rather than `dict` or `words`: the coarser modes return a bbox per span or
    per word, and a word's bbox says nothing about the direction its characters advance
    in. Whitespace is dropped here — a space carries no position worth citing — but the
    physical gap it leaves behind survives in the coordinates, which is what cell
    splitting actually measures.
    """
    glyphs: list[Glyph] = []
    with pymupdf.open(pdf_path) as doc:
        if not 1 <= page <= doc.page_count:
            msg = f"page {page} out of range for a {doc.page_count}-page document"
            raise ValueError(msg)
        raw = doc[page - 1].get_text("rawdict")

    for block in raw.get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                for char in span.get("chars", ()):
                    text = char.get("c", "")
                    if not text or text.isspace():
                        continue
                    x0, y0, x1, y1 = char["bbox"]
                    glyphs.append(Glyph(char=text, x0=x0, y0=y0, x1=x1, y1=y1, index=len(glyphs)))
    return glyphs


# --------------------------------------------------------------------------------------
# Storage order
# --------------------------------------------------------------------------------------


def order_of(glyphs: list[Glyph]) -> GlyphOrder:
    """Which direction the RTL characters advance in, as the extractor presents them.

    Only RTL glyphs are consulted. A cell's digits and Latin codes advance left-to-right
    under both storage orders, so including them would dilute the signal with characters
    that carry none. The comparison runs over `index` — the position in the extracted
    stream — because everything else in this module has already sorted by coordinate.

    What this reports is the order *MuPDF hands us*, which is not always the order the
    producer wrote. MuPDF applies its own bidi pass to Arabic letter runs, so a
    visually-stored word arrives already reordered and this returns LOGICAL for it. The
    verdict is therefore diagnostic rather than load-bearing: `text_from_geometry` does
    not consult it, and reconstructs from coordinates whatever this says.

    UNDETERMINED rather than a guess when the run is short or the advance is not
    monotonic — see `MIN_GLYPHS_FOR_ORDER`.
    """
    rtl = sorted((g for g in glyphs if is_rtl(g.char)), key=lambda g: g.index)
    if len(rtl) < MIN_GLYPHS_FOR_ORDER:
        return GlyphOrder.UNDETERMINED

    steps = list(pairwise(rtl))
    forward = sum(1 for a, b in steps if b.x0 > a.x0)
    backward = sum(1 for a, b in steps if b.x0 < a.x0)
    if forward and backward:
        return GlyphOrder.UNDETERMINED
    if backward:
        return GlyphOrder.LOGICAL
    if forward:
        return GlyphOrder.VISUAL
    return GlyphOrder.UNDETERMINED


# --------------------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------------------


def _rows(glyphs: list[Glyph]) -> list[list[Glyph]]:
    """Band glyphs into rows by vertical overlap.

    Overlap rather than equal `y0`: a row of a Bayan table mixes Arabic, which has
    descenders and a taller box, with ASCII digits, and the two do not share a top edge.
    """
    bands: list[list[Glyph]] = []
    for glyph in sorted(glyphs, key=lambda g: (g.y0, g.x0)):
        for band in bands:
            top = max(band[0].y0, glyph.y0)
            bottom = min(max(g.y1 for g in band), glyph.y1)
            if glyph.height and (bottom - top) / glyph.height >= ROW_OVERLAP_RATIO:
                band.append(glyph)
                break
        else:
            bands.append([glyph])
    return bands


def _gap_threshold(row: list[Glyph]) -> float:
    widths = [g.width for g in row if g.width > 0]
    if not widths:
        return COLUMN_GAP_POINTS
    return max(COLUMN_GAP_POINTS, COLUMN_GAP_WIDTHS * median(widths))


def _split_cells(row: list[Glyph]) -> list[list[Glyph]]:
    """Split one row into cells at horizontal gaps wider than a word space."""
    ordered = sorted(row, key=lambda g: g.x0)
    threshold = _gap_threshold(ordered)
    cells: list[list[Glyph]] = [[ordered[0]]]
    for glyph in ordered[1:]:
        if glyph.x0 - max(g.x1 for g in cells[-1]) > threshold:
            cells.append([glyph])
        else:
            cells[-1].append(glyph)
    return cells


# --------------------------------------------------------------------------------------
# Logical order from coordinates
# --------------------------------------------------------------------------------------


def _is_rtl_cell(glyphs: list[Glyph]) -> bool:
    """Whether the cell's own script makes it right-to-left.

    Counted over letters only. A cell holding an Arabic label and a figure is RTL; one
    holding "84713000" is not, whatever the cells beside it hold.
    """
    letters = [g for g in glyphs if g.char.isalpha()]
    if not letters:
        return False
    return sum(1 for g in letters if is_rtl(g.char)) * 2 > len(letters)


def text_from_geometry(glyphs: list[Glyph]) -> str:
    """Rebuild a cell's logical-order text from glyph positions alone.

    The character sequence is never read. Glyphs are sorted left-to-right — the order
    they were painted in, which is a fact about the page rather than about the producer —
    and then re-emitted in reading order.

    For an RTL cell that means the runs come out right-to-left and the RTL characters
    within a run come out right-to-left, while embedded digits and Latin codes keep their
    left-to-right order. That last part is not a nicety: a Bayan's declaration number and
    HS code sit inside Arabic cells, and reversing them produces a number that parses
    cleanly and is wrong.
    """
    if not glyphs:
        return ""

    visual = sorted(glyphs, key=lambda g: g.x0)
    widths = [g.width for g in visual if g.width > 0]
    gap = WORD_GAP_WIDTHS * (median(widths) if widths else 0.0)

    if not _is_rtl_cell(glyphs):
        return normalise_digits(_run_text(visual, gap, reverse=False)).strip()

    runs: list[tuple[bool, list[Glyph]]] = []
    for glyph in visual:
        rtl = is_rtl(glyph.char)
        if runs and runs[-1][0] == rtl:
            runs[-1][1].append(glyph)
        else:
            runs.append((rtl, [glyph]))

    # Neutrals — punctuation and separators, spaces having been dropped — take the
    # direction of the run they follow, which is what the bidi algorithm does for them
    # between two runs of the same direction.
    pieces: list[str] = []
    for index, (rtl, run) in enumerate(reversed(runs)):
        if index and _gap_between(runs, len(runs) - index) > gap:
            pieces.append(" ")
        pieces.append(_run_text(run, gap, reverse=rtl))
    return normalise_digits("".join(pieces)).strip()


def _run_text(run: list[Glyph], gap: float, *, reverse: bool) -> str:
    """One directional run, with word spaces restored from the physical gaps."""
    chars: list[str] = []
    for index, glyph in enumerate(run):
        chars.append(glyph.char)
        if index + 1 < len(run) and run[index + 1].x0 - glyph.x1 > gap:
            chars.append(" ")
    if reverse:
        chars.reverse()
    return "".join(chars)


def _gap_between(runs: list[tuple[bool, list[Glyph]]], right_index: int) -> float:
    """Horizontal gap between two adjacent runs, given the index of the right-hand one."""
    left = runs[right_index - 1][1]
    right = runs[right_index][1]
    return right[0].x0 - left[-1].x1


# --------------------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------------------


def extract_table(pdf_path: Path, page: int) -> list[TableRow]:
    """Reconstruct a page's table cells with their coordinates.

    Rows come out top-to-bottom. Cells within a row come out in reading order: right-to-
    left when the row's own glyphs are predominantly Arabic, left-to-right otherwise, so
    `column` 0 is the first cell a reader meets rather than the leftmost one on the page.

    Every cell carries the box it was measured from. That box is what a
    `ProvenanceSpan` for a figure in the cell cites, which is the whole point of doing
    this from geometry rather than from the character stream: a figure extracted from a
    reordered string has no coordinates to point at.
    """
    glyphs = glyphs_on_page(pdf_path, page)
    if not glyphs:
        return []

    table: list[TableRow] = []
    for row_index, band in enumerate(_rows(glyphs)):
        cells_glyphs = _split_cells(band)
        rtl_row = _is_rtl_cell(band)
        ordered = list(reversed(cells_glyphs)) if rtl_row else cells_glyphs

        cells: list[Cell] = []
        for column, cell_glyphs in enumerate(ordered):
            text = text_from_geometry(cell_glyphs)
            if not text:
                continue
            cells.append(
                Cell(
                    page=page,
                    row=row_index,
                    column=column,
                    text=text,
                    bbox=(
                        min(g.x0 for g in cell_glyphs),
                        min(g.y0 for g in cell_glyphs),
                        max(g.x1 for g in cell_glyphs),
                        max(g.y1 for g in cell_glyphs),
                    ),
                    language=detect_language(text),
                    glyph_order=order_of(cell_glyphs),
                )
            )
        if cells:
            table.append(TableRow(index=len(table), cells=tuple(cells), rtl=rtl_row))
    return table


def extract_table_cells(pdf_path: Path, page: int) -> list[list[str]]:
    """Table text only, in reading order. See `extract_table` for the coordinates.

    Returns an empty list for a page with no glyphs — a page that is genuinely blank, or
    a scan with no text layer, which `native.assess` has already refused by the time
    anything calls this.
    """
    return [list(row.texts) for row in extract_table(pdf_path, page)]

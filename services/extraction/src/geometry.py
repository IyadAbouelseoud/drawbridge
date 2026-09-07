"""Glyph-geometry RTL reconstruction — deferred, deliberately isolated.

**Status: stubbed. Nothing in weeks 3-6 depends on this.**

Why it exists as a module rather than a TODO comment: week 2's
`arabic.looks_visually_ordered` decides visual-vs-logical order from a heuristic — a
field label never begins with its separator. That works for `label: value` lines and
*cannot* work for table cells, which carry no separator at all. The character stream
genuinely does not determine the answer; both orderings are valid Unicode.

Glyph x-coordinates do determine it. If the characters of an Arabic run ascend in x as
the string advances, the run was stored in visual order; if they descend, logical. That
is decidable, but it needs per-glyph geometry that `page.get_text("dict")` does not
expose — it requires `page.get_texttrace()` or raw content-stream parsing, plus a real
scanned *Bayan* corpus to validate against.

Isolating it here means the matcher consumes `EntryLine` / `ExportLine`, which the week 2
native path already populates from `label: value` layouts. Table extraction landing later
extends coverage; it does not change any downstream contract.

Revisit at week 4 alongside the OCR corpus — see docs/ROADMAP.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GlyphOrder(StrEnum):
    """What the geometry says about a run's storage order."""

    LOGICAL = "logical"
    VISUAL = "visual"
    UNDETERMINED = "undetermined"
    """Too few glyphs, or x-coordinates not monotonic — fall back to the heuristic."""


@dataclass(frozen=True, slots=True)
class Glyph:
    """One positioned character from the PDF content stream."""

    char: str
    x0: float
    x1: float
    y: float


def order_of(glyphs: list[Glyph]) -> GlyphOrder:  # noqa: ARG001 - stub signature
    """Decide storage order from glyph advance direction.

    Not implemented. Returns UNDETERMINED so callers fall through to
    `arabic.looks_visually_ordered`, which is what the pipeline uses today.
    """
    return GlyphOrder.UNDETERMINED


def extract_table_cells(pdf_path: object, page: int) -> list[list[str]]:
    """Reconstruct a bilingual table into logical-order cells.

    Not implemented — see the module docstring. Raising rather than returning empty:
    a silent empty table would look like a document with no line items, which is exactly
    the failure mode that turns into an under-claimed refund.
    """
    msg = (
        "glyph-geometry table extraction is deferred to week 4+; the native "
        "label/value path in services/extraction/src/native.py covers current ingest"
    )
    raise NotImplementedError(msg)

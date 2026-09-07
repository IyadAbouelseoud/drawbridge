"""Native PDF text extraction with provenance spans.

Ordering invariant: the native text layer is always tried first; OCR is a fallback, never
the default. OCR is slower, less accurate, and produces weaker provenance. A document that
yields clean native text must never be sent through OCR.

`assess` decides which path a document takes, and returns *why* — the confidence floor is
a tunable, not a magic number buried in a branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymupdf

from services.extraction.src.arabic import detect_language, normalise
from services.extraction.src.field_aliases import Field, resolve, specificity

# Below this ratio of extractable characters per page, the native layer is considered
# absent or unreliable and the document is routed to OCR. Scanned PDFs typically yield
# 0-20 characters per page (page furniture only); native ones yield hundreds.
MIN_CHARS_PER_PAGE = 80

# A page whose text is mostly replacement/control characters has a broken embedded font —
# common in older Arabic PDFs, where the native layer exists but decodes to garbage.
MAX_UNMAPPED_RATIO = 0.15

_UNMAPPED = {"�", "\x00"}


@dataclass(frozen=True, slots=True)
class TextSpan:
    """One extracted text region with its coordinates.

    `bbox` is (x0, top, x1, bottom) in PDF points, origin top-left — pdfplumber's
    convention, which PyMuPDF also uses for `get_text("words")`.
    """

    page: int
    bbox: tuple[float, float, float, float]
    text: str
    language: str


@dataclass(frozen=True, slots=True)
class NativeAssessment:
    """Whether the native text layer is usable, and the evidence for the verdict."""

    usable: bool
    chars_per_page: float
    unmapped_ratio: float
    reason: str

    @property
    def confidence(self) -> float:
        """Rough confidence in the native layer, for `Confidence.score`.

        Deliberately conservative: a clean native layer is ~0.98, not 1.0, because a
        correct character stream can still sit in a mislabelled field.
        """
        if not self.usable:
            return 0.0
        density = min(self.chars_per_page / (MIN_CHARS_PER_PAGE * 4), 1.0)
        return round(0.80 + 0.18 * density, 4)


def assess(pdf_path: Path) -> NativeAssessment:
    """Decide whether the native text layer can carry the extraction."""
    with pymupdf.open(pdf_path) as doc:
        pages = doc.page_count
        if pages == 0:
            return NativeAssessment(False, 0.0, 0.0, "document has no pages")
        text = "".join(page.get_text() for page in doc)

    chars_per_page = len(text) / pages
    unmapped = sum(text.count(c) for c in _UNMAPPED)
    unmapped_ratio = unmapped / len(text) if text else 1.0

    if chars_per_page < MIN_CHARS_PER_PAGE:
        return NativeAssessment(
            False,
            chars_per_page,
            unmapped_ratio,
            f"only {chars_per_page:.0f} chars/page (floor {MIN_CHARS_PER_PAGE}); likely a scan",
        )
    if unmapped_ratio > MAX_UNMAPPED_RATIO:
        return NativeAssessment(
            False,
            chars_per_page,
            unmapped_ratio,
            f"{unmapped_ratio:.0%} unmapped glyphs (ceiling {MAX_UNMAPPED_RATIO:.0%}); "
            "broken embedded font",
        )
    return NativeAssessment(True, chars_per_page, unmapped_ratio, "native text layer is usable")


def extract_spans(pdf_path: Path) -> list[TextSpan]:
    """Pull word-level spans with coordinates from the native text layer.

    Text is normalised (bidi controls stripped, Arabic folded, Arabic-Indic digits mapped
    to ASCII) before it leaves this function, so no downstream parser ever sees a
    Arabic-Indic numeral. The pre-normalisation form is not preserved here because
    `Span.raw_text` on the schema side records it where it matters.
    """
    spans: list[TextSpan] = []
    with pymupdf.open(pdf_path) as doc:
        for page_index, page in enumerate(doc, start=1):
            for x0, top, x1, bottom, word, *_ in page.get_text("words"):
                cleaned = normalise(word)
                if not cleaned:
                    continue
                spans.append(
                    TextSpan(
                        page=page_index,
                        bbox=(x0, top, x1, bottom),
                        text=cleaned,
                        language=detect_language(cleaned),
                    )
                )
    return spans


def extract_lines(pdf_path: Path) -> list[TextSpan]:
    """Line-level spans. Field labels are multi-word, so label matching runs on these."""
    spans: list[TextSpan] = []
    with pymupdf.open(pdf_path) as doc:
        for page_index, page in enumerate(doc, start=1):
            data = page.get_text("dict")
            for block in data.get("blocks", ()):
                for line in block.get("lines", ()):
                    text = "".join(s.get("text", "") for s in line.get("spans", ()))
                    cleaned = normalise(text)
                    if not cleaned:
                        continue
                    x0, top, x1, bottom = line["bbox"]
                    spans.append(
                        TextSpan(
                            page=page_index,
                            bbox=(x0, top, x1, bottom),
                            text=cleaned,
                            language=detect_language(cleaned),
                        )
                    )
    return spans


def parse_amount(text: str) -> Decimal | None:
    """Parse a monetary amount from already-normalised text.

    Returns None rather than raising: an unparseable amount is a review signal, not an
    exception. Never returns a float — see the Decimal invariant in CLAUDE.md.
    """
    cleaned = normalise(text).replace(",", "").replace(" ", "")
    cleaned = "".join(c for c in cleaned if c.isdigit() or c in ".-")
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def find_labelled_values(lines: list[TextSpan]) -> dict[Field, TextSpan]:
    """Resolve `label: value` pairs on a single line to canonical fields.

    Where two labels on the same document resolve to one field — a CBP 7501 carries both
    "Duty" and "Customs Duty", holding different numbers — the more specific label wins.
    First-wins would pick the wrong figure roughly half the time.

    Handles the common single-line layout only. Two-column and table layouts land in
    week 3 alongside the real Bayan corpus; this returns what it can and stays silent
    about the rest rather than guessing a wrong association.
    """
    found: dict[Field, TextSpan] = {}
    ranks: dict[Field, int] = {}

    for span in lines:
        for separator in (":", "：", "\t"):
            if separator not in span.text:
                continue
            label, _, raw_value = span.text.partition(separator)
            field = resolve(label)
            value = raw_value.strip()
            if field is None or not value:
                break
            rank = specificity(label)
            if field not in found or rank > ranks[field]:
                found[field] = TextSpan(
                    page=span.page,
                    bbox=span.bbox,
                    text=value,
                    language=detect_language(value),
                )
                ranks[field] = rank
            break
    return found

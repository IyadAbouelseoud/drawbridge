"""Arabic-capable font discovery for golden fixtures.

PyMuPDF's base-14 fonts (helv, times, cour) have no Arabic glyphs and no Arabic encoding:
inserting Arabic with them writes replacement characters into the text layer, producing a
fixture that silently tests nothing. So the Bayan fixture needs a real font with Arabic
coverage, discovered from the host.

Where no such font exists, the PDF-level Arabic tests skip with a clear reason. The
text-level tests — normalisation, alias resolution, amount parsing, which carry the actual
extraction logic — never skip, because they need no font at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Ordered by Arabic coverage quality, not by preference for any platform.
_CANDIDATES = (
    # Linux / CI — fonts-noto-core, fonts-dejavu, fonts-liberation
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)

# A short Arabic string exercising the letters the Bayan labels actually use.
_PROBE = "رقم البيان"


def _covers_arabic(path: Path) -> bool:
    """Whether a font can actually encode Arabic, not merely exist."""
    import pymupdf

    try:
        font = pymupdf.Font(fontfile=str(path))
    except Exception:
        return False
    return all(font.has_glyph(ord(ch)) for ch in _PROBE if not ch.isspace())


@lru_cache(maxsize=1)
def arabic_font_path() -> Path | None:
    """First host font with real Arabic coverage, or None."""
    for candidate in _CANDIDATES:
        path = Path(candidate)
        if path.is_file() and _covers_arabic(path):
            return path
    return None


SKIP_REASON = (
    "no Arabic-capable font on this host; install fonts-noto-core (Linux) to run the "
    "PDF-level Bayan fixtures. Text-level bilingual tests still run."
)

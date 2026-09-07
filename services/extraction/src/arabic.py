"""Arabic/English text normalisation for the GCC lane.

None of this is solved by "turning on Arabic OCR". A ZATCA *Bayan* mixes Arabic labels,
Latin HS codes, and numerals that may be Arabic-Indic or ASCII inside the same document,
and each of the five failure modes below silently corrupts a figure rather than raising.

A duty amount read as ٥٠٠٠ must become 5000 before anything parses it. Getting this wrong
does not crash — it files a wrong number with a customs authority.
"""

from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------------------------------
# Digits
# --------------------------------------------------------------------------------------

# Arabic-Indic (U+0660-0669), used across the Gulf.
_ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
# Extended/Eastern Arabic-Indic (U+06F0-06F9), Persian/Urdu forms that appear in
# documents produced by regional systems.
_EASTERN_ARABIC_INDIC = "۰۱۲۳۴۵۶۷۸۹"

_DIGIT_MAP = {
    **{ord(c): str(i) for i, c in enumerate(_ARABIC_INDIC)},
    **{ord(c): str(i) for i, c in enumerate(_EASTERN_ARABIC_INDIC)},
    # Arabic decimal separator and thousands separator.
    0x066B: ".",
    0x066C: ",",
    # Arabic percent sign.
    0x066A: "%",
}


def normalise_digits(text: str) -> str:
    """Map Arabic-Indic and Eastern Arabic-Indic digits to ASCII.

    Also maps the Arabic decimal separator (U+066B) and thousands separator (U+066C),
    which are distinct code points from '.' and ',' and would otherwise survive into a
    Decimal parse and raise — or worse, truncate.
    """
    return text.translate(_DIGIT_MAP)


# --------------------------------------------------------------------------------------
# Letters
# --------------------------------------------------------------------------------------

# Tashkeel (harakat) and the tatweel elongation character carry no lexical meaning but
# break exact-match field lookup.
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")

# Orthographic variants that are the same letter for matching purposes. Issuers are not
# consistent about them, so 'رقم البيان' from two systems may not compare equal.
_LETTER_MAP = {
    "آ": "ا",  # alef with madda
    "أ": "ا",  # alef with hamza above
    "إ": "ا",  # alef with hamza below
    "ٱ": "ا",  # alef wasla
    "ى": "ي",  # alef maksura -> ya
    "ة": "ه",  # ta marbuta -> ha
    "ک": "ك",  # Persian keheh -> Arabic kaf
    "ی": "ي",  # Persian farsi yeh -> Arabic ya
}
_LETTER_TABLE = {ord(k): v for k, v in _LETTER_MAP.items()}


def normalise_arabic(text: str) -> str:
    """Fold Arabic orthographic variation so field labels compare equal.

    NFKC first, which collapses Arabic presentation forms (U+FB50-FDFF, U+FE70-FEFF)
    back to their canonical letters. Scanned and PDF-extracted Arabic frequently arrives
    in presentation form, where the same word has a different code point per position.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _TASHKEEL.sub("", text)
    return text.translate(_LETTER_TABLE)


# --------------------------------------------------------------------------------------
# Bidirectional text
# --------------------------------------------------------------------------------------

_BIDI_CONTROLS = re.compile(r"[‎‏‪-‮⁦-⁩​-‍]")


def strip_bidi_controls(text: str) -> str:
    """Remove bidi embedding/override marks and zero-width characters.

    These are invisible and survive into extracted strings, where they defeat equality
    comparison and can split a number token in two.
    """
    return _BIDI_CONTROLS.sub("", text)


# Invisible or lookalike separators that PDF font subsetting substitutes for ASCII ones.
# U+00AD SOFT HYPHEN in place of '-' is the common one and silently breaks date parsing.
_LOOKALIKES = {
    0x00AD: "-",  # soft hyphen
    0x2010: "-",  # hyphen
    0x2011: "-",  # non-breaking hyphen
    0x2012: "-",  # figure dash
    0x2013: "-",  # en dash
    0x2014: "-",  # em dash
    0x00A0: " ",  # no-break space
    0x2007: " ",  # figure space
    0x202F: " ",  # narrow no-break space
    0xFF1A: ":",  # fullwidth colon
    0x061B: ";",  # Arabic semicolon
    0x066D: "*",  # Arabic five-pointed star
}


def normalise_separators(text: str) -> str:
    """Fold lookalike punctuation to its ASCII equivalent."""
    return text.translate(_LOOKALIKES)


# Arabic-Indic numerals live inside the Arabic block but are NOT right-to-left. In the
# bidi algorithm they are class AN (Arabic Number) — weak, and rendered left to right
# even inside an RTL run. Treating them as RTL reverses every amount on the document:
# ٤٦٨٧٥٫٠٠ would come back as 00.57864. They must be classified before _RTL_CHAR.
_ARABIC_NUMERIC = re.compile(r"[٠-٩۰-۹٫٬]")

# Arabic letters plus the marks that belong to an RTL run.
_RTL_CHAR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

# Characters with no direction of their own: punctuation and spaces get absorbed into
# whichever run surrounds them.
_NEUTRAL = re.compile(r"[\s!-/:-@\[-`{-~،؛؟٭]")


def is_rtl(char: str) -> bool:
    """Whether a character is right-to-left *strong*.

    Arabic-Indic numerals are explicitly excluded — see `_ARABIC_NUMERIC`.
    """
    if _ARABIC_NUMERIC.match(char):
        return False
    return bool(_RTL_CHAR.match(char))


def to_logical_order(text: str) -> str:
    """Reconstruct logical order from a visually-ordered RTL run.

    PDF text layers do not agree on this. Conforming RTL generators store logical order;
    many real-world producers — and MuPDF's own text insertion — store *visual* order,
    where an Arabic run appears reversed and its trailing punctuation has migrated to the
    front:

        logical:  'رقم البيان: 20240115447821'
        visual:   ':نايبلا مقر20240115447821'

    Left alone, `partition(':')` on the visual form yields an empty label and the field
    is silently lost. So each maximal RTL run — together with the neutral punctuation and
    spaces attached to it — is reversed back; Latin and digit runs are left in place,
    because those are already stored in reading order.

    This is a targeted reconstruction, not the full Unicode Bidi Algorithm. It covers the
    layout customs paperwork actually uses (an RTL label, an LTR or numeric value) and
    deliberately does nothing to text with no RTL content.
    """
    if not _RTL_CHAR.search(text):
        return text

    # Per-character strong direction: True = RTL, False = LTR/numeric, None = neutral.
    # Arabic-Indic numerals resolve to False via is_rtl, so a run of them is left in
    # reading order rather than reversed.
    strengths: list[bool | None] = [
        True if is_rtl(c) else (None if _NEUTRAL.match(c) else False) for c in text
    ]

    # Paragraph direction is the direction of the first strong character (Unicode Bidi
    # rule P2/P3). For a Bayan label that is Arabic, which is why the trailing colon
    # renders on the left and must be pulled back to the right.
    base = next((s for s in strengths if s is not None), True)

    # Rules N1/N2: a neutral run takes the shared direction of the strong characters on
    # either side, or the paragraph direction when they disagree. Line boundaries count
    # as the paragraph direction.
    resolved: list[bool] = []
    index = 0
    while index < len(strengths):
        if strengths[index] is not None:
            resolved.append(bool(strengths[index]))
            index += 1
            continue
        end = index
        while end < len(strengths) and strengths[end] is None:
            end += 1
        before = next((s for s in reversed(strengths[:index]) if s is not None), base)
        after = next((s for s in strengths[end:] if s is not None), base)
        direction = before if before == after else base
        resolved.extend([bool(direction)] * (end - index))
        index = end

    # Reverse each maximal RTL run; LTR and numeric runs are already in reading order.
    # Whitespace bounding a run stays put (Unicode Bidi rule L1): it separates the label
    # from its value, and folding it into the reversal moves it to the wrong side.
    out: list[str] = []
    run_start = 0
    for position in range(len(text) + 1):
        at_end = position == len(text)
        if at_end or resolved[position] != resolved[run_start]:
            chunk = text[run_start:position]
            if resolved[run_start]:
                core = chunk.strip()
                lead = chunk[: len(chunk) - len(chunk.lstrip())]
                trail = chunk[len(chunk.rstrip()) :]
                out.append(lead + "".join(reversed(core)) + trail)
            else:
                out.append(chunk)
            run_start = position
        if at_end:
            break
    return "".join(out)


def looks_visually_ordered(text: str) -> bool:
    """Whether an RTL line appears to be stored in visual rather than logical order.

    This matters because the two are indistinguishable as character sequences: both are
    valid Unicode, and reordering unconditionally corrupts conforming documents exactly
    as badly as never reordering corrupts non-conforming ones. So the transform must be
    conditional on evidence.

    The evidence used here is narrow and reliable for form documents: a field label never
    *begins* with its separator. If a colon appears before any Arabic letter on a line
    that does contain Arabic letters, the run has been reversed and the trailing colon
    has migrated to the front.

    Known limitation: a line with no separator — a heading, a bare value — carries no
    such evidence and is left alone. Getting that right needs the glyph x-coordinates
    from the PDF, not the character stream, and no field is extracted from those lines.
    """
    first_rtl = next((i for i, c in enumerate(text) if is_rtl(c)), None)
    if first_rtl is None:
        return False
    separator = text.find(":")
    return separator != -1 and separator < first_rtl


def normalise(text: str, *, reorder: bool | None = None) -> str:
    """Full normalisation pipeline.

    `reorder` forces visual-to-logical reordering on or off. The default (None) decides
    per line via `looks_visually_ordered`; callers with access to glyph geometry should
    pass an explicit verdict instead, because geometry is decisive where the character
    stream is not.

    Order matters:
      1. bidi controls and lookalike separators removed, so run detection is not
         confused by invisible marks;
      2. NFKC and Arabic letter folding, which collapses presentation forms — reversal
         would otherwise operate on positional glyph variants;
      3. reordering, while RTL characters are still identifiable;
      4. digits last, because mapping Arabic-Indic numerals to ASCII changes their
         directional class and would break step 3.
    """
    text = normalise_separators(strip_bidi_controls(text))
    text = normalise_arabic(text)
    if reorder is None:
        reorder = looks_visually_ordered(text)
    if reorder:
        text = to_logical_order(text)
    return normalise_digits(text).strip()


# --------------------------------------------------------------------------------------
# Script detection
# --------------------------------------------------------------------------------------

_ARABIC_RANGE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN_RANGE = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """Return 'ar', 'en', or 'mixed' for a span of text.

    Used to stamp `Span.language` so an Arabic-sourced figure is distinguishable from a
    Latin-sourced one during analyst review. Digits alone are not evidence of either.
    """
    has_arabic = bool(_ARABIC_RANGE.search(text))
    has_latin = bool(_LATIN_RANGE.search(text))
    if has_arabic and has_latin:
        return "mixed"
    if has_arabic:
        return "ar"
    return "en"

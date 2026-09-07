"""ZATCA Integrated Customs Tariff parser — التعرفة الجمركية المتكاملة.

Published bilingually as a spreadsheet, exported here to CSV. Three differences from the
USITC export drive everything in this module:

**It is bilingual, and the Arabic is authoritative.** The English column is a translation.
A line may carry Arabic and no English, which is legitimate and must load — the schema's
`ck_us_lines_have_english` constraint deliberately applies to US lines only.

**The Arabic is cleaned but not folded.** ZATCA's export carries presentation-form
ligatures and embedded bidi control characters from whatever produced the spreadsheet, and
both are removed — they are invisible artefacts of the export, not part of the text. What
is *not* applied is the orthographic folding `arabic.normalise_arabic` performs for field
matching: it collapses أ إ آ to ا and ة to ه, which is right for comparing a label against
a template and wrong for storing a published schedule. A tariff description is a legal
description of the goods, and returning آلات as الات misspells it on every packet that
quotes the line.

Folding therefore belongs in the query, not in the corpus. Both sides of a search can be
folded at comparison time; the stored text stays as ZATCA published it.

**Codes are 8 or 12 digits depending on chapter**, not a fixed 10. The digits-only
normaliser handles both; nothing here assumes a length.

Column headers vary between ZATCA's published exports, so the mapping is by candidate
rather than by fixed position — a schedule that loads under one header spelling and
silently produces zero rows under another is the failure this avoids.
"""

from __future__ import annotations

import csv
import unicodedata
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.extraction.src.arabic import (
    normalise_digits,
    normalise_separators,
    strip_bidi_controls,
)
from services.ingest.src.tariff import TariffRecord, normalise_code

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

JURISDICTION = "ksa"
SOURCE = "zatca_tariff"

# Header candidates, tried in order and matched case-insensitively after whitespace is
# collapsed. Both the Arabic and English spellings ZATCA has published are listed.
_CODE_HEADERS = ("hscode", "hs_code", "code", "tariffcode", "البند", "رمزالبند", "رقمالبند")
_AR_HEADERS = ("descriptionar", "arabicdescription", "الوصف", "بيانالصنف", "وصفالصنف")
_EN_HEADERS = ("descriptionen", "englishdescription", "description", "goodsdescription")
_DUTY_HEADERS = ("dutyrate", "duty", "rate", "الفئة", "فئةالرسم", "نسبةالرسم")
_UNIT_HEADERS = ("unit", "uom", "unitofquantity", "الوحدة", "وحدةالقياس")


def _key(header: str) -> str:
    """Collapse a header to a comparison key: lowercased, no spaces or punctuation."""
    return "".join(ch for ch in header.lower() if ch.isalnum())


def _resolve_columns(fieldnames: Sequence[str]) -> dict[str, str | None]:
    """Map our field names onto whatever this export happens to call them.

    Returns `None` for a field the export does not carry. A missing English column is
    survivable; a missing code column is not, and `parse_rows` refuses the file rather
    than loading a schedule with no codes in it.
    """
    lookup = {_key(name): name for name in fieldnames}

    def first(candidates: tuple[str, ...]) -> str | None:
        for candidate in candidates:
            if candidate in lookup:
                return lookup[candidate]
        return None

    return {
        "code": first(_CODE_HEADERS),
        "description_ar": first(_AR_HEADERS),
        "description_en": first(_EN_HEADERS),
        "duty": first(_DUTY_HEADERS),
        "unit": first(_UNIT_HEADERS),
    }


def _cell(row: dict[str, Any], column: str | None) -> str | None:
    if column is None:
        return None
    value = row.get(column)
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _clean_arabic(raw: str) -> str:
    """Remove export artefacts from Arabic text without altering its spelling.

    Two passes, both non-lossy. NFKC folds the Arabic presentation forms a spreadsheet
    export leaves behind (U+FE70-FEFF) onto their canonical letters, which is a change of
    encoding rather than of spelling — أ stays أ. Bidi controls are invisible and defeat
    equality comparison, so they go.

    Orthographic folding is deliberately absent: see the module docstring.
    """
    return strip_bidi_controls(unicodedata.normalize("NFKC", raw)).strip()


def _normalise_rate(raw: str | None) -> str | None:
    """Normalise the published rate string without interpreting it.

    ZATCA writes rates with Arabic-Indic digits, the Arabic decimal separator, and
    sometimes an Arabic percent sign, so the numerals are converted to ASCII — a rate is a
    number and has no orthography to preserve. The *form* is left alone: a specific or
    compound rate stays as published, per the rule in `tariff.TariffRecord`.
    """
    if raw is None:
        return None
    cleaned = normalise_separators(normalise_digits(strip_bidi_controls(raw)))
    return cleaned.replace("\u066a", "%").strip() or None


def parse_rows(
    rows: Sequence[dict[str, Any]],
    *,
    fieldnames: Sequence[str],
    revision: str,
    effective_from: date,
) -> Iterator[TariffRecord]:
    """Turn export rows into records.

    Unlike the USITC schedule there is no indent hierarchy: ZATCA publishes each line with
    its own full description, so a row is self-contained and rows may be skipped freely.
    """
    columns = _resolve_columns(fieldnames)
    if columns["code"] is None:
        msg = (
            "ZATCA export carries no recognisable tariff-code column; headers were "
            f"{list(fieldnames)}. Add the spelling to _CODE_HEADERS rather than "
            "renaming the export, so the next one loads too."
        )
        raise ValueError(msg)

    for row in rows:
        code = normalise_code(_cell(row, columns["code"]))
        if code is None:
            continue

        arabic_raw = _cell(row, columns["description_ar"])
        arabic = _clean_arabic(arabic_raw) if arabic_raw else None
        english = _cell(row, columns["description_en"])

        if not arabic and not english:
            continue

        yield TariffRecord(
            jurisdiction=JURISDICTION,
            source=SOURCE,
            code=code,
            # A KSA line may legitimately have no English yet. Empty string rather than
            # null keeps the column non-nullable without inventing a translation.
            description_en=english or "",
            description_ar=arabic,
            unit_of_quantity=_cell(row, columns["unit"]),
            duty_rate_general=_normalise_rate(_cell(row, columns["duty"])),
            revision=revision,
            effective_from=effective_from,
        )


def parse_file(path: Path, *, revision: str, effective_from: date) -> Iterator[TariffRecord]:
    """Parse a ZATCA tariff export from CSV.

    `utf-8-sig` because ZATCA's exports carry a byte-order mark, which would otherwise
    end up glued to the first header name and defeat the column resolution.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    yield from parse_rows(
        rows, fieldnames=fieldnames, revision=revision, effective_from=effective_from
    )

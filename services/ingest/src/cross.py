"""CBP CROSS rulings parser.

CROSS is the Customs Rulings Online Search System — the body of CBP classification rulings
that turn a heading into a defensible position. A claim citing a ruling that actually
addresses the article survives a desk audit; one citing a plausible heading does not.

Two things this parser is careful about:

**Revocations.** A ruling may be revoked or modified by a later one. CROSS records this in
the ruling text rather than in a field, so `detect_supersession` reads it out of the body.
A revoked ruling is *kept*, flagged — deleting it would erase the basis a claim was filed
on while it was still good law.

**Multiple tariff codes per ruling.** A ruling frequently classifies several articles. The
first is taken as `classified_code` and the rest are appended to the body, so a search for
any of them still finds the ruling. Storing one row per code would multiply-count a single
ruling in the result set and make three citations look like three authorities.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.ingest.src.tariff import RulingRecord, normalise_code

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

JURISDICTION = "us"
SOURCE = "cbp_cross"

# CROSS ruling numbers: a letter prefix, digits, sometimes a suffix. HQ H123456,
# NY N012345, HQ 967890.
_RULING_NUMBER = re.compile(r"\b((?:HQ|NY|PD|CLA)[\s-]?[A-Z]?\d{5,6})\b", re.IGNORECASE)

# The revocation language CROSS uses. Deliberately narrow: "revoked by" and "modified by"
# name a superseding ruling, whereas a body that merely mentions "revocation" in passing
# does not, and treating the second as the first would retire good rulings.
_SUPERSEDED = re.compile(
    r"\b(?:revoked|modified|superseded)\s+by\s+((?:HQ|NY|PD|CLA)[\s-]?[A-Z]?\d{5,6})\b",
    re.IGNORECASE,
)

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%B %d, %Y", "%Y%m%d")


def _text(row: dict[str, Any], *keys: str) -> str | None:
    """First non-empty value among several candidate keys.

    The CROSS export has changed its column names more than once; accepting the known
    spellings costs nothing and avoids a load that silently produces empty subjects.
    """
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            value = " ".join(str(part) for part in value if part)
        stripped = str(value).strip()
        if stripped:
            return stripped
    return None


def parse_date(raw: str | None) -> date | None:
    """Parse a ruling date in any of the formats CROSS has published.

    Returns `None` rather than a default. A ruling with an unparseable date is skipped: the
    date decides whether the ruling was in force for a given entry, and a guessed one would
    make an out-of-force ruling look citable.
    """
    if not raw:
        return None
    cleaned = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def detect_supersession(body: str, explicit: str | None = None) -> str | None:
    """The ruling that superseded this one, if the text says so.

    An explicit column wins when present. Only the body regex is a heuristic, and it is
    written to fire on the phrase that names a successor rather than on the mere presence
    of the word.
    """
    if explicit:
        return explicit.strip() or None
    match = _SUPERSEDED.search(body)
    return match.group(1).upper().replace(" ", " ") if match else None


def _codes(row: dict[str, Any]) -> list[str]:
    """Every tariff code the ruling classifies, in document order.

    Accepts a list, a delimited string, or a single code — the export has used all three.
    """
    raw = row.get("tariffs") or row.get("tariff") or row.get("classified_code") or ""
    if isinstance(raw, list):
        parts: Sequence[str] = [str(part) for part in raw]
    else:
        parts = re.split(r"[;,|]\s*", str(raw))

    codes: list[str] = []
    for part in parts:
        code = normalise_code(part)
        if code and code not in codes:
            codes.append(code)
    return codes


def parse_rows(rows: Sequence[dict[str, Any]]) -> Iterator[RulingRecord]:
    """Turn CROSS export rows into ruling records."""
    for row in rows:
        number = _text(row, "rulingNumber", "ruling_number", "number")
        if not number:
            continue
        number = number.upper().strip()

        ruling_date = parse_date(_text(row, "rulingDate", "ruling_date", "date"))
        if ruling_date is None:
            continue

        codes = _codes(row)
        if not codes:
            continue

        subject = _text(row, "subject", "title", "category") or number
        body = _text(row, "rulingText", "body", "text", "content") or ""

        if len(codes) > 1:
            # Keep the secondary codes searchable without duplicating the ruling. Trigram
            # search runs over subject and body together, so appending reaches them.
            body = f"{body}\n\nAlso classified: {', '.join(codes[1:])}"

        yield RulingRecord(
            jurisdiction=JURISDICTION,
            source=SOURCE,
            ruling_number=number,
            ruling_date=ruling_date,
            classified_code=codes[0],
            subject=subject,
            body=body,
            url=_text(row, "url", "link"),
            superseded_by=detect_supersession(
                body, _text(row, "supersededBy", "superseded_by", "revokedBy")
            ),
        )


def parse_file(path: Path) -> Iterator[RulingRecord]:
    """Parse a downloaded CROSS export, JSON or CSV."""
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("rulings", [])
    else:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    yield from parse_rows(rows)

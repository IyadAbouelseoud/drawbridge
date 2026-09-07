"""USITC Harmonized Tariff Schedule parser.

The schedule is published at `hts.usitc.gov/reststop/exportList` as JSON, and as CSV from
the same site. Both are the same tree flattened into rows, and the flattening is the whole
difficulty: a row's `description` is relative to its indent, so line 8471.30.01.00 reads
"Other" and means the concatenation of four ancestors. `resolve_hierarchy` rebuilds them.

Rows without an `htsno` are structural — chapter headings, notes, superior text carrying
the parent description for the indented lines beneath. They are not classifiable and are
skipped, but they still *participate in the hierarchy*, because dropping them before the
indent walk would reattach every child to the wrong ancestor.

No network call. The export is downloaded once and ingested from a file, so a corpus load
is reproducible and a classification made in 2026 can be reproduced in 2031 without
depending on a USITC endpoint still existing.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.ingest.src.tariff import TariffRecord, normalise_code, resolve_hierarchy

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

JURISDICTION = "us"
SOURCE = "usitc_hts"

# Column names in the USITC JSON export. The CSV export uses the same names as its header
# row, so one mapping serves both.
_HTSNO = "htsno"
_INDENT = "indent"
_DESCRIPTION = "description"
_UNITS = "units"
_GENERAL = "general"
_SPECIAL = "special"
_OTHER = "other"


def _text(row: dict[str, Any], key: str) -> str | None:
    """A trimmed cell, or None where the export wrote an empty string.

    The export uses "" for absent rather than null, and an empty duty rate is not the same
    fact as a rate of zero — one means the schedule was silent, the other means Free.
    """
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        # `units` arrives as an array in the JSON export, e.g. ["No.", "kg"].
        value = ", ".join(str(part) for part in value if part)
    stripped = str(value).strip()
    return stripped or None


def _indent_of(row: dict[str, Any]) -> int:
    raw = row.get(_INDENT, 0)
    try:
        return int(str(raw).strip() or 0)
    except ValueError:
        return 0


def parse_rows(
    rows: Sequence[dict[str, Any]], *, revision: str, effective_from: date
) -> Iterator[TariffRecord]:
    """Turn export rows into records, resolving the indent hierarchy first.

    Every row feeds the hierarchy walk; only rows carrying a usable code are emitted. That
    ordering is the point — a structural row with no `htsno` is precisely the row holding
    the description its children inherit.
    """
    hierarchy = [(_indent_of(row), _text(row, _DESCRIPTION) or "") for row in rows]
    full = dict(resolve_hierarchy(hierarchy))

    for index, row in enumerate(rows):
        code = normalise_code(_text(row, _HTSNO))
        if code is None:
            continue

        description = full.get(index, "").strip()
        if not description:
            continue

        yield TariffRecord(
            jurisdiction=JURISDICTION,
            source=SOURCE,
            code=code,
            description_en=description,
            unit_of_quantity=_text(row, _UNITS),
            duty_rate_general=_text(row, _GENERAL),
            duty_rate_special=_text(row, _SPECIAL),
            duty_rate_column2=_text(row, _OTHER),
            revision=revision,
            effective_from=effective_from,
        )


def parse_file(path: Path, *, revision: str, effective_from: date) -> Iterator[TariffRecord]:
    """Parse a downloaded USITC export, JSON or CSV, chosen by suffix.

    The whole file is read before parsing rather than streamed: the indent hierarchy needs
    ancestors, so a row cannot be resolved without the rows above it, and a schedule
    export is a few tens of megabytes at most.
    """
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("results", [])
    else:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    yield from parse_rows(rows, revision=revision, effective_from=effective_from)

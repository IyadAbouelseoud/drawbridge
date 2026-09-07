"""Shared shape and load path for every tariff corpus.

Three sources feed one table (`services/api/src/hts_models.py`): the USITC HTS, the ZATCA
Integrated Customs Tariff, and CBP CROSS rulings. They arrive in three different formats
and agree on almost nothing, so each parser is separate and each produces the same record.

Two decisions worth naming, because both are the opposite of the obvious one:

**Revisions are inserted, not replaced.** A claim is classified against the schedule in
force on its entry date, and a five-year lookback spans several revisions. Overwriting on
reload would silently reclassify old claims against a schedule that did not exist when
they were entered. The unique key therefore includes `revision`, and a reload of the same
revision updates only the descriptive fields.

**Duty rates stay as published text.** "Free", "2.5%", "6.5c/kg", "4.4c/kg + 2.8%" — a
float column loses the specific and compound forms silently, and silently is exactly how
a compound rate turns into an understated claim. `ad_valorem_rate` is populated only where
the rate is purely ad valorem, and the text column stays authoritative in every case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from sqlalchemy.orm import Session

# Purely ad valorem: an optional sign, digits, optional decimals, a percent sign, and
# nothing else. "2.5% + 6.5c/kg" deliberately fails this — a compound rate is not an
# ad valorem rate, and treating it as one understates the duty.
_AD_VALOREM = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")

# "Free" in every casing the schedules use. Distinct from an absent rate: a free line is
# classified and carries zero duty; a null rate means the schedule did not say.
_FREE = re.compile(r"^\s*free\s*$", re.IGNORECASE)

_DIGITS = re.compile(r"\D")


@dataclass(frozen=True, slots=True)
class TariffRecord:
    """One schedule line, normalised across sources."""

    jurisdiction: str
    source: str
    code: str
    description_en: str
    revision: str
    effective_from: date
    description_ar: str | None = None
    unit_of_quantity: str | None = None
    duty_rate_general: str | None = None
    duty_rate_special: str | None = None
    duty_rate_column2: str | None = None
    effective_to: date | None = None

    @property
    def heading(self) -> str:
        return self.code[:4]

    @property
    def hs6(self) -> str:
        return self.code[:6]

    @property
    def ad_valorem_rate(self) -> Decimal | None:
        return parse_ad_valorem(self.duty_rate_general)


@dataclass(frozen=True, slots=True)
class RulingRecord:
    """One classification ruling."""

    jurisdiction: str
    source: str
    ruling_number: str
    ruling_date: date
    classified_code: str
    subject: str
    body: str
    url: str | None = None
    superseded_by: str | None = None

    @property
    def hs6(self) -> str:
        return self.classified_code[:6]


@dataclass(slots=True)
class IngestReport:
    """What a load actually did, including what it refused.

    `skipped` is not noise. A schedule export carries chapter headings, statistical notes
    and 4-digit headings that are not classifiable lines, and a loader that silently drops
    them is indistinguishable from one that silently drops real lines. The counts are
    reported so a load that suddenly skips 40% of the file is visible.
    """

    source: str
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
        }


# --------------------------------------------------------------------------- parsing


def normalise_code(raw: str | None) -> str | None:
    """Strip a published tariff code to digits.

    Schedules publish "8471.30.01.00", "8471300100" and "8471.30" interchangeably. The
    dots are presentation. Anything shorter than six digits is a chapter or heading rather
    than a classifiable line and is rejected here rather than stored as a stub.
    """
    if not raw:
        return None
    digits = _DIGITS.sub("", raw)
    if len(digits) < 6 or len(digits) > 12:
        return None
    return digits


def parse_ad_valorem(rate: str | None) -> Decimal | None:
    """The ad valorem percentage, where the rate is purely ad valorem.

    Returns zero for "Free" and `None` for specific, compound or absent rates. `None` is
    the honest answer for a compound rate: any number here would be wrong in a direction
    the caller cannot see.
    """
    if rate is None:
        return None
    if _FREE.match(rate):
        return Decimal("0")
    match = _AD_VALOREM.match(rate)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:  # pragma: no cover - regex already constrains the shape
        return None


def resolve_hierarchy(
    rows: Sequence[tuple[int, str]],
) -> Iterator[tuple[int, str]]:
    """Expand indent-relative descriptions into full ones.

    The USITC export is a tree flattened into rows: a line at indent 3 reads "Other" and
    means "Automatic data processing machines > Portable > Weighing not more than 10 kg >
    Other". Storing "Other" makes the corpus unsearchable — every chapter has dozens of
    them, and trigram similarity against "Other" matches everything and identifies nothing.

    Yields `(row_index, full_description)` for each input row, joining ancestors with a
    comma. An indent that jumps by more than one level is tolerated by truncating the
    stack, because real exports contain them and refusing the file over a formatting
    artefact would lose the whole schedule.
    """
    stack: list[str] = []
    for index, (indent, description) in enumerate(rows):
        level = max(indent, 0)
        del stack[level:]
        while len(stack) < level:
            # Gap in the indent sequence. Pad rather than misattribute the ancestor.
            stack.append("")
        stack.append(description.strip())
        parts = [part for part in stack if part]
        yield index, ", ".join(parts)


# ----------------------------------------------------------------------------- loading

_UPSERT_LINE = text("""
    INSERT INTO tariff_lines (
        jurisdiction, source, code, heading, hs6,
        description_en, description_ar, unit_of_quantity,
        duty_rate_general, duty_rate_special, duty_rate_column2, ad_valorem_rate,
        revision, effective_from, effective_to
    ) VALUES (
        :jurisdiction, :source, :code, :heading, :hs6,
        :description_en, :description_ar, :unit_of_quantity,
        :duty_rate_general, :duty_rate_special, :duty_rate_column2, :ad_valorem_rate,
        :revision, :effective_from, :effective_to
    )
    ON CONFLICT (jurisdiction, source, code, revision) DO UPDATE SET
        description_en    = EXCLUDED.description_en,
        description_ar    = COALESCE(EXCLUDED.description_ar, tariff_lines.description_ar),
        unit_of_quantity  = EXCLUDED.unit_of_quantity,
        duty_rate_general = EXCLUDED.duty_rate_general,
        duty_rate_special = EXCLUDED.duty_rate_special,
        duty_rate_column2 = EXCLUDED.duty_rate_column2,
        ad_valorem_rate   = EXCLUDED.ad_valorem_rate,
        effective_to      = EXCLUDED.effective_to
    RETURNING (xmax = 0) AS inserted
""")

_UPSERT_RULING = text("""
    INSERT INTO tariff_rulings (
        jurisdiction, source, ruling_number, ruling_date,
        classified_code, hs6, subject, body, url, superseded_by
    ) VALUES (
        :jurisdiction, :source, :ruling_number, :ruling_date,
        :classified_code, :hs6, :subject, :body, :url, :superseded_by
    )
    ON CONFLICT (source, ruling_number) DO UPDATE SET
        ruling_date     = EXCLUDED.ruling_date,
        classified_code = EXCLUDED.classified_code,
        hs6             = EXCLUDED.hs6,
        subject         = EXCLUDED.subject,
        body            = EXCLUDED.body,
        url             = EXCLUDED.url,
        superseded_by   = EXCLUDED.superseded_by
    RETURNING (xmax = 0) AS inserted
""")


def load_tariff_lines(
    session: Session, records: Iterable[TariffRecord], *, source: str, batch_size: int = 500
) -> IngestReport:
    """Upsert schedule lines, reporting inserts against updates.

    `xmax = 0` distinguishes the two on the way back out of Postgres. It matters: a reload
    that reports 12,000 inserts when it should report 12,000 updates means the revision
    string changed, and a duplicated revision is how the same schedule ends up searched
    twice under two names.
    """
    report = IngestReport(source=source)
    pending = 0

    for record in records:
        rate = record.ad_valorem_rate
        row = session.execute(
            _UPSERT_LINE,
            {
                "jurisdiction": record.jurisdiction,
                "source": record.source,
                "code": record.code,
                "heading": record.heading,
                "hs6": record.hs6,
                "description_en": record.description_en,
                "description_ar": record.description_ar,
                "unit_of_quantity": record.unit_of_quantity,
                "duty_rate_general": record.duty_rate_general,
                "duty_rate_special": record.duty_rate_special,
                "duty_rate_column2": record.duty_rate_column2,
                "ad_valorem_rate": rate,
                "revision": record.revision,
                "effective_from": record.effective_from,
                "effective_to": record.effective_to,
            },
        ).scalar_one()

        if row:
            report.inserted += 1
        else:
            report.updated += 1

        pending += 1
        if pending >= batch_size:
            session.flush()
            pending = 0

    session.flush()
    return report


def load_rulings(
    session: Session, records: Iterable[RulingRecord], *, source: str, batch_size: int = 200
) -> IngestReport:
    """Upsert classification rulings.

    A revoked ruling is updated in place with `superseded_by` rather than deleted: a claim
    filed while it was good law relied on it, and an audit years later needs to see both
    the reliance and the revocation.
    """
    report = IngestReport(source=source)
    pending = 0

    for record in records:
        row = session.execute(
            _UPSERT_RULING,
            {
                "jurisdiction": record.jurisdiction,
                "source": record.source,
                "ruling_number": record.ruling_number,
                "ruling_date": record.ruling_date,
                "classified_code": record.classified_code,
                "hs6": record.hs6,
                "subject": record.subject,
                "body": record.body,
                "url": record.url,
                "superseded_by": record.superseded_by,
            },
        ).scalar_one()

        if row:
            report.inserted += 1
        else:
            report.updated += 1

        pending += 1
        if pending >= batch_size:
            session.flush()
            pending = 0

    session.flush()
    return report

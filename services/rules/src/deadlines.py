"""Statutory deadline arithmetic.

The whole reason this module exists: "six Gregorian months" is not 180 days. Depending
on which months are spanned it is 181 to 184 days, and a 180-day constant files late for
most of the year. Likewise "one Gregorian year" is 366 days across a leap day.

Calendar arithmetic here is exact. Nothing approximates a month as 30 days.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from drawbridge_schemas.jurisdiction import (
    Deadline,
    DeadlineUnit,
    JurisdictionProfile,
)


def add_gregorian_months(start: date, months: int) -> date:
    """Add calendar months, clamping to the last valid day of the target month.

    31 Jan + 1 month is 28/29 Feb, not 3 March. Clamping is the conservative direction
    for a filing deadline: it never produces a date later than the statute allows.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_gregorian_years(start: date, years: int) -> date:
    """Add calendar years. 29 Feb clamps to 28 Feb in a non-leap target year."""
    return add_gregorian_months(start, years * 12)


def apply(start: date, deadline: Deadline) -> date:
    """Project a statutory period from a start date, in the unit the statute uses."""
    match deadline.unit:
        case DeadlineUnit.DAYS:
            return date.fromordinal(start.toordinal() + deadline.amount)
        case DeadlineUnit.GREGORIAN_MONTHS:
            return add_gregorian_months(start, deadline.amount)
        case DeadlineUnit.GREGORIAN_YEARS:
            return add_gregorian_years(start, deadline.amount)


@dataclass(frozen=True, slots=True)
class EligibilityWindow:
    """The three dates that bound a claim, with the citation for each."""

    clock_start: date
    """Jurisdiction anchor: US import date, GCC duty-payment date."""

    reexport_deadline: date
    """Goods must leave by this date."""

    filing_deadline: date
    """Claim must be filed by this date, measured from actual export."""

    absolute_bar: date | None
    """Hard prescription from the clock start, regardless of anything else."""

    def export_in_window(self, export_date: date) -> bool:
        return self.clock_start <= export_date <= self.reexport_deadline

    def filing_in_window(self, filing_date: date) -> bool:
        if filing_date > self.filing_deadline:
            return False
        return self.absolute_bar is None or filing_date <= self.absolute_bar

    @property
    def effective_filing_deadline(self) -> date:
        """The earlier of the lane deadline and the absolute bar.

        GCC claims can hit Article 174's three-year prescription before the six-month
        filing clock expires, when the re-export happens late in the window.
        """
        if self.absolute_bar is None:
            return self.filing_deadline
        return min(self.filing_deadline, self.absolute_bar)


def window_for(
    profile: JurisdictionProfile, clock_start: date, export_date: date
) -> EligibilityWindow:
    """Compute the eligibility window for one import/export pair.

    `clock_start` comes from `EntryLine.eligibility_clock_start`, which already resolves
    the jurisdiction's anchor (import date vs. duty-payment date).
    """
    return EligibilityWindow(
        clock_start=clock_start,
        reexport_deadline=apply(clock_start, profile.reexport_window),
        filing_deadline=apply(export_date, profile.claim_filing_deadline),
        absolute_bar=(apply(clock_start, profile.absolute_bar) if profile.absolute_bar else None),
    )

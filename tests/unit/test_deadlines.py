"""Statutory deadline arithmetic.

The reason this module exists at all: "six Gregorian months" is not 180 days, and
"one Gregorian year" is not always 365. Both approximations were carried into the
project brief; both file late or early. These tests pin the exact behaviour.
"""

from __future__ import annotations

from datetime import date

import pytest

from drawbridge_schemas.jurisdiction import KSA_PROFILE, US_PROFILE
from services.rules.src.deadlines import (
    add_gregorian_months,
    add_gregorian_years,
    window_for,
)


class TestGregorianMonths:
    @pytest.mark.parametrize(
        ("start", "months", "expected", "day_count"),
        [
            # Six months from a January re-export spans Feb-Jul: 181 days, not 180.
            (date(2024, 1, 31), 6, date(2024, 7, 31), 182),
            # From July it spans Aug-Jan: 184 days.
            (date(2024, 7, 15), 6, date(2025, 1, 15), 184),
            # From March 2023 (non-leap): 184 days.
            (date(2023, 3, 10), 6, date(2023, 9, 10), 184),
        ],
    )
    def test_six_months_is_never_exactly_180_days(
        self, start: date, months: int, expected: date, day_count: int
    ) -> None:
        result = add_gregorian_months(start, months)
        assert result == expected
        assert (result - start).days == day_count
        assert (result - start).days != 180

    def test_clamps_to_last_day_of_shorter_month(self) -> None:
        # 31 Jan + 1 month is 29 Feb in a leap year, not 2 March.
        assert add_gregorian_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
        assert add_gregorian_months(date(2023, 1, 31), 1) == date(2023, 2, 28)

    def test_rolls_across_year_boundary(self) -> None:
        assert add_gregorian_months(date(2024, 11, 15), 3) == date(2025, 2, 15)


class TestGregorianYears:
    def test_one_year_across_a_leap_day_is_366_days(self) -> None:
        start = date(2023, 3, 1)
        result = add_gregorian_years(start, 1)
        assert result == date(2024, 3, 1)
        assert (result - start).days == 366

    def test_leap_day_clamps_in_non_leap_target(self) -> None:
        assert add_gregorian_years(date(2024, 2, 29), 1) == date(2025, 2, 28)


class TestEligibilityWindow:
    def test_gcc_window_runs_from_duty_payment(self) -> None:
        """Art. 16 §3(a): one Gregorian year from the date duties were paid."""
        payment = date(2024, 2, 8)
        export = date(2024, 9, 12)
        window = window_for(KSA_PROFILE, payment, export)

        assert window.reexport_deadline == date(2025, 2, 8)
        assert window.export_in_window(export)
        # Art. 16 §3(b): six Gregorian months from re-export.
        assert window.filing_deadline == date(2025, 3, 12)
        # Art. 174: three-year absolute bar from payment.
        assert window.absolute_bar == date(2027, 2, 8)

    def test_gcc_export_after_one_year_is_out_of_window(self) -> None:
        payment = date(2024, 2, 8)
        window = window_for(KSA_PROFILE, payment, date(2025, 3, 1))
        assert not window.export_in_window(date(2025, 3, 1))

    def test_gcc_filing_a_day_late_is_rejected(self) -> None:
        window = window_for(KSA_PROFILE, date(2024, 2, 8), date(2024, 9, 12))
        assert window.filing_in_window(date(2025, 3, 12))
        assert not window.filing_in_window(date(2025, 3, 13))

    def test_180_day_constant_would_have_filed_late(self) -> None:
        """The concrete cost of the approximation.

        Re-export 12 Sep 2024. Six Gregorian months is 12 Mar 2025 (181 days). A
        180-day constant lands on 11 Mar and would reject a claim that is still valid,
        or — filed the other way round — a deadline computed as 180 days from a
        re-export in a long-month span expires before the statute does.
        """
        export = date(2024, 9, 12)
        window = window_for(KSA_PROFILE, date(2024, 2, 8), export)
        naive_180 = date.fromordinal(export.toordinal() + 180)

        assert window.filing_deadline == date(2025, 3, 12)
        assert naive_180 == date(2025, 3, 11)
        assert window.filing_deadline != naive_180

    def test_absolute_bar_can_bite_before_the_filing_clock(self) -> None:
        """A late re-export can hit Art. 174 before the six-month clock expires."""
        payment = date(2022, 3, 1)
        export = date(2023, 2, 20)  # inside the one-year window
        window = window_for(KSA_PROFILE, payment, export)

        assert window.filing_deadline == date(2023, 8, 20)
        assert window.absolute_bar == date(2025, 3, 1)
        # Here the lane deadline is the binding one.
        assert window.effective_filing_deadline == date(2023, 8, 20)

    def test_us_window_runs_from_import_and_has_no_bar(self) -> None:
        imported = date(2023, 3, 14)
        export = date(2024, 1, 9)
        window = window_for(US_PROFILE, imported, export)

        assert window.reexport_deadline == date(2028, 3, 14)
        assert window.filing_deadline == date(2027, 1, 9)
        assert window.absolute_bar is None
        assert window.effective_filing_deadline == date(2027, 1, 9)

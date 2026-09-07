"""Parsers for the three tariff corpora.

These test the parse, not the load: a load needs Postgres and lives in the integration
suite. What breaks here breaks silently in production — a hierarchy walk that reattaches a
line to the wrong ancestor produces a corpus that searches cleanly and classifies wrongly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

from services.ingest.src import cross, usitc, zatca
from services.ingest.src.tariff import (
    IngestReport,
    normalise_code,
    parse_ad_valorem,
    resolve_hierarchy,
)

EFFECTIVE = date(2026, 1, 1)


class TestCodeNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("8471.30.01.00", "8471300100"),
            ("8471300100", "8471300100"),
            ("847130", "847130"),
            ("8471.30", "847130"),
            ("  8471.30.01  ", "84713001"),
        ],
    )
    def test_digits_are_extracted(self, raw: str, expected: str) -> None:
        assert normalise_code(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "84", "8471.", "chapter 84", "8471300100123456"])
    def test_unclassifiable_codes_are_rejected(self, raw: str | None) -> None:
        """A 4-digit heading is not a classifiable line and must not become a stub row."""
        assert normalise_code(raw) is None


class TestAdValoremParsing:
    def test_pure_ad_valorem(self) -> None:
        assert parse_ad_valorem("2.5%") == Decimal("2.5")
        assert parse_ad_valorem(" 15 % ") == Decimal("15")

    def test_free_is_zero_not_null(self) -> None:
        assert parse_ad_valorem("Free") == Decimal("0")
        assert parse_ad_valorem("free") == Decimal("0")

    @pytest.mark.parametrize(
        "rate", ["6.5c/kg", "4.4c/kg + 2.8%", "2.5% + 6.5c/kg", "$0.02/liter", None, ""]
    )
    def test_specific_and_compound_rates_yield_none(self, rate: str | None) -> None:
        """A compound rate has no single ad valorem number. None is the honest answer."""
        assert parse_ad_valorem(rate) is None


class TestHierarchyResolution:
    def test_ancestors_are_joined(self) -> None:
        rows = [
            (0, "Automatic data processing machines"),
            (1, "Portable"),
            (2, "Weighing not more than 10 kg"),
            (3, "Other"),
        ]
        resolved = dict(resolve_hierarchy(rows))
        assert resolved[3] == (
            "Automatic data processing machines, Portable, Weighing not more than 10 kg, Other"
        )

    def test_a_sibling_replaces_rather_than_appends(self) -> None:
        rows = [(0, "Machines"), (1, "Portable"), (1, "Other")]
        resolved = dict(resolve_hierarchy(rows))
        assert resolved[2] == "Machines, Other"

    def test_returning_to_a_shallower_level_drops_the_deeper_ancestors(self) -> None:
        rows = [(0, "A"), (1, "B"), (2, "C"), (1, "D")]
        resolved = dict(resolve_hierarchy(rows))
        assert resolved[3] == "A, D"

    def test_an_indent_jump_is_tolerated(self) -> None:
        """Real exports contain them. Losing the schedule over a formatting artefact is worse."""
        rows = [(0, "A"), (3, "B")]
        resolved = dict(resolve_hierarchy(rows))
        assert resolved[1] == "A, B"


class TestUsitcParser:
    @pytest.fixture
    def rows(self) -> list[dict[str, object]]:
        return [
            {"htsno": "8471", "indent": "0", "description": "Automatic data processing machines"},
            {"htsno": "", "indent": "1", "description": "Portable, weighing not over 10 kg"},
            {
                "htsno": "8471.30.01.00",
                "indent": "2",
                "description": "Other",
                "units": ["No."],
                "general": "Free",
                "special": "",
                "other": "35%",
            },
        ]

    def test_structural_rows_are_skipped_but_still_carry_the_hierarchy(
        self, rows: list[dict[str, object]]
    ) -> None:
        """The row with no htsno holds the description its child inherits."""
        records = list(usitc.parse_rows(rows, revision="2026-r3", effective_from=EFFECTIVE))
        # 8471 is a 4-digit heading, rejected by normalise_code; the blank row has no code.
        assert [r.code for r in records] == ["8471300100"]
        assert records[0].description_en == (
            "Automatic data processing machines, Portable, weighing not over 10 kg, Other"
        )

    def test_rates_are_kept_as_published(self, rows: list[dict[str, object]]) -> None:
        record = next(usitc.parse_rows(rows, revision="2026-r3", effective_from=EFFECTIVE))
        assert record.duty_rate_general == "Free"
        assert record.duty_rate_column2 == "35%"
        assert record.duty_rate_special is None
        assert record.ad_valorem_rate == Decimal("0")

    def test_units_arrive_as_a_list_and_are_flattened(self) -> None:
        rows = [
            {
                "htsno": "8471.30.01.00",
                "indent": "0",
                "description": "Machines",
                "units": ["No.", "kg"],
            }
        ]
        record = next(usitc.parse_rows(rows, revision="r", effective_from=EFFECTIVE))
        assert record.unit_of_quantity == "No., kg"

    def test_derived_keys(self) -> None:
        rows = [{"htsno": "8471.30.01.00", "indent": "0", "description": "Machines"}]
        record = next(usitc.parse_rows(rows, revision="r", effective_from=EFFECTIVE))
        assert record.heading == "8471"
        assert record.hs6 == "847130"
        assert record.jurisdiction == "us"
        assert record.source == "usitc_hts"


class TestZatcaParser:
    HEADERS: ClassVar[list[str]] = [
        "HS Code",
        "الوصف",
        "Description EN",
        "Duty Rate",
        "Unit",
    ]

    def test_bilingual_lines_load(self) -> None:
        rows = [
            {
                "HS Code": "8471.30.00",
                "الوصف": "آلات معالجة بيانات أوتوماتيكية محمولة",
                "Description EN": "Portable automatic data processing machines",
                "Duty Rate": "٥٪",
                "Unit": "PCE",
            }
        ]
        record = next(
            zatca.parse_rows(
                rows, fieldnames=self.HEADERS, revision="2025-ICT", effective_from=EFFECTIVE
            )
        )
        assert record.code == "84713000"
        assert record.jurisdiction == "ksa"
        assert record.description_en == "Portable automatic data processing machines"
        assert record.description_ar is not None

    def test_arabic_indic_digits_in_the_rate_are_normalised(self) -> None:
        rows = [{"HS Code": "84713000", "الوصف": "آلات", "Duty Rate": "٥٪"}]
        record = next(
            zatca.parse_rows(
                rows,
                fieldnames=["HS Code", "الوصف", "Duty Rate"],
                revision="r",
                effective_from=EFFECTIVE,
            )
        )
        assert record.duty_rate_general == "5%"
        assert record.ad_valorem_rate == Decimal("5")

    def test_the_published_arabic_spelling_is_preserved(self) -> None:
        """Storage keeps the schedule's own orthography.

        `arabic.normalise_arabic` folds أ إ آ to ا and ة to ه, which is right for matching
        a field label and wrong for a legal description of goods: a packet quoting the
        line would misspell it.
        """
        rows = [
            {
                "HS Code": "84713000",
                "الوصف": "آلات معالجة بيانات أوتوماتيكية",
                "Description EN": "Machines",
            }
        ]
        record = next(
            zatca.parse_rows(rows, fieldnames=self.HEADERS, revision="r", effective_from=EFFECTIVE)
        )
        assert record.description_ar == "آلات معالجة بيانات أوتوماتيكية"

    def test_bidi_controls_and_presentation_forms_are_stripped(self) -> None:
        """Export artefacts, not text. Both are invisible or an encoding variant."""
        rows = [{"HS Code": "84713000", "الوصف": "‏ﺱﻴﺎ‎"}]
        record = next(
            zatca.parse_rows(
                rows,
                fieldnames=["HS Code", "الوصف"],
                revision="r",
                effective_from=EFFECTIVE,
            )
        )
        assert record.description_ar == "سيا"

    def test_an_arabic_only_line_loads_with_an_empty_english_description(self) -> None:
        """The ck_us_lines_have_english constraint is US-only, deliberately."""
        rows = [{"HS Code": "84713000", "الوصف": "آلات معالجة بيانات"}]
        record = next(
            zatca.parse_rows(
                rows, fieldnames=["HS Code", "الوصف"], revision="r", effective_from=EFFECTIVE
            )
        )
        assert record.description_en == ""
        assert record.description_ar

    def test_alternate_header_spellings_resolve(self) -> None:
        rows = [{"hs_code": "84713000", "arabicDescription": "آلات", "description": "Machines"}]
        record = next(
            zatca.parse_rows(
                rows,
                fieldnames=["hs_code", "arabicDescription", "description"],
                revision="r",
                effective_from=EFFECTIVE,
            )
        )
        assert record.code == "84713000"
        assert record.description_en == "Machines"

    def test_a_file_with_no_code_column_is_refused_loudly(self) -> None:
        with pytest.raises(ValueError, match="no recognisable tariff-code column"):
            list(
                zatca.parse_rows(
                    [{"foo": "bar"}],
                    fieldnames=["foo"],
                    revision="r",
                    effective_from=EFFECTIVE,
                )
            )

    def test_a_row_with_neither_description_is_skipped(self) -> None:
        rows = [{"HS Code": "84713000", "الوصف": "", "Description EN": ""}]
        assert not list(
            zatca.parse_rows(rows, fieldnames=self.HEADERS, revision="r", effective_from=EFFECTIVE)
        )


class TestCrossParser:
    def test_a_ruling_parses(self) -> None:
        rows = [
            {
                "rulingNumber": "ny n012345",
                "rulingDate": "2019-03-14",
                "tariffs": "8471.30.0100",
                "subject": "Classification of a ruggedised field laptop",
                "rulingText": "The applicable subheading will be 8471.30.0100.",
                "url": "https://rulings.cbp.gov/ruling/N012345",
            }
        ]
        record = next(cross.parse_rows(rows))
        assert record.ruling_number == "NY N012345"
        assert record.classified_code == "8471300100"
        assert record.hs6 == "847130"
        assert record.jurisdiction == "us"
        assert record.superseded_by is None

    @pytest.mark.parametrize("raw", ["2019-03-14", "03/14/2019", "March 14, 2019", "20190314"])
    def test_date_formats(self, raw: str) -> None:
        assert cross.parse_date(raw) == date(2019, 3, 14)

    def test_an_unparseable_date_skips_the_ruling(self) -> None:
        """A guessed date would make an out-of-force ruling look citable."""
        assert cross.parse_date("sometime in 2019") is None
        rows = [{"rulingNumber": "HQ H123456", "rulingDate": "sometime", "tariffs": "8471300100"}]
        assert not list(cross.parse_rows(rows))

    def test_revocation_is_read_out_of_the_body(self) -> None:
        body = "This ruling is revoked by HQ H987654 effective 1 January 2021."
        assert cross.detect_supersession(body) == "HQ H987654"

    def test_a_passing_mention_of_revocation_does_not_retire_a_ruling(self) -> None:
        body = "The revocation procedure under 19 CFR 177.12 was considered and not applied."
        assert cross.detect_supersession(body) is None

    def test_an_explicit_column_wins_over_the_body(self) -> None:
        body = "revoked by HQ H111111"
        assert cross.detect_supersession(body, "HQ H222222") == "HQ H222222"

    def test_multiple_codes_keep_one_row_and_stay_searchable(self) -> None:
        rows = [
            {
                "rulingNumber": "HQ H555555",
                "rulingDate": "2020-06-01",
                "tariffs": ["8471.30.0100", "8473.30.1180"],
                "subject": "Two articles",
                "rulingText": "Body text.",
            }
        ]
        records = list(cross.parse_rows(rows))
        assert len(records) == 1
        assert records[0].classified_code == "8471300100"
        assert "8473301180" in records[0].body

    def test_a_ruling_with_no_usable_code_is_skipped(self) -> None:
        rows = [{"rulingNumber": "HQ H1", "rulingDate": "2020-06-01", "tariffs": "n/a"}]
        assert not list(cross.parse_rows(rows))


class TestIngestReport:
    def test_skip_reasons_are_counted(self) -> None:
        report = IngestReport(source="usitc_hts")
        report.skip("no code")
        report.skip("no code")
        report.skip("no description")
        assert report.skipped == 3
        assert report.as_dict()["skip_reasons"] == {"no code": 2, "no description": 1}

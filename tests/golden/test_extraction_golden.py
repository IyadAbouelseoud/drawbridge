"""Golden extraction fixtures — hand-verified answer keys.

Expected values live in `answer_keys.py`, derived by hand from the source lines. If a
refactor changes a computed figure, these fail.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from drawbridge_schemas.jurisdiction import KSA_PROFILE, US_PROFILE
from services.extraction.src.arabic import (
    detect_language,
    normalise,
    normalise_arabic,
    normalise_digits,
    to_logical_order,
)
from services.extraction.src.field_aliases import Field, resolve
from services.extraction.src.native import (
    TextSpan,
    assess,
    find_labelled_values,
    parse_amount,
)
from tests.golden.answer_keys import (
    BAYAN_CUSTOMS_VALUE,
    BAYAN_DECLARATION_DATE,
    BAYAN_DUTY,
    BAYAN_DUTY_PAYMENT_DATE,
    BAYAN_EXPECTED,
    BAYAN_LOGICAL,
    BAYAN_TOTAL,
    BAYAN_VAT,
    BAYAN_VISUAL,
    CBP_7501_EXPECTED,
    CBP_7501_LINES,
    CBP_ENTERED_VALUE,
    CBP_SECTION_301,
    CBP_TOTAL,
)

pytestmark = pytest.mark.golden


def _as_spans(lines: list[str]) -> list[TextSpan]:
    """Wrap raw document lines as spans, applying the same normalisation the native
    extractor applies. Geometry is synthetic; only the text matters here."""
    return [
        TextSpan(
            page=1,
            bbox=(60.0, 60.0 + index * 22, 535.0, 74.0 + index * 22),
            text=normalise(line),
            language=detect_language(normalise(line)),
        )
        for index, line in enumerate(lines)
    ]


class TestArabicNormalisation:
    """The failure modes that silently corrupt a figure rather than raising."""

    def test_arabic_indic_digits_become_ascii(self) -> None:
        assert normalise_digits("٤٦٨٧٥") == "46875"

    def test_eastern_arabic_indic_digits_become_ascii(self) -> None:
        assert normalise_digits("۱۲۳۴۵۶۷۸۹۰") == "1234567890"

    def test_arabic_decimal_separator_becomes_period(self) -> None:
        # U+066B is a distinct code point from '.' and would survive into Decimal().
        assert normalise_digits("٩٣٧٥٠٠٫٠٠") == "937500.00"

    def test_arabic_thousands_separator_becomes_comma(self) -> None:
        assert normalise_digits("١٬٢٠٠") == "1,200"

    def test_alef_variants_fold_together(self) -> None:
        assert normalise_arabic("إجمالي") == normalise_arabic("اجمالي")

    def test_tashkeel_stripped(self) -> None:
        assert normalise_arabic("الرُّسوم") == normalise_arabic("الرسوم")

    def test_ta_marbuta_folds_to_ha(self) -> None:
        assert normalise_arabic("القيمة المضافة") == normalise_arabic("القيمه المضافه")

    def test_bidi_controls_removed(self) -> None:
        # RLM between digits would split the number token in two.
        assert normalise("46‏875") == "46875"

    def test_soft_hyphen_folds_to_ascii_hyphen(self) -> None:
        # PDF font subsetting substitutes U+00AD for '-', silently breaking date parsing.
        assert normalise("2024­01­15") == "2024-01-15"

    def test_language_detection(self) -> None:
        assert detect_language("رقم البيان") == "ar"
        assert detect_language("Entry Number") == "en"
        # Digits are not Latin letters, so Arabic-plus-numerals is still Arabic.
        assert detect_language("البند الجمركي: 8471300000") == "ar"
        # Mixed needs actual Latin letters alongside Arabic.
        assert detect_language("المنفذ: Jeddah Islamic Port") == "mixed"
        # Digits alone are evidence of neither script; default to English.
        assert detect_language("12345") == "en"


class TestVisualToLogicalOrder:
    """Many real PDF producers store RTL runs in visual order.

    Left alone, the trailing colon migrates to the front and `partition(':')` yields an
    empty label, silently losing the field.
    """

    def test_reverses_an_rtl_run_and_restores_its_colon(self) -> None:
        assert to_logical_order(":نايبلا مقر") == "رقم البيان:"

    def test_leaves_latin_untouched(self) -> None:
        text = "Zakat, Tax and Customs Authority"
        assert to_logical_order(text) == text

    def test_leaves_arabic_indic_numerals_in_reading_order(self) -> None:
        """Arabic-Indic digits are bidi class AN — weak, rendered left to right.

        Reversing them turns 46875.00 into 00.57864, which parses cleanly and is wrong.
        """
        assert normalise("٤٦٨٧٥٫٠٠") == "46875.00"

    def test_mixed_rtl_and_latin_value(self) -> None:
        assert to_logical_order(":ذفنملا Jeddah Islamic Port") == "المنفذ: Jeddah Islamic Port"

    def test_visual_and_logical_documents_extract_identically(self) -> None:
        """The same Bayan from a conforming and a non-conforming generator.

        This is the regression barrier for `to_logical_order`: if it breaks, the two
        documents stop agreeing.
        """
        logical = find_labelled_values(_as_spans(BAYAN_LOGICAL))
        visual = find_labelled_values(_as_spans(BAYAN_VISUAL))

        assert {f: s.text for f, s in logical.items()} == {f: s.text for f, s in visual.items()}


class TestFieldAliases:
    """Bilingual label resolution — the same field under either script."""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("رقم البيان", Field.DECLARATION_NUMBER),
            ("Declaration No", Field.DECLARATION_NUMBER),
            ("Entry Number", Field.DECLARATION_NUMBER),
            ("البند الجمركي", Field.HS_CODE),
            ("HTSUS Number", Field.HS_CODE),
            ("الرسوم الجمركية", Field.DUTY_AMOUNT),
            ("Customs Duty", Field.DUTY_AMOUNT),
            ("ضريبة القيمة المضافة", Field.VAT_AMOUNT),
            ("تاريخ السداد", Field.DUTY_PAYMENT_DATE),
            ("رقم البيان الأصلي", Field.LINKED_IMPORT_DECLARATION),
        ],
    )
    def test_resolves_both_scripts(self, label: str, expected: Field) -> None:
        assert resolve(label) is expected

    def test_trailing_punctuation_ignored(self) -> None:
        assert resolve("Declaration No.:") is Field.DECLARATION_NUMBER

    def test_case_insensitive(self) -> None:
        assert resolve("CUSTOMS DUTY") is Field.DUTY_AMOUNT

    def test_unknown_label_returns_none(self) -> None:
        # Not an error — the signal that a new layout needs an alias, not a guess.
        assert resolve("Widget Throughput Coefficient") is None


class TestAmountParsing:
    def test_parses_arabic_indic_amount(self) -> None:
        assert parse_amount("٤٦٨٧٥٫٠٠") == Decimal("46875.00")

    def test_parses_arabic_thousands_grouped_amount(self) -> None:
        assert parse_amount("١٬١٣٢٬٠٣١٫٢٥") == Decimal("1132031.25")

    def test_parses_ascii_thousands_separated_amount(self) -> None:
        assert parse_amount("1,132,031.25") == Decimal("1132031.25")

    def test_returns_none_on_garbage(self) -> None:
        # A review signal, not an exception.
        assert parse_amount("N/A") is None
        assert parse_amount("") is None

    def test_never_returns_float(self) -> None:
        assert isinstance(parse_amount("62500.00"), Decimal)


class TestNativeRouting:
    """The ordering invariant: native first, OCR only as fallback."""

    def test_text_layer_document_is_usable(self, cbp_7501_pdf: Path) -> None:
        verdict = assess(cbp_7501_pdf)
        assert verdict.usable, verdict.reason
        assert verdict.confidence > 0.8

    def test_scanned_document_routes_to_ocr(self, scanned_pdf: Path) -> None:
        verdict = assess(scanned_pdf)
        assert not verdict.usable
        assert verdict.confidence == 0.0
        assert "scan" in verdict.reason


class TestCbp7501Golden:
    """Answer key for the CBP 7501."""

    def test_every_expected_field_extracts(self) -> None:
        found = find_labelled_values(_as_spans(CBP_7501_LINES))
        for field, expected in CBP_7501_EXPECTED.items():
            assert field in found, f"missing {field}"
            assert found[field].text == expected, field

    def test_specific_duty_label_beats_the_bare_one(self) -> None:
        """ "Duty: 0.00" and "Customs Duty: 62500.00" both resolve to DUTY_AMOUNT.

        First-wins files 0.00 — a claim for nothing on a $62,500 entry.
        """
        found = find_labelled_values(_as_spans(CBP_7501_LINES))
        assert parse_amount(found[Field.DUTY_AMOUNT].text) == CBP_SECTION_301
        assert parse_amount(found[Field.DUTY_AMOUNT].text) != Decimal("0.00")

    def test_total_reconciles_to_its_components(self) -> None:
        """Internal consistency of the answer key. If this fails, the key is wrong."""
        found = find_labelled_values(_as_spans(CBP_7501_LINES))
        assert parse_amount(found[Field.CUSTOMS_VALUE].text) == CBP_ENTERED_VALUE
        assert CBP_ENTERED_VALUE + CBP_SECTION_301 + Decimal("864.00") == CBP_TOTAL


class TestZatcaBayanGolden:
    """Answer key for the ZATCA Bayan.

    Amounts are written in Arabic-Indic numerals with U+066B as the decimal separator,
    so these fail unless the whole normalisation chain is intact.
    """

    def test_every_expected_field_extracts(self) -> None:
        found = find_labelled_values(_as_spans(BAYAN_LOGICAL))
        for field, expected in BAYAN_EXPECTED.items():
            assert field in found, f"missing {field}"
            assert found[field].text == expected, field

    def test_arabic_indic_amounts_normalise_to_expected_decimals(self) -> None:
        found = find_labelled_values(_as_spans(BAYAN_LOGICAL))
        assert parse_amount(found[Field.CUSTOMS_VALUE].text) == BAYAN_CUSTOMS_VALUE
        assert parse_amount(found[Field.DUTY_AMOUNT].text) == BAYAN_DUTY
        assert parse_amount(found[Field.VAT_AMOUNT].text) == BAYAN_VAT
        assert parse_amount(found[Field.TOTAL_AMOUNT].text) == BAYAN_TOTAL

    def test_quantity_in_arabic_indic_digits(self) -> None:
        found = find_labelled_values(_as_spans(BAYAN_LOGICAL))
        assert parse_amount(found[Field.QUANTITY].text) == Decimal("1200")

    def test_duty_payment_date_distinct_from_declaration_date(self) -> None:
        """The GCC re-export clock runs from payment, not from the declaration.

        These are 24 days apart because ZATCA permits payment to be postponed up to 30
        days — the case that makes the anchor matter.
        """
        found = find_labelled_values(_as_spans(BAYAN_LOGICAL))
        assert found[Field.DECLARATION_DATE].text == BAYAN_DECLARATION_DATE
        assert found[Field.DUTY_PAYMENT_DATE].text == BAYAN_DUTY_PAYMENT_DATE
        assert BAYAN_DECLARATION_DATE != BAYAN_DUTY_PAYMENT_DATE

    def test_duty_is_five_percent_of_customs_value(self) -> None:
        """Internal consistency of the answer key. If this fails, the key is wrong."""
        assert (BAYAN_CUSTOMS_VALUE * Decimal("0.05")).quantize(Decimal("0.01")) == BAYAN_DUTY

    def test_vat_is_fifteen_percent_of_value_plus_duty(self) -> None:
        taxable = BAYAN_CUSTOMS_VALUE + BAYAN_DUTY
        assert (taxable * Decimal("0.15")).quantize(Decimal("0.01")) == BAYAN_VAT

    def test_total_reconciles(self) -> None:
        assert BAYAN_CUSTOMS_VALUE + BAYAN_DUTY + BAYAN_VAT == BAYAN_TOTAL

    def test_recoverable_base_is_duty_only(self, ksa_entry_line) -> None:
        """VAT is on the document but is not drawback.

        KSA import VAT is recovered through the VAT return as input tax. A claim that
        includes it is a filing error — docs/COMPLIANCE-GCC.md §2.4.
        """
        assert ksa_entry_line.recoverable_base(KSA_PROFILE) == BAYAN_DUTY
        assert not KSA_PROFILE.includes_consumption_tax_in_base
        assert not KSA_PROFILE.includes_fees_in_base

    def test_gcc_refund_is_the_full_duty(self) -> None:
        """GCC refunds duty actually paid, with no 99% haircut."""
        assert (BAYAN_DUTY * KSA_PROFILE.refund_rate) == BAYAN_DUTY
        assert (BAYAN_DUTY * US_PROFILE.refund_rate) != BAYAN_DUTY

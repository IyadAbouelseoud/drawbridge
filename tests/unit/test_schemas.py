"""Contract tests for the shared schema package.

These guard the invariants the rest of the system assumes: substitution pivots on 8
digits and exists only in the US, designated quantity cannot exceed imported quantity,
refunds quantize to the cent at the jurisdiction's own rate, and claim state cannot skip
a stage.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from uuid import uuid4

import pytest
from pydantic import ValidationError

from drawbridge_schemas.claim import (
    Claim,
    ClaimState,
    DrawbackType,
    RecoveryLane,
    RefundLine,
)
from drawbridge_schemas.jurisdiction import (
    GCC_MIN_REEXPORT_VALUE_USD,
    KSA_PROFILE,
    US_PROFILE,
    ClockAnchor,
    Currency,
    DeadlineUnit,
    Jurisdiction,
    MatchTheory,
    profile_for,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode, LineMatch


class TestHTSCode:
    def test_substitution_pivots_on_eight_digits(self) -> None:
        a = HTSCode(code="8471300100")
        b = HTSCode(code="8471300150")
        assert a.substitution_key == "84713001"
        assert a.substitutable_with(b)

    def test_different_eighth_digit_blocks_substitution(self) -> None:
        assert not HTSCode(code="8471300100").substitutable_with(HTSCode(code="8471301100"))

    def test_hs6_is_the_international_portion(self) -> None:
        assert HTSCode(code="8471300100").hs6 == "847130"

    def test_accepts_gcc_eight_digit_code(self) -> None:
        # GCC declarations carry 8- or 12-digit HS codes, not the US 10.
        assert HTSCode(code="84713000").heading == "8471"

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(ValidationError):
            HTSCode(code="8471ABCD")


class TestJurisdictionProfiles:
    """The constants that were wrong in the brief, pinned to primary source."""

    def test_us_refunds_ninety_nine_percent(self) -> None:
        assert US_PROFILE.refund_rate == Decimal("0.99")

    def test_gcc_refunds_duty_actually_paid_in_full(self) -> None:
        # Rules of Impl. Art. 16 §6 prescribes no percentage haircut.
        assert KSA_PROFILE.refund_rate == Decimal("1.00")

    def test_gcc_has_no_substitution_theory(self) -> None:
        assert not KSA_PROFILE.permits(MatchTheory.HTS_SUBSTITUTION)
        assert KSA_PROFILE.permits(MatchTheory.DECLARATION_LINKAGE)

    def test_us_has_substitution_but_no_declaration_linkage(self) -> None:
        assert US_PROFILE.permits(MatchTheory.HTS_SUBSTITUTION)
        assert not US_PROFILE.permits(MatchTheory.DECLARATION_LINKAGE)

    def test_gcc_clock_anchors_on_duty_payment_not_import(self) -> None:
        # ZATCA permits payment postponement, so these genuinely differ.
        assert KSA_PROFILE.clock_anchor is ClockAnchor.DUTY_PAYMENT_DATE
        assert US_PROFILE.clock_anchor is ClockAnchor.IMPORT_DATE

    def test_gcc_filing_deadline_is_calendar_months_not_days(self) -> None:
        # "Six Gregorian months" is 181-184 days; a 180-day constant files late.
        deadline = KSA_PROFILE.claim_filing_deadline
        assert deadline.amount == 6
        assert deadline.unit is DeadlineUnit.GREGORIAN_MONTHS

    def test_gcc_minimum_claim_value_is_five_thousand_usd(self) -> None:
        assert KSA_PROFILE.min_claim_value == GCC_MIN_REEXPORT_VALUE_USD == Decimal("5000.00")
        assert KSA_PROFILE.min_claim_value_currency is Currency.USD

    def test_us_has_no_minimum_claim_value(self) -> None:
        assert US_PROFILE.min_claim_value is None

    def test_gcc_has_absolute_three_year_bar(self) -> None:
        # Common Customs Law Art. 174.
        assert KSA_PROFILE.absolute_bar is not None
        assert KSA_PROFILE.absolute_bar.amount == 3
        assert US_PROFILE.absolute_bar is None

    def test_consumption_tax_never_in_recoverable_base(self) -> None:
        assert not US_PROFILE.includes_consumption_tax_in_base
        assert not KSA_PROFILE.includes_consumption_tax_in_base

    def test_every_profile_carries_a_citation(self) -> None:
        for jurisdiction in Jurisdiction:
            profile = profile_for(jurisdiction)
            assert profile.refund_rate_citation
            assert profile.reexport_window.citation
            assert profile.claim_filing_deadline.citation


class TestEntryLine:
    def test_us_base_includes_fees(self, entry_line: EntryLine) -> None:
        # 62_500.00 section 301 + 864.00 MPF; the US profile includes fees.
        assert entry_line.recoverable_base(US_PROFILE) == Decimal("63364.00")

    def test_gcc_base_excludes_fees(self, entry_line: EntryLine) -> None:
        assert entry_line.recoverable_base(KSA_PROFILE) == Decimal("62500.00")

    def test_us_clock_starts_at_import_date(self, entry_line: EntryLine) -> None:
        assert entry_line.eligibility_clock_start == entry_line.import_date

    def test_gcc_clock_starts_at_duty_payment_date(self, ksa_entry_line: EntryLine) -> None:
        assert ksa_entry_line.duty_payment_date == date(2024, 2, 8)
        assert ksa_entry_line.eligibility_clock_start == date(2024, 2, 8)
        assert ksa_entry_line.eligibility_clock_start != ksa_entry_line.import_date

    def test_quantity_available_nets_prior_designations(self, entry_line: EntryLine) -> None:
        partly = entry_line.model_copy(update={"quantity_designated": Decimal("250")})
        assert partly.quantity_available == Decimal("750")

    def test_over_designation_rejected(self, entry_line: EntryLine) -> None:
        payload = entry_line.model_dump() | {"quantity_designated": Decimal("1001")}
        with pytest.raises(ValidationError, match="exceeds imported"):
            EntryLine.model_validate(payload)


class TestExportLine:
    def test_destruction_needs_no_destination(self, export_line: ExportLine) -> None:
        payload = export_line.model_dump()
        payload |= {"is_destruction": True, "destination_country": None}
        assert ExportLine.model_validate(payload).is_destruction

    def test_export_without_destination_rejected(self, export_line: ExportLine) -> None:
        payload = export_line.model_dump() | {"destination_country": None}
        with pytest.raises(ValidationError, match="destination or destruction"):
            ExportLine.model_validate(payload)

    def test_gcc_reexport_requires_declaration_link(self, export_line: ExportLine) -> None:
        """Art. 15(c) — without the link a GCC claim has no theory at all."""
        payload = export_line.model_dump() | {
            "jurisdiction": Jurisdiction.KSA,
            "linked_import_declaration": None,
        }
        with pytest.raises(ValidationError, match="linked_import_declaration"):
            ExportLine.model_validate(payload)


class TestLineMatch:
    def test_substitution_match_requires_key(self) -> None:
        with pytest.raises(ValidationError, match="substitution_key"):
            LineMatch(
                import_line_id=uuid4(),
                export_line_id=uuid4(),
                quantity=Decimal("1"),
                theory=MatchTheory.HTS_SUBSTITUTION,
                duty_allocated=Decimal("1.00"),
                refund_amount=Decimal("0.99"),
                days_clock_start_to_export=1,
            )

    def test_linkage_match_requires_declaration(self) -> None:
        with pytest.raises(ValidationError, match="linked_import_declaration"):
            LineMatch(
                import_line_id=uuid4(),
                export_line_id=uuid4(),
                quantity=Decimal("1"),
                theory=MatchTheory.DECLARATION_LINKAGE,
                duty_allocated=Decimal("1.00"),
                refund_amount=Decimal("1.00"),
                days_clock_start_to_export=1,
            )


class TestRefund:
    def _us_match(self) -> LineMatch:
        return LineMatch(
            import_line_id=uuid4(),
            export_line_id=uuid4(),
            quantity=Decimal("400"),
            theory=MatchTheory.HTS_SUBSTITUTION,
            substitution_key="84713001",
            duty_allocated=Decimal("25000.00"),
            refund_amount=Decimal("24750.00"),
            days_clock_start_to_export=301,
        )

    def _gcc_match(self) -> LineMatch:
        return LineMatch(
            import_line_id=uuid4(),
            export_line_id=uuid4(),
            quantity=Decimal("400"),
            theory=MatchTheory.DECLARATION_LINKAGE,
            linked_import_declaration="20240115447821",
            duty_allocated=Decimal("25000.00"),
            refund_amount=Decimal("25000.00"),
            days_clock_start_to_export=301,
        )

    def test_us_refund_is_ninety_nine_percent_of_duty_plus_fees(self) -> None:
        line = RefundLine(
            match=self._us_match(),
            duty_component=Decimal("25000.00"),
            mpf_component=Decimal("345.60"),
        )
        # (25000.00 + 345.60) x 0.99 = 25345.60 x 0.99 = 25092.144 -> 25092.14
        assert line.refund(US_PROFILE) == Decimal("25092.14")

    def test_gcc_refund_is_full_duty_and_ignores_fees(self) -> None:
        line = RefundLine(
            match=self._gcc_match(),
            duty_component=Decimal("25000.00"),
            mpf_component=Decimal("345.60"),
        )
        # GCC excludes fees from the base and applies no haircut.
        assert line.refund(KSA_PROFILE) == Decimal("25000.00")

    def test_refund_is_always_decimal(self) -> None:
        line = RefundLine(match=self._us_match(), duty_component=Decimal("1.00"))
        assert isinstance(line.refund(US_PROFILE), Decimal)


class TestClaimState:
    def test_happy_path_transitions(self) -> None:
        path = [
            ClaimState.INTAKE,
            ClaimState.EXTRACTING,
            ClaimState.EXTRACTED,
            ClaimState.CLASSIFYING,
            ClaimState.CLASSIFIED,
            ClaimState.MATCHING,
            ClaimState.MATCHED,
            ClaimState.QUANTIFIED,
            ClaimState.ANALYST_REVIEW,
            ClaimState.APPROVED,
            ClaimState.PACKAGED,
            ClaimState.HANDED_OFF,
            ClaimState.FILED,
            ClaimState.PAID,
        ]
        for current, nxt in pairwise(path):
            assert current.can_move_to(nxt), f"{current} -> {nxt}"

    def test_cannot_skip_analyst_review(self) -> None:
        assert not ClaimState.QUANTIFIED.can_move_to(ClaimState.APPROVED)
        assert not ClaimState.MATCHED.can_move_to(ClaimState.PACKAGED)

    def test_terminal_states_are_terminal(self) -> None:
        for terminal in (ClaimState.PAID, ClaimState.REJECTED, ClaimState.EXPIRED):
            assert not any(terminal.can_move_to(s) for s in ClaimState)


class TestClaim:
    def _claim(self, **overrides: object) -> Claim:
        now = datetime(2026, 9, 7, 12, 0, 0)
        base: dict[str, object] = {
            "claim_id": uuid4(),
            "tenant_id": uuid4(),
            "jurisdiction": Jurisdiction.US,
            "currency": Currency.USD,
            "lane": RecoveryLane.DRAWBACK,
            "drawback_type": DrawbackType.UNUSED_SUBSTITUTION,
            "period_start": date(2023, 1, 1),
            "period_end": date(2023, 12, 31),
            "filing_deadline": date(2027, 1, 9),
            "created_at": now,
            "updated_at": now,
        }
        return Claim.model_validate(base | overrides)

    def _gcc_claim(self, **overrides: object) -> Claim:
        payload: dict[str, object] = {
            "jurisdiction": Jurisdiction.KSA,
            "currency": Currency.SAR,
            "lane": RecoveryLane.GCC_REEXPORT_DRAWBACK,
            "drawback_type": DrawbackType.GCC_UNUSED_REEXPORT,
        }
        return self._claim(**(payload | overrides))

    def test_drawback_lane_requires_type(self) -> None:
        with pytest.raises(ValidationError, match="requires a drawback_type"):
            self._claim(drawback_type=None)

    def test_psc_lane_rejects_drawback_type(self) -> None:
        with pytest.raises(ValidationError, match="meaningless"):
            self._claim(lane=RecoveryLane.POST_SUMMARY_CORRECTION)

    def test_us_lane_unavailable_in_ksa(self) -> None:
        with pytest.raises(ValidationError, match="not available in jurisdiction"):
            self._claim(
                jurisdiction=Jurisdiction.KSA,
                currency=Currency.SAR,
                lane=RecoveryLane.DRAWBACK,
            )

    def test_substitution_rejected_outside_us(self) -> None:
        """The failure that would otherwise be silent and expensive."""
        with pytest.raises(ValidationError, match="US-only theory"):
            self._gcc_claim(drawback_type=DrawbackType.UNUSED_SUBSTITUTION)

    def test_currency_must_match_jurisdiction(self) -> None:
        with pytest.raises(ValidationError, match="files in"):
            self._gcc_claim(currency=Currency.USD)

    def test_gcc_claim_is_valid(self) -> None:
        claim = self._gcc_claim()
        assert claim.profile is KSA_PROFILE
        assert claim.total_refund == Decimal("0.00")

    def test_inverted_period_rejected(self) -> None:
        with pytest.raises(ValidationError, match="precedes"):
            self._claim(period_start=date(2024, 1, 1), period_end=date(2023, 1, 1))

    def test_empty_claim_totals_zero(self) -> None:
        assert self._claim().total_refund == Decimal("0.00")

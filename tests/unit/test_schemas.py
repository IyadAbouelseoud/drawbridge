"""Contract tests for the shared schema package.

These guard the invariants the rest of the system assumes: substitution pivots on 8
digits, designated quantity cannot exceed imported quantity, refunds are exactly 99% at
two decimal places, and claim state cannot skip a stage.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from uuid import uuid4

import pytest
from pydantic import ValidationError

from drawbridge_schemas.claim import (
    DRAWBACK_REFUND_RATE,
    Claim,
    ClaimState,
    DrawbackType,
    RecoveryLane,
    RefundLine,
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

    def test_rejects_non_ten_digit(self) -> None:
        with pytest.raises(ValidationError):
            HTSCode(code="84713001")


class TestEntryLine:
    def test_recoverable_base_includes_section_301_and_fees(self, entry_line: EntryLine) -> None:
        # 62_500.00 section 301 + 864.00 MPF + 0.00 duty + 0.00 HMF
        assert entry_line.total_recoverable_base == Decimal("63364.00")

    def test_quantity_available_nets_prior_designations(self, entry_line: EntryLine) -> None:
        partly = entry_line.model_copy(update={"quantity_designated": Decimal("250")})
        assert partly.quantity_available == Decimal("750")

    def test_over_designation_rejected(self, entry_line: EntryLine) -> None:
        with pytest.raises(ValidationError, match="exceeds imported"):
            entry_line.model_copy(update={"quantity_designated": Decimal("1001")}).model_validate(
                entry_line.model_dump() | {"quantity_designated": Decimal("1001")}
            )


class TestExportLine:
    def test_destruction_needs_no_destination(self, export_line: ExportLine) -> None:
        payload = export_line.model_dump()
        payload |= {"is_destruction": True, "destination_country": None}
        assert ExportLine.model_validate(payload).is_destruction

    def test_export_without_destination_rejected(self, export_line: ExportLine) -> None:
        payload = export_line.model_dump() | {"destination_country": None}
        with pytest.raises(ValidationError, match="destination or destruction"):
            ExportLine.model_validate(payload)


class TestRefund:
    def test_refund_is_ninety_nine_percent_to_the_cent(self) -> None:
        match = LineMatch(
            import_line_id=uuid4(),
            export_line_id=uuid4(),
            quantity=Decimal("400"),
            is_direct_identity=False,
            substitution_key="84713001",
            duty_allocated=Decimal("25000.00"),
            refund_amount=Decimal("24750.00"),
            days_import_to_export=301,
        )
        line = RefundLine(
            match=match,
            duty_component=Decimal("25000.00"),
            mpf_component=Decimal("345.60"),
        )
        expected = (Decimal("25345.60") * DRAWBACK_REFUND_RATE).quantize(Decimal("0.01"))
        assert line.refund == expected == Decimal("25092.14")

    def test_substitution_match_requires_key(self) -> None:
        with pytest.raises(ValidationError, match="substitution_key"):
            LineMatch(
                import_line_id=uuid4(),
                export_line_id=uuid4(),
                quantity=Decimal("1"),
                is_direct_identity=False,
                duty_allocated=Decimal("1.00"),
                refund_amount=Decimal("0.99"),
                days_import_to_export=1,
            )


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
        base = {
            "claim_id": uuid4(),
            "tenant_id": uuid4(),
            "lane": RecoveryLane.DRAWBACK,
            "drawback_type": DrawbackType.UNUSED_SUBSTITUTION,
            "period_start": date(2023, 1, 1),
            "period_end": date(2023, 12, 31),
            "filing_deadline": date(2027, 1, 9),
            "created_at": now,
            "updated_at": now,
        }
        return Claim.model_validate(base | overrides)

    def test_drawback_lane_requires_type(self) -> None:
        with pytest.raises(ValidationError, match="requires a drawback_type"):
            self._claim(drawback_type=None)

    def test_psc_lane_rejects_drawback_type(self) -> None:
        with pytest.raises(ValidationError, match="meaningless"):
            self._claim(lane=RecoveryLane.POST_SUMMARY_CORRECTION)

    def test_inverted_period_rejected(self) -> None:
        with pytest.raises(ValidationError, match="precedes"):
            self._claim(period_start=date(2024, 1, 1), period_end=date(2023, 1, 1))

    def test_empty_claim_totals_zero(self) -> None:
        assert self._claim().total_refund == Decimal("0.00")

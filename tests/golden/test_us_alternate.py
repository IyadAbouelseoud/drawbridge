"""Known-answer fixtures for the two US non-drawback lanes.

Both recover duty that was never owed, and both are governed by a short deadline that a
naive check gets wrong: a PSC is bounded by liquidation as well as by the 300-day offset,
and a §1520(d) claim has one year with no extension. Those are the assertions that matter
most here — a refund figure that is right on a claim filed a day late is worth nothing.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from pypdf import PdfReader

from drawbridge_schemas.claim import RecoveryLane
from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from services.packager.src.cbp_forms import field_values
from services.packager.src.packet import Claimant, PacketRequest
from services.packager.src.router import build_packet
from services.packager.src.us_alternate import (
    FTA_CLAIM_DAYS,
    PSC_MAX_DAYS_FROM_ENTRY,
    FtaAgreement,
    FtaClaimLine,
    PscCorrection,
    PscEntry,
    PscReasonCode,
    build_fta_packet,
    build_fta_payload,
    build_psc_packet,
    build_psc_payload,
)

pytestmark = pytest.mark.golden

CLAIMANT = Claimant(
    name="Meridian Components Inc",
    identifier="47-2938471-00",
    address_line1="4400 Harbour Point Blvd",
    city="Baltimore MD",
    country="US",
    postal_code="21226",
)

PREPARED = date(2026, 9, 8)


def request_for(claim_id: str = "clm-psc-0001") -> PacketRequest:
    return PacketRequest(
        claim_id=claim_id,
        tenant_id="tnt-0001",
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        claimant=CLAIMANT,
        lines=(),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 12, 31),
        filing_deadline=date(2027, 1, 1),
        prepared_on=PREPARED,
    )


def correction(
    *,
    line: int = 1,
    original: str = "62500.00",
    corrected: str = "0.00",
    reason: PscReasonCode = PscReasonCode.EXCLUSION,
) -> PscCorrection:
    return PscCorrection(
        line_number=line,
        field="duty_rate",
        original_value="25% Section 301",
        corrected_value="Free - exclusion 9903.88.67",
        reason=reason,
        duty_original=Decimal(original),
        duty_corrected=Decimal(corrected),
        narrative="Exclusion 9903.88.67 covered this article at the time of entry.",
    )


def entry(
    *,
    entry_date: date = date(2026, 3, 2),
    liquidation: date | None = None,
    corrections: tuple[PscCorrection, ...] = (),
) -> PscEntry:
    return PscEntry(
        entry_number="A12-3456789-0",
        port_code="1303",
        filer_code="A12",
        entry_date=entry_date,
        entry_summary_date=entry_date,
        importer_of_record="47-2938471-00",
        corrections=corrections or (correction(),),
        liquidation_date=liquidation,
    )


def fta_line(
    *,
    import_date: date = date(2026, 2, 10),
    duty_paid: str = "18400.00",
    preferential: str = "0.00",
    certified: bool = True,
    line: int = 1,
) -> FtaClaimLine:
    return FtaClaimLine(
        entry_number="A12-9988776-5",
        line_number=line,
        entry_date=import_date,
        import_date=import_date,
        hts_code="8544429000",
        description="Insulated electric conductors fitted with connectors",
        quantity=Decimal("4000.0000"),
        unit_of_measure="NO",
        country_of_origin="MX",
        entered_value=Decimal("230000.00"),
        duty_paid=Decimal(duty_paid),
        preferential_rate_duty=Decimal(preferential),
        origin_criterion="B",
        certification_on_file=certified,
    )


# ------------------------------------------------------------------------------- PSC


class TestPscDeadline:
    def test_unliquidated_entries_are_bound_by_the_300_day_offset(self) -> None:
        row = entry(entry_date=date(2026, 3, 2))
        assert row.deadline == date(2026, 3, 2) + timedelta(days=PSC_MAX_DAYS_FROM_ENTRY)

    def test_liquidation_binds_earlier_and_wins(self) -> None:
        """The bound an offset-only check misses. 15 days before liquidation, not 300 from entry."""
        row = entry(entry_date=date(2026, 3, 2), liquidation=date(2026, 10, 1))
        assert row.deadline == date(2026, 9, 16)
        assert row.deadline < date(2026, 3, 2) + timedelta(days=300)

    def test_a_late_liquidation_does_not_extend_the_offset(self) -> None:
        row = entry(entry_date=date(2026, 3, 2), liquidation=date(2027, 6, 1))
        assert row.deadline == date(2026, 12, 27)

    def test_timeliness_is_evaluated_against_the_binding_bound(self) -> None:
        row = entry(entry_date=date(2026, 3, 2), liquidation=date(2026, 9, 15))
        assert row.is_timely(PREPARED) is False


class TestPscPayload:
    def test_it_names_the_bound_that_applies(self) -> None:
        payload = build_psc_payload(
            request_for(), entry(entry_date=date(2026, 3, 2), liquidation=date(2026, 10, 1))
        )
        assert payload["timeliness"]["boundBy"] == "liquidation_minus_15_days"
        assert payload["timeliness"]["deadline"] == "2026-09-16"

    def test_offset_bound_is_named_when_it_binds(self) -> None:
        payload = build_psc_payload(request_for(), entry())
        assert payload["timeliness"]["boundBy"] == "entry_plus_300_days"

    def test_both_values_travel_with_every_correction(self) -> None:
        """A PSC stating only the new figure gives CBP nothing to reconcile against."""
        payload = build_psc_payload(request_for(), entry())
        item = payload["corrections"][0]
        assert item["originalValue"] == "25% Section 301"
        assert item["correctedValue"] == "Free - exclusion 9903.88.67"
        assert item["dutyOriginal"] == "62500.00"
        assert item["dutyCorrected"] == "0.00"
        assert item["refund"] == "62500.00"

    def test_money_is_strings(self) -> None:
        payload = json.loads(json.dumps(build_psc_payload(request_for(), entry())))
        assert isinstance(payload["totals"]["refundClaimed"], str)
        assert payload["totals"]["refundClaimed"] == "62500.00"

    def test_totals_sum_the_corrections(self) -> None:
        payload = build_psc_payload(
            request_for(),
            entry(
                corrections=(
                    correction(line=1, original="62500.00"),
                    correction(line=2, original="1200.50", corrected="200.50"),
                )
            ),
        )
        assert payload["totals"]["refundClaimed"] == "63500.00"
        assert payload["totals"]["correctionCount"] == 2

    def test_the_signature_is_left_unsigned(self) -> None:
        payload = build_psc_payload(request_for(), entry())
        assert payload["certification"]["signedBy"] is None


class TestPscPacket:
    def test_it_produces_a_payload_and_a_summary(self) -> None:
        packet = build_psc_packet(request_for(), entry())
        assert [a.filename for a in packet.artifacts] == [
            "psc-clm-psc-0001.json",
            "psc-summary-clm-psc-0001.pdf",
        ]

    def test_the_summary_says_it_is_not_the_filing(self) -> None:
        packet = build_psc_packet(request_for(), entry())
        content = packet.artifact("psc-summary-clm-psc-0001.pdf").content
        text = PdfReader(BytesIO(content)).pages[0].extract_text()
        assert "not the filing" in text

    def test_summary_fields_carry_the_figures(self) -> None:
        packet = build_psc_packet(request_for(), entry())
        values = field_values(packet.artifact("psc-summary-clm-psc-0001.pdf").content)
        assert values["psc_entry_number"] == "A12-3456789-0"
        assert values["psc_refund_claimed"] == "62,500.00"
        assert values["psc_deadline"] == "2026-12-27"

    def test_a_correction_that_increases_duty_is_refused(self) -> None:
        """A valid PSC, but not a recovery claim. Presenting it as one would misstate it."""
        with pytest.raises(ValueError, match="not a recovery claim"):
            build_psc_packet(
                request_for(),
                entry(corrections=(correction(original="100.00", corrected="500.00"),)),
            )

    def test_an_expired_window_warns_that_it_cannot_be_transmitted(self) -> None:
        packet = build_psc_packet(
            request_for(), entry(entry_date=date(2025, 1, 2), liquidation=date(2025, 8, 1))
        )
        assert any("correction window" in w for w in packet.warnings)

    def test_an_unliquidated_entry_warns_to_confirm_the_date(self) -> None:
        packet = build_psc_packet(request_for(), entry())
        assert any("unliquidated" in w for w in packet.warnings)

    def test_a_ksa_claim_is_refused(self) -> None:
        ksa = PacketRequest(
            claim_id="clm-x",
            tenant_id="t",
            jurisdiction=Jurisdiction.KSA,
            currency=Currency.SAR,
            claimant=CLAIMANT,
            lines=(),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            filing_deadline=date(2027, 1, 1),
            prepared_on=PREPARED,
        )
        with pytest.raises(ValueError, match="is a US filing"):
            build_psc_packet(ksa, entry())


# -------------------------------------------------------------------------- §1520(d)


class TestFtaDeadline:
    def test_one_year_from_importation(self) -> None:
        line = fta_line(import_date=date(2026, 2, 10))
        assert (line.deadline - date(2026, 2, 10)).days == FTA_CLAIM_DAYS

    def test_a_late_line_is_flagged_in_the_payload(self) -> None:
        payload = build_fta_payload(
            request_for("clm-fta-1"),
            [fta_line(import_date=date(2025, 1, 5))],
            FtaAgreement.USMCA,
        )
        assert payload["lines"][0]["timely"] is False

    def test_a_late_line_warns_that_the_statute_admits_no_extension(self) -> None:
        packet = build_fta_packet(
            request_for("clm-fta-1"),
            [fta_line(import_date=date(2025, 1, 5))],
            FtaAgreement.USMCA,
        )
        assert any("no extension" in w for w in packet.warnings)


class TestFtaPayload:
    def test_the_refund_is_duty_paid_less_the_preferential_rate(self) -> None:
        payload = build_fta_payload(
            request_for("clm-fta-1"),
            [fta_line(duty_paid="18400.00", preferential="2300.00")],
            FtaAgreement.USMCA,
        )
        assert payload["lines"][0]["refund"] == "16100.00"
        assert payload["totals"]["refundClaimed"] == "16100.00"

    def test_a_phased_preferential_rate_is_not_assumed_to_be_zero(self) -> None:
        """Several agreements phase rates down rather than to nothing."""
        payload = build_fta_payload(
            request_for("clm-fta-1"),
            [fta_line(duty_paid="1000.00", preferential="250.00")],
            FtaAgreement.KORUS,
        )
        assert payload["lines"][0]["dutyAtPreferentialRate"] == "250.00"

    def test_the_agreement_is_named(self) -> None:
        payload = build_fta_payload(request_for("clm-fta-1"), [fta_line()], FtaAgreement.CAFTA_DR)
        assert payload["agreement"] == "CAFTA-DR"

    def test_a_missing_certification_sets_analyst_review(self) -> None:
        payload = build_fta_payload(
            request_for("clm-fta-1"), [fta_line(certified=False)], FtaAgreement.USMCA
        )
        assert payload["requiresAnalystReview"] is True

    def test_a_complete_claim_needs_no_review(self) -> None:
        payload = build_fta_payload(request_for("clm-fta-1"), [fta_line()], FtaAgreement.USMCA)
        assert payload["requiresAnalystReview"] is False


class TestFtaPacket:
    def test_it_produces_a_written_claim_and_a_payload(self) -> None:
        packet = build_fta_packet(request_for("clm-fta-1"), [fta_line()], FtaAgreement.USMCA)
        assert sorted(a.filename for a in packet.artifacts) == [
            "cbp1520d-clm-fta-1.json",
            "cbp1520d-clm-fta-1.pdf",
        ]

    def test_the_claim_fields_carry_the_figures(self) -> None:
        packet = build_fta_packet(
            request_for("clm-fta-1"),
            [fta_line(duty_paid="18400.00", preferential="0.00")],
            FtaAgreement.USMCA,
        )
        values = field_values(packet.artifact("cbp1520d-clm-fta-1.pdf").content)
        assert values["fta_agreement"] == "USMCA"
        assert values["fta_refund_claimed"] == "18,400.00"
        assert values["fta_earliest_deadline"] == "2027-02-10"

    def test_a_missing_certification_is_stated_on_the_claim_itself(self) -> None:
        packet = build_fta_packet(
            request_for("clm-fta-1"), [fta_line(certified=False)], FtaAgreement.USMCA
        )
        content = packet.artifact("cbp1520d-clm-fta-1.pdf").content
        text = "\n".join(p.extract_text() for p in PdfReader(BytesIO(content)).pages)
        assert "ANALYST REVIEW" in text
        assert "cannot be reclaimed" in text

    def test_a_preferential_rate_above_the_rate_paid_is_refused(self) -> None:
        """It means the wrong rate was used, not that the preference costs more."""
        with pytest.raises(ValueError, match="wrong rate was used"):
            build_fta_packet(
                request_for("clm-fta-1"),
                [fta_line(duty_paid="100.00", preferential="500.00")],
                FtaAgreement.USMCA,
            )

    def test_an_empty_claim_warns(self) -> None:
        packet = build_fta_packet(request_for("clm-fta-1"), [], FtaAgreement.USMCA)
        assert any("seek nothing" in w for w in packet.warnings)


# --------------------------------------------------------------------------- routing


class TestLaneRouting:
    def test_an_alternate_lane_is_directed_rather_than_rendered_as_drawback(self) -> None:
        """Rendering a 7551 for a PSC would produce a coherent form for the wrong claim."""
        request = replace(request_for(), lane=RecoveryLane.POST_SUMMARY_CORRECTION)
        with pytest.raises(ValueError, match="us_alternate"):
            build_packet(request)

    def test_the_fta_lane_is_directed_too(self) -> None:
        request = replace(request_for(), lane=RecoveryLane.FTA_RETROACTIVE)
        with pytest.raises(ValueError, match="build_fta_packet"):
            build_packet(request)

    def test_drawback_is_the_default_and_still_routes(self) -> None:
        request = request_for()
        assert request.lane is RecoveryLane.DRAWBACK
        assert build_packet(request).artifacts

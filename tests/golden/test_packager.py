"""Known-answer fixtures for the output packager.

Two lanes, two entirely different assertions:

- **US** — the 7551/7552 must be a readable PDF whose AcroForm fields carry the exact
  figures, read back through `pypdf` rather than through the code that wrote them.
- **KSA** — the refund-request JSON must carry the Art. 15(c) linkage on every declaration,
  money as strings, and every ZATCA procedural citation as an `ANALYST_REVIEW` placeholder
  that blocks transmission.

The placeholder assertions are the ones that matter most. A packet that quietly filled in a
plausible Resolution 28624 article number would pass every other test in this file.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from pypdf import PdfReader

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from services.packager.src import citations as cite
from services.packager.src.branding import Preparer
from services.packager.src.cbp_forms import (
    build_us_packet,
    field_values,
    needs_7552,
    render_7551,
)
from services.packager.src.packet import Claimant, PacketLine, PacketRequest
from services.packager.src.router import build_packet
from services.packager.src.zatca_payload import build_ksa_packet, build_payload

pytestmark = pytest.mark.golden


# --------------------------------------------------------------------------- fixtures

US_CLAIMANT = Claimant(
    name="Meridian Components Inc",
    identifier="47-2938471-00",
    address_line1="4400 Harbour Point Blvd",
    city="Baltimore MD",
    country="US",
    postal_code="21226",
    contact_email="trade@meridian-components.example",
    broker_identifier="ABI-7741",
)

KSA_CLAIMANT = Claimant(
    name="Al Faisaliah Trading Company",
    identifier="1010384726",
    address_line1="King Fahd Road, Olaya District",
    city="Riyadh",
    country="SA",
    postal_code="12212",
    contact_email="customs@alfaisaliah.example",
)


def us_line(
    *,
    theory: str = "hts_substitution",
    duty_allocated: str = "62500.00",
    refund: str = "61875.00",
    quantity: str = "1000.0000",
    bom_path: str | None = None,
) -> PacketLine:
    return PacketLine(
        import_declaration="A12-3456789-0",
        import_line_number=1,
        import_date=date(2022, 4, 18),
        import_hts="8471300100",
        description="Portable automatic data processing machines, weight not over 10 kg",
        quantity_designated=Decimal(quantity),
        unit_of_measure="NO",
        duty_paid=Decimal("62500.00"),
        duty_allocated=Decimal(duty_allocated),
        refund_amount=Decimal(refund),
        export_reference="MAEU-284471938",
        export_line_number=1,
        export_date=date(2024, 9, 3),
        export_hts="8471300150",
        destination_country="CA",
        theory=theory,
        port_of_entry="1303",
        substitution_key="84713001",
        bom_path=bom_path,
    )


def ksa_line(*, partial: bool = False) -> PacketLine:
    return PacketLine(
        import_declaration="20240115447821",
        import_line_number=1,
        import_date=date(2024, 1, 15),
        duty_payment_date=date(2024, 1, 18),
        import_hts="847130",
        description="Portable automatic data processing machines",
        quantity_designated=Decimal("40.0000") if partial else Decimal("100.0000"),
        unit_of_measure="PCE",
        duty_paid=Decimal("18750.00"),
        duty_allocated=Decimal("7500.00") if partial else Decimal("18750.00"),
        refund_amount=Decimal("7500.00") if partial else Decimal("18750.00"),
        export_reference="20240712558193",
        export_line_number=1,
        export_date=date(2024, 7, 12),
        export_hts="847130",
        destination_country="AE",
        theory="declaration_linkage",
        port_of_entry="SAJED",
        consignment_id="CONS-2024-0451",
        is_partial_shipment=partial,
    )


def us_request(
    *lines: PacketLine,
    manufacturer: Claimant | None = None,
    preparer: Preparer | None = None,
) -> PacketRequest:
    return PacketRequest(
        claim_id="clm-us-0001",
        tenant_id="tnt-0001",
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        claimant=US_CLAIMANT,
        lines=lines or (us_line(),),
        period_start=date(2022, 1, 1),
        period_end=date(2024, 12, 31),
        filing_deadline=date(2027, 9, 3),
        prepared_on=date(2026, 9, 7),
        port_code="1303",
        manufacturer=manufacturer,
        **({"preparer": preparer} if preparer else {}),
    )


def ksa_request(*lines: PacketLine, iban: str = "SA0380000000608010167519") -> PacketRequest:
    return PacketRequest(
        claim_id="clm-ksa-0001",
        tenant_id="tnt-0001",
        jurisdiction=Jurisdiction.KSA,
        currency=Currency.SAR,
        claimant=KSA_CLAIMANT,
        lines=lines or (ksa_line(),),
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        filing_deadline=date(2025, 1, 12),
        prepared_on=date(2026, 9, 7),
        refund_account_iban=iban,
    )


# ------------------------------------------------------------------------------- US


class TestForm7551:
    def test_renders_a_readable_pdf(self) -> None:
        content = render_7551(us_request())
        assert content.startswith(b"%PDF-")
        reader = PdfReader(BytesIO(content))
        assert len(reader.pages) >= 1

    def test_field_values_carry_the_exact_figures(self) -> None:
        request = us_request(us_line(duty_allocated="62500.00", refund="61875.00"))
        values = field_values(render_7551(request))
        assert values["cbp7551_total_duty"] == "62,500.00"
        assert values["cbp7551_total_claimed"] == "61,875.00"
        assert values["cbp7551_claimant_id"] == "47-2938471-00"
        assert values["cbp7551_port_code"] == "1303"
        assert values["cbp7551_filing_deadline"] == "2027-09-03"

    def test_totals_sum_the_lines_rather_than_repeating_one(self) -> None:
        request = us_request(
            us_line(duty_allocated="62500.00", refund="61875.00"),
            us_line(duty_allocated="1000.50", refund="990.50"),
        )
        values = field_values(render_7551(request))
        assert values["cbp7551_total_duty"] == "63,500.50"
        assert values["cbp7551_total_claimed"] == "62,865.50"
        assert values["cbp7551_line_count"] == "2"

    def test_provision_is_derived_from_the_theories_claimed(self) -> None:
        values = field_values(render_7551(us_request(us_line(theory="direct_identity"))))
        assert "1313(j)(1)" in values["cbp7551_provision"]

    def test_mixed_theories_print_every_provision(self) -> None:
        request = us_request(us_line(theory="direct_identity"), us_line(theory="hts_substitution"))
        provision = field_values(render_7551(request))["cbp7551_provision"]
        assert "1313(j)(1)" in provision
        assert "1313(j)(2)" in provision

    def test_signature_fields_are_present_and_empty(self) -> None:
        """Drawbridge never signs a certification. The field exists; the value does not."""
        values = field_values(render_7551(us_request()))
        assert values["cbp7551_signatory_name"] == ""
        assert values["cbp7551_signature_date"] == ""

    def test_the_form_says_it_is_not_a_cbp_issued_document(self) -> None:
        text = PdfReader(BytesIO(render_7551(us_request()))).pages[0].extract_text()
        assert "not a CBP-issued form" in text
        assert "not a customs broker" in text

    def test_schedules_itemise_both_sides_of_the_designation(self) -> None:
        pages = PdfReader(BytesIO(render_7551(us_request()))).pages
        text = "\n".join(page.extract_text() for page in pages)
        assert "A12-3456789-0" in text
        assert "MAEU-284471938" in text


class TestForm7552:
    def test_omitted_for_a_plain_unused_merchandise_claim(self) -> None:
        request = us_request(us_line(theory="hts_substitution"))
        assert needs_7552(request) is False
        packet = build_us_packet(request)
        assert [a.filename for a in packet.artifacts] == ["cbp7551-clm-us-0001.pdf"]

    def test_emitted_for_a_manufacturing_claim(self) -> None:
        request = us_request(us_line(theory="manufacturing_substitution"))
        assert needs_7552(request) is True
        packet = build_us_packet(request)
        assert len(packet.artifacts) == 2
        values = field_values(packet.artifact("cbp7552-clm-us-0001.pdf").content)
        assert values["cbp7552_basis"] == "Certificate of manufacture and delivery"

    def test_emitted_when_a_separate_transferor_is_named(self) -> None:
        maker = Claimant(
            name="Tidewater Assemblies LLC",
            identifier="52-1188390-00",
            address_line1="18 Foundry Road",
            city="Norfolk VA",
            country="US",
        )
        packet = build_us_packet(us_request(us_line(), manufacturer=maker))
        values = field_values(packet.artifact("cbp7552-clm-us-0001.pdf").content)
        assert values["cbp7552_transferor_name"] == "Tidewater Assemblies LLC"
        assert values["cbp7552_transferee_name"] == "Meridian Components Inc"
        assert values["cbp7552_basis"] == "Delivery certificate"


class TestUsPacket:
    def test_us_citations_are_all_verified(self) -> None:
        """The US lane has no unobtainable authority. Nothing here may be open."""
        packet = build_us_packet(us_request())
        assert packet.citations
        assert packet.open_citations == []
        assert packet.requires_analyst_review is False

    def test_a_bom_route_pulls_in_the_relative_value_citation(self) -> None:
        request = us_request(
            us_line(theory="manufacturing_substitution", bom_path="8483409000>7325990000")
        )
        packet = build_us_packet(request)
        assert cite.US_190_SUBPART_B in packet.citations

    def test_the_bom_route_is_printed_so_the_multiplier_is_reproducible(self) -> None:
        request = us_request(
            us_line(theory="manufacturing_substitution", bom_path="8483409000>7325990000")
        )
        content = build_us_packet(request).artifact("cbp7551-clm-us-0001.pdf").content
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
        assert "8483409000>7325990000" in text

    def test_an_empty_claim_warns_rather_than_rendering_silently(self) -> None:
        request = PacketRequest(
            claim_id="clm-us-empty",
            tenant_id="tnt-0001",
            jurisdiction=Jurisdiction.US,
            currency=Currency.USD,
            claimant=US_CLAIMANT,
            lines=(),
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            filing_deadline=date(2027, 1, 1),
            prepared_on=date(2026, 9, 7),
        )
        packet = build_us_packet(request)
        assert any("claim nothing" in w for w in packet.warnings)

    def test_a_ksa_claim_is_refused_by_the_us_builder(self) -> None:
        with pytest.raises(ValueError, match="received a ksa claim"):
            build_us_packet(ksa_request())


# ------------------------------------------------------------------------------ KSA


class TestZatcaPayload:
    def test_every_declaration_carries_the_article_15c_link(self) -> None:
        payload = build_payload(ksa_request())
        for declaration in payload["declarations"]:
            link = declaration["linkedImportDeclaration"]
            assert link["number"] == "20240115447821"
            assert link["dutyPaymentDate"] == "2024-01-18"

    def test_money_is_strings_not_json_numbers(self) -> None:
        """A JSON number is a double. A claim has to reproduce to the halala."""
        raw = json.dumps(build_payload(ksa_request()))
        payload = json.loads(raw)
        assert payload["totals"]["refundClaimed"] == "18750.00"
        assert isinstance(payload["totals"]["refundClaimed"], str)
        assert isinstance(payload["declarations"][0]["amounts"]["dutyAttributable"], str)

    def test_refund_rate_is_the_gcc_hundred_percent_not_the_us_ninety_nine(self) -> None:
        totals = build_payload(ksa_request())["totals"]
        assert totals["refundRate"] == "1.00"
        assert "Art. 16 §6" in totals["refundRateCitation"]

    def test_the_settlement_account_is_carried(self) -> None:
        payload = build_payload(ksa_request())
        assert payload["settlement"]["iban"] == "SA0380000000608010167519"


class TestAnalystReviewPlaceholders:
    """The §8.4.1 mitigation. These are the assertions that stop a guessed citation."""

    def test_every_zatca_procedural_citation_is_open(self) -> None:
        payload = build_payload(ksa_request())
        procedural = [
            c for c in payload["citations"] if c["authority"] == cite.ZATCA_28624_AUTHORITY
        ]
        assert len(procedural) == 5
        assert all(c["status"] == "analyst_review" for c in procedural)
        assert all(c["article"] is None for c in procedural)

    def test_no_procedural_citation_asserts_an_article_number(self) -> None:
        raw = json.dumps(build_payload(ksa_request()), ensure_ascii=False)
        assert "28624" in raw  # the decision itself is named
        for guess in ("Article 1", "Article 2", "Article 3", "art. 1", "Art. 1 of 28624"):
            assert f"{guess} of Administrative Decision 28624" not in raw

    def test_the_reason_travels_with_the_placeholder(self) -> None:
        payload = build_payload(ksa_request())
        procedural = next(
            c for c in payload["citations"] if c["authority"] == cite.ZATCA_28624_AUTHORITY
        )
        assert "not obtainable" in procedural["reason"]
        assert "§8.4" in procedural["reason"]

    def test_the_payload_blocks_transmission(self) -> None:
        payload = build_payload(ksa_request())
        assert payload["requiresAnalystReview"] is True
        assert payload["analystReview"]["blocking"] is True
        assert payload["analystReview"]["openCitations"] == 5

    def test_the_packet_agrees_with_the_payload(self) -> None:
        packet = build_ksa_packet(ksa_request())
        assert packet.requires_analyst_review is True
        assert len(packet.open_citations) == 5
        assert any("blocked from transmission" in w for w in packet.warnings)

    def test_gcc_statutory_citations_stay_verified(self) -> None:
        """The GCC law is transcribed from a primary source. Nothing there is a guess."""
        packet = build_ksa_packet(ksa_request())
        statutory = [c for c in packet.citations if c.authority != cite.ZATCA_28624_AUTHORITY]
        assert statutory
        assert all(not c.is_open for c in statutory)
        assert cite.GCC_ART_97 in statutory
        assert cite.GCC_IMPL_ART_16_2 in statutory

    def test_an_open_citation_renders_as_a_visible_placeholder(self) -> None:
        rendered = cite.zatca_procedural("refund_request").render()
        assert "[ANALYST_REVIEW]" in rendered

    def test_an_unknown_procedural_step_raises_rather_than_inventing_one(self) -> None:
        with pytest.raises(KeyError, match="unknown ZATCA procedural citation"):
            cite.zatca_procedural("made_up_step")


class TestKsaPacket:
    def test_produces_one_json_artifact(self) -> None:
        packet = build_ksa_packet(ksa_request())
        assert [a.filename for a in packet.artifacts] == ["zatca-refund-clm-ksa-0001.json"]
        assert packet.artifacts[0].media_type == "application/json"

    def test_the_artifact_is_valid_utf8_json(self) -> None:
        content = build_ksa_packet(ksa_request()).artifacts[0].content
        payload = json.loads(content.decode("utf-8"))
        assert payload["schema"] == "drawbridge/zatca-refund-request/1"

    def test_a_partial_shipment_flags_the_undetermined_platform_behaviour(self) -> None:
        packet = build_ksa_packet(ksa_request(ksa_line(partial=True)))
        assert any("§8.2" in w for w in packet.warnings)
        payload = json.loads(packet.artifacts[0].content)
        consignment = payload["declarations"][0]["consignment"]
        assert consignment["isPartShipment"] is True
        assert consignment["platformBehaviourUndetermined"] is True

    def test_a_partial_shipment_pulls_in_article_16_4(self) -> None:
        packet = build_ksa_packet(ksa_request(ksa_line(partial=True)))
        assert cite.GCC_IMPL_ART_16_4 in packet.citations

    def test_a_full_shipment_does_not_claim_article_16_4(self) -> None:
        packet = build_ksa_packet(ksa_request(ksa_line(partial=False)))
        assert cite.GCC_IMPL_ART_16_4 not in packet.citations

    def test_a_missing_iban_warns(self) -> None:
        packet = build_ksa_packet(ksa_request(iban=""))
        assert any("IBAN" in w for w in packet.warnings)

    def test_a_us_claim_is_refused_by_the_ksa_builder(self) -> None:
        with pytest.raises(ValueError, match="received a us claim"):
            build_ksa_packet(us_request())

    def test_the_manifest_records_what_blocks_the_packet(self) -> None:
        manifest = build_ksa_packet(ksa_request()).manifest()
        assert manifest["requires_analyst_review"] is True
        assert manifest["open_citations"] == 5
        assert manifest["artifacts"][0]["bytes"] > 0


# --------------------------------------------------------------------------- routing


class TestRouting:
    def test_us_routes_to_pdfs(self) -> None:
        packet = build_packet(us_request())
        assert all(a.media_type == "application/pdf" for a in packet.artifacts)

    def test_ksa_routes_to_json(self) -> None:
        packet = build_packet(ksa_request())
        assert all(a.media_type == "application/json" for a in packet.artifacts)

    def test_a_missing_artifact_raises_with_what_is_there(self) -> None:
        packet = build_packet(us_request())
        with pytest.raises(KeyError, match="cbp7551"):
            packet.artifact("nonexistent.pdf")


# ------------------------------------------------------------------------ white label


BROKER = Preparer(
    name="Harborline Customs Brokers, Inc.",
    is_licensed_broker=True,
    filer_code="J7K",
    contact="trade@harborline.example",
)


class TestWhiteLabelling:
    """A broker running this on their own network prepares filings under their own
    licence. The preparer paragraph on a 7551 is a representation to CBP about who
    produced the document and what they may do with it, so it is composed from what is
    true of the deployer rather than having a name substituted into ours."""

    def test_the_unconfigured_default_reproduces_the_notice_byte_for_byte(self) -> None:
        """Every packet rendered since week 4 carries this paragraph. Branding must not
        change a document that nobody asked to be branded."""
        text = PdfReader(BytesIO(render_7551(us_request()))).pages[0].extract_text()
        assert "Prepared by Drawbridge for filing by a licensed customs broker" in text
        assert "Drawbridge is not a customs broker and does not transmit to CBP" in text

    def test_a_broker_deployment_does_not_print_our_disclaimer(self) -> None:
        """The failure this exists to prevent: a renamed preparer keeping "is not a
        customs broker" would be a false statement about the filer's own licence, on a
        form filed with a federal agency."""
        text = PdfReader(BytesIO(render_7551(us_request(preparer=BROKER)))).pages[0].extract_text()
        assert "Drawbridge" not in text
        assert "is not a customs broker" not in text
        assert "Harborline Customs Brokers, Inc. is a licensed customs broker" in text
        assert "J7K" in text

    def test_the_document_still_says_it_is_not_a_cbp_issued_form(self) -> None:
        """Not brandable. It is a fact about the document rather than a claim about the
        preparer, and a deployment able to remove it could present a transcription as the
        authority's own form."""
        for preparer in (None, BROKER):
            page = PdfReader(BytesIO(render_7551(us_request(preparer=preparer)))).pages[0]
            assert "not a CBP-issued form" in page.extract_text()

    def test_the_certification_is_not_brandable(self) -> None:
        """The declaration a person signs. Its wording is CBP's, not a deployment's."""
        pages = PdfReader(BytesIO(render_7551(us_request(preparer=BROKER)))).pages
        text = "\n".join(page.extract_text() for page in pages)
        # A phrase short enough not to straddle a line wrap: the renderer breaks the
        # certification across lines and pypdf reports the break as a newline.
        assert "I declare that the merchandise described was imported and duty paid" in text
        assert "has not been and will not be the subject of any" in text

    def test_the_figures_are_untouched_by_branding(self) -> None:
        """Reproduces to the cent regardless of whose name is on it."""
        line = us_line(duty_allocated="62500.00", refund="61875.00")
        plain = field_values(render_7551(us_request(line)))
        branded = field_values(render_7551(us_request(line, preparer=BROKER)))
        assert plain["cbp7551_total_claimed"] == branded["cbp7551_total_claimed"] == "61,875.00"

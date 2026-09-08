"""The two US recovery lanes that are not drawback.

Drawback recovers duty on goods that left the country. These two recover duty that was
never owed in the first place, and they are structurally different from drawback and from
each other:

**Post Summary Correction (19 CFR §141.11, ACE).** The entry summary was wrong — wrong
classification, wrong value, a missed exclusion. A PSC replaces the summary with a
corrected one. There is no paper form: a PSC is transmitted through ABI as a full
replacement entry summary, so the output here is the structured payload a filer's ABI
software sends, not a rendered document.

**Post-importation FTA claim (19 U.S.C. §1520(d)).** The goods qualified for preferential
treatment under an agreement, and the claim was not made at entry. This *is* a written
claim, filed with the port director, so it renders as a document as well as a payload.

Two deadlines that are easy to get wrong, and are checked here rather than assumed:

- A PSC must be filed within **300 days of the date of entry** and no later than **15 days
  before liquidation**. The second bound usually binds first, and it is the one a
  date-arithmetic-only check misses because liquidation is an event, not an offset.
- A §1520(d) claim must be filed within **one year of the date of importation**. Unlike
  drawback's §1313(r) three years, this one is short and unextendable.

Neither lane's figures originate here. The packager renders; the rules engine decides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from drawbridge_schemas.claim import RecoveryLane
from drawbridge_schemas.jurisdiction import Jurisdiction
from services.packager.src import citations as cite
from services.packager.src.packet import (
    FilingPacket,
    PacketArtifact,
    PacketRequest,
    money,
    quantity,
)
from services.packager.src.pdf import Document, Field, Section

if TYPE_CHECKING:
    from collections.abc import Sequence

JSON_MEDIA_TYPE = "application/json"
PDF_MEDIA_TYPE = "application/pdf"

# 19 CFR §141.11 / CBP's PSC policy.
PSC_MAX_DAYS_FROM_ENTRY = 300
PSC_MIN_DAYS_BEFORE_LIQUIDATION = 15

# 19 U.S.C. §1520(d). One year, and no provision extends it.
FTA_CLAIM_DAYS = 365

PSC_PAYLOAD_SCHEMA = "drawbridge/cbp-psc/1"
FTA_PAYLOAD_SCHEMA = "drawbridge/cbp-1520d/1"


class PscReasonCode(StrEnum):
    """Why the entry summary is being corrected.

    CBP requires a reason on every PSC. These are the ones a duty-recovery engine
    legitimately produces; a PSC for an operational reason is somebody else's filing.
    """

    CLASSIFICATION = "classification"
    """Wrong HTS number on the original summary."""

    VALUE = "value"
    """Wrong entered value — assists, royalties, price adjustments."""

    EXCLUSION = "exclusion"
    """A Section 301 or 232 exclusion that applied and was not claimed."""

    QUANTITY = "quantity"
    """Wrong quantity or unit of measure."""

    OTHER_DUTY = "other_duty"
    """Wrong ADD/CVD, MPF or HMF."""


class FtaAgreement(StrEnum):
    """Agreements a post-importation §1520(d) claim can be made under.

    Not every FTA permits one. §1520(d) names the agreements it covers, and a claim under
    an agreement outside that list is refused rather than filed — a claim CBP cannot grant
    still consumes the year.
    """

    USMCA = "usmca"
    CAFTA_DR = "cafta_dr"
    KORUS = "korus"
    US_CHILE = "us_chile"
    US_COLOMBIA = "us_colombia"
    US_PANAMA = "us_panama"
    US_PERU = "us_peru"
    US_SINGAPORE = "us_singapore"


@dataclass(frozen=True, slots=True)
class PscCorrection:
    """One field being corrected, with both values.

    Both values, always. A PSC that states only the new figure gives CBP nothing to
    reconcile against the original summary, and the delta is what the refund rests on.
    """

    line_number: int
    field: str
    original_value: str
    corrected_value: str
    reason: PscReasonCode
    duty_original: Decimal
    duty_corrected: Decimal
    narrative: str = ""

    @property
    def refund(self) -> Decimal:
        """Duty overpaid on this line. Never negative on a recovery claim.

        A correction that *increases* duty is a legitimate PSC and a legitimate thing for
        an importer to file — it is simply not this lane. `build_psc_packet` refuses one
        rather than presenting an amount owed as an amount recoverable.
        """
        return self.duty_original - self.duty_corrected


@dataclass(frozen=True, slots=True)
class PscEntry:
    """The entry summary being corrected."""

    entry_number: str
    port_code: str
    filer_code: str
    entry_date: date
    entry_summary_date: date
    importer_of_record: str
    corrections: Sequence[PscCorrection]
    liquidation_date: date | None = None
    """None where the entry is unliquidated. That is the ordinary case and is not a
    missing value — it is what makes the 15-day bound inapplicable rather than unknown."""

    @property
    def total_refund(self) -> Decimal:
        return sum((c.refund for c in self.corrections), Decimal("0.00"))

    @property
    def deadline(self) -> date:
        """The binding PSC deadline: 300 days from entry, or 15 days before liquidation.

        The earlier of the two. Liquidation usually binds first, and a check that looks
        only at the 300-day offset will call a claim timely that CBP will refuse.
        """
        by_offset = self.entry_date + timedelta(days=PSC_MAX_DAYS_FROM_ENTRY)
        if self.liquidation_date is None:
            return by_offset
        by_liquidation = self.liquidation_date - timedelta(days=PSC_MIN_DAYS_BEFORE_LIQUIDATION)
        return min(by_offset, by_liquidation)

    def is_timely(self, as_of: date) -> bool:
        return as_of <= self.deadline


@dataclass(frozen=True, slots=True)
class FtaClaimLine:
    """One line of a post-importation preference claim."""

    entry_number: str
    line_number: int
    entry_date: date
    import_date: date
    hts_code: str
    description: str
    quantity: Decimal
    unit_of_measure: str
    country_of_origin: str
    entered_value: Decimal
    duty_paid: Decimal
    preferential_rate_duty: Decimal
    """Duty that would have been assessed at the preferential rate. Often zero, but not
    assumed to be: several agreements phase rates down rather than to nothing."""

    origin_criterion: str
    """The agreement's own criterion letter — USMCA A/B/C/D. Carried as published rather
    than interpreted, because the criterion is what the certification asserts."""

    certification_on_file: bool = False

    @property
    def refund(self) -> Decimal:
        return self.duty_paid - self.preferential_rate_duty

    @property
    def deadline(self) -> date:
        return self.import_date + timedelta(days=FTA_CLAIM_DAYS)


# ------------------------------------------------------------------------------- PSC


def build_psc_payload(request: PacketRequest, entry: PscEntry) -> dict[str, Any]:
    """The ABI replacement entry summary for a PSC.

    Shaped as a replacement rather than a delta because that is what a PSC *is*: ACE
    accepts a complete corrected summary, not a patch. The corrections list travels
    alongside so the filer and CBP can both see what moved, but the authoritative content
    is the corrected summary itself.
    """
    return {
        "schema": PSC_PAYLOAD_SCHEMA,
        "filingType": "POST_SUMMARY_CORRECTION",
        "authority": "19 CFR 141.11",
        "claimReference": request.claim_id,
        "preparedOn": request.prepared_on.isoformat(),
        "entrySummary": {
            "entryNumber": entry.entry_number,
            "portCode": entry.port_code,
            "filerCode": entry.filer_code,
            "entryDate": entry.entry_date.isoformat(),
            "entrySummaryDate": entry.entry_summary_date.isoformat(),
            "importerOfRecord": entry.importer_of_record,
            "liquidationDate": (
                entry.liquidation_date.isoformat() if entry.liquidation_date else None
            ),
        },
        "timeliness": {
            "deadline": entry.deadline.isoformat(),
            "boundBy": (
                "liquidation_minus_15_days"
                if entry.liquidation_date
                and entry.liquidation_date - timedelta(days=PSC_MIN_DAYS_BEFORE_LIQUIDATION)
                < entry.entry_date + timedelta(days=PSC_MAX_DAYS_FROM_ENTRY)
                else "entry_plus_300_days"
            ),
            "timely": entry.is_timely(request.prepared_on),
            "daysRemaining": (entry.deadline - request.prepared_on).days,
        },
        "corrections": [
            {
                "lineNumber": correction.line_number,
                "field": correction.field,
                "reasonCode": correction.reason,
                "originalValue": correction.original_value,
                "correctedValue": correction.corrected_value,
                "dutyOriginal": str(correction.duty_original.quantize(Decimal("0.01"))),
                "dutyCorrected": str(correction.duty_corrected.quantize(Decimal("0.01"))),
                "refund": str(correction.refund.quantize(Decimal("0.01"))),
                "narrative": correction.narrative or None,
            }
            for correction in entry.corrections
        ],
        "totals": {
            "correctionCount": len(entry.corrections),
            # Strings, not JSON numbers: a JSON number is a double, and an entry summary
            # has to reconcile to the cent against CBP's own figures.
            "refundClaimed": str(entry.total_refund.quantize(Decimal("0.01"))),
            "currency": "USD",
        },
        "certification": {
            "text": PSC_CERTIFICATION,
            "signedBy": None,
            "signedOn": None,
        },
        "citations": [c.as_dict() for c in psc_citations()],
        "requiresAnalystReview": False,
    }


PSC_CERTIFICATION = (
    "The corrected entry summary is true and correct to the best of my knowledge, the "
    "merchandise is properly described and classified, and all statements in the "
    "documents filed are true. No protest has been filed and no other post summary "
    "correction is pending on this entry."
)


def psc_citations() -> list[cite.Citation]:
    return [
        cite.Citation(
            authority="19 CFR",
            article="§141.11",
            proposition="Post summary correction of an entry summary already filed",
        ),
        cite.Citation(
            authority="19 CFR",
            article="§159.1",
            proposition="Liquidation, which bounds the correction window",
        ),
    ]


def render_psc_summary(request: PacketRequest, entry: PscEntry) -> bytes:
    """A human-readable cover for the PSC transmission.

    Not the filing. The PSC goes over ABI as data; this exists so the analyst approving it,
    and the importer authorising it, can see what is being changed without reading JSON.
    """
    document = Document(
        "Post Summary Correction - transmission summary",
        f"Claim {request.claim_id} | prepared {request.prepared_on.isoformat()} | "
        "PSC is transmitted through ABI; this page is not the filing",
    )
    document.paragraph(
        "Prepared by Drawbridge for transmission by a licensed customs broker. The "
        "authoritative filing is the replacement entry summary in the accompanying JSON "
        "payload; this summary exists for review and authorisation."
    )

    document.section(
        Section(
            title="Entry being corrected",
            rows=[
                [
                    Field("psc_entry_number", "Entry number", entry.entry_number, 0.34),
                    Field("psc_port_code", "Port", entry.port_code, 0.16),
                    Field("psc_filer_code", "Filer", entry.filer_code, 0.16),
                    Field("psc_entry_date", "Entry date", entry.entry_date.isoformat(), 0.34),
                ],
                [
                    Field("psc_importer", "Importer of record", entry.importer_of_record, 0.58),
                    Field(
                        "psc_liquidation_date",
                        "Liquidation",
                        entry.liquidation_date.isoformat()
                        if entry.liquidation_date
                        else "unliquidated",
                        0.42,
                    ),
                ],
            ],
        )
    )

    remaining = (entry.deadline - request.prepared_on).days
    document.section(
        Section(
            title="Timeliness",
            rows=[
                [
                    Field("psc_deadline", "Filing deadline", entry.deadline.isoformat(), 0.34),
                    Field("psc_days_remaining", "Days remaining", str(remaining), 0.24),
                    Field(
                        "psc_refund_claimed",
                        "Refund claimed",
                        money(entry.total_refund),
                        0.42,
                    ),
                ]
            ],
            note=(
                "The deadline is the earlier of 300 days from entry and 15 days before "
                "liquidation. Liquidation usually binds first, and an offset-only check "
                "would call this claim timely when CBP will not."
            ),
        )
    )

    document.section(Section(title="Corrections", rows=[]))
    document.table(
        headers=["Ln", "Field", "Reason", "Original", "Corrected", "Refund"],
        widths=[0.05, 0.19, 0.15, 0.21, 0.21, 0.19],
        rows=[
            [
                str(c.line_number),
                c.field,
                c.reason,
                c.original_value,
                c.corrected_value,
                money(c.refund),
            ]
            for c in entry.corrections
        ],
    )

    document.section(
        Section(
            title="Certification",
            rows=[
                [
                    Field("psc_signatory_name", "Signature", "", 0.45),
                    Field("psc_signatory_title", "Title", "", 0.30),
                    Field("psc_signature_date", "Date", "", 0.25),
                ]
            ],
            note=PSC_CERTIFICATION,
        )
    )
    return document.render()


def build_psc_packet(request: PacketRequest, entry: PscEntry) -> FilingPacket:
    """Render a PSC: the ABI payload, plus a summary for authorisation."""
    if request.jurisdiction is not Jurisdiction.US:
        msg = f"a post summary correction is a US filing; got {request.jurisdiction}"
        raise ValueError(msg)

    increases = [c for c in entry.corrections if c.refund < 0]
    if increases:
        lines = sorted({c.line_number for c in increases})
        msg = (
            f"lines {lines} correct duty upward; a PSC that increases duty is a valid "
            "filing but is not a recovery claim, and presenting an amount owed as an "
            "amount recoverable would misstate the claim"
        )
        raise ValueError(msg)

    packet = FilingPacket(
        claim_id=request.claim_id,
        jurisdiction=request.jurisdiction,
        citations=psc_citations(),
    )

    if not entry.corrections:
        packet.warnings.append("no corrections; the PSC would replace the summary unchanged")
    if not entry.is_timely(request.prepared_on):
        packet.warnings.append(
            f"past the PSC deadline {entry.deadline.isoformat()}; the correction window "
            "closed and this cannot be transmitted"
        )
    elif (entry.deadline - request.prepared_on).days <= 30:
        packet.warnings.append(
            f"{(entry.deadline - request.prepared_on).days} days to the PSC deadline"
        )
    if entry.liquidation_date is None:
        packet.warnings.append(
            "entry is unliquidated; confirm the liquidation date before transmission, "
            "because it can bind earlier than the 300-day offset"
        )

    packet.artifacts.append(
        PacketArtifact(
            filename=f"psc-{request.claim_id}.json",
            media_type=JSON_MEDIA_TYPE,
            content=json.dumps(
                build_psc_payload(request, entry), indent=2, ensure_ascii=False
            ).encode("utf-8"),
        )
    )
    packet.artifacts.append(
        PacketArtifact(
            filename=f"psc-summary-{request.claim_id}.pdf",
            media_type=PDF_MEDIA_TYPE,
            content=render_psc_summary(request, entry),
        )
    )
    return packet


# -------------------------------------------------------------------------- §1520(d)


FTA_CERTIFICATION = (
    "I declare that the goods described qualified as originating goods under the "
    "agreement named at the time of importation, that a valid certification of origin is "
    "in my possession, and that no protest, petition or post summary correction "
    "concerning the tariff classification or preferential treatment of these goods has "
    "been filed. I will provide the certification and supporting records on request."
)


def fta_citations(agreement: FtaAgreement) -> list[cite.Citation]:
    return [
        cite.Citation(
            authority="19 U.S.C.",
            article="§1520(d)",
            proposition=(
                "Post-importation claim for preferential tariff treatment, filed within "
                "one year of the date of importation"
            ),
        ),
        cite.Citation(
            authority="19 CFR",
            article="§182.32",
            proposition="Filing requirements for a post-importation preference claim",
        ),
        cite.Citation(
            authority="Agreement",
            article=agreement.value.upper().replace("_", "-"),
            proposition="Rules of origin under which the goods qualify",
        ),
    ]


def build_fta_payload(
    request: PacketRequest, lines: Sequence[FtaClaimLine], agreement: FtaAgreement
) -> dict[str, Any]:
    """The structured §1520(d) claim."""
    total_refund = sum((line.refund for line in lines), Decimal("0.00"))
    return {
        "schema": FTA_PAYLOAD_SCHEMA,
        "filingType": "POST_IMPORTATION_PREFERENCE_CLAIM",
        "authority": "19 U.S.C. 1520(d)",
        "agreement": agreement.value.upper().replace("_", "-"),
        "claimReference": request.claim_id,
        "preparedOn": request.prepared_on.isoformat(),
        "claimant": {
            "name": request.claimant.name,
            "identifier": request.claimant.identifier,
            "address": request.claimant.address_block(),
        },
        "lines": [
            {
                "entryNumber": line.entry_number,
                "lineNumber": line.line_number,
                "entryDate": line.entry_date.isoformat(),
                "importDate": line.import_date.isoformat(),
                "htsCode": line.hts_code,
                "description": line.description,
                "quantity": str(line.quantity.quantize(Decimal("0.0001"))),
                "unitOfMeasure": line.unit_of_measure,
                "countryOfOrigin": line.country_of_origin,
                "enteredValue": str(line.entered_value.quantize(Decimal("0.01"))),
                "dutyPaid": str(line.duty_paid.quantize(Decimal("0.01"))),
                "dutyAtPreferentialRate": str(
                    line.preferential_rate_duty.quantize(Decimal("0.01"))
                ),
                "refund": str(line.refund.quantize(Decimal("0.01"))),
                "originCriterion": line.origin_criterion,
                "certificationOnFile": line.certification_on_file,
                "deadline": line.deadline.isoformat(),
                "timely": request.prepared_on <= line.deadline,
            }
            for line in lines
        ],
        "totals": {
            "lineCount": len(lines),
            "refundClaimed": str(total_refund.quantize(Decimal("0.01"))),
            "currency": "USD",
        },
        "certification": {"text": FTA_CERTIFICATION, "signedBy": None, "signedOn": None},
        "citations": [c.as_dict() for c in fta_citations(agreement)],
        "requiresAnalystReview": any(not line.certification_on_file for line in lines),
    }


def render_fta_claim(
    request: PacketRequest, lines: Sequence[FtaClaimLine], agreement: FtaAgreement
) -> bytes:
    """The written §1520(d) claim, as filed with the port director."""
    total_refund = sum((line.refund for line in lines), Decimal("0.00"))
    agreement_name = agreement.value.upper().replace("_", "-")

    document = Document(
        "Post-Importation Claim for Preferential Tariff Treatment",
        f"19 U.S.C. 1520(d) | {agreement_name} | claim {request.claim_id} | "
        f"prepared {request.prepared_on.isoformat()}",
    )
    document.paragraph(
        "Prepared by Drawbridge for filing by a licensed customs broker. Drawbridge is "
        "not a customs broker and does not transmit to CBP. Every figure below traces to "
        "a source-document span retained under 19 CFR 163."
    )

    document.section(
        Section(
            title="Claimant",
            rows=[
                [
                    Field("fta_claimant_name", "Name", request.claimant.name, 0.62),
                    Field(
                        "fta_claimant_id",
                        "Importer number",
                        request.claimant.identifier,
                        0.38,
                    ),
                ],
                [
                    Field(
                        "fta_claimant_address",
                        "Address",
                        request.claimant.address_block(),
                        0.80,
                    ),
                    Field("fta_claimant_country", "Country", request.claimant.country, 0.20),
                ],
            ],
        )
    )

    earliest = min((line.deadline for line in lines), default=request.prepared_on)
    document.section(
        Section(
            title="Claim",
            rows=[
                [
                    Field("fta_agreement", "Agreement", agreement_name, 0.28),
                    Field("fta_line_count", "Entry lines", str(len(lines)), 0.18),
                    Field(
                        "fta_earliest_deadline",
                        "Earliest deadline",
                        earliest.isoformat(),
                        0.26,
                    ),
                    Field("fta_refund_claimed", "Refund claimed", money(total_refund), 0.28),
                ]
            ],
            note=(
                "A section 1520(d) claim must be filed within one year of the date of "
                "importation. No provision extends it, and the earliest line deadline "
                "governs the packet."
            ),
        )
    )

    document.section(
        Section(
            title="Declaration",
            rows=[
                [
                    Field("fta_signatory_name", "Signature", "", 0.45),
                    Field("fta_signatory_title", "Title", "", 0.30),
                    Field("fta_signature_date", "Date", "", 0.25),
                ]
            ],
            note=FTA_CERTIFICATION,
        )
    )

    document.section(Section(title="Entry lines claimed", rows=[]))
    document.table(
        headers=["Entry no.", "Ln", "Import date", "HTS", "Origin", "Qty", "Duty paid", "Refund"],
        widths=[0.18, 0.04, 0.12, 0.12, 0.08, 0.14, 0.16, 0.16],
        rows=[
            [
                line.entry_number,
                str(line.line_number),
                line.import_date.isoformat(),
                line.hts_code,
                line.country_of_origin,
                f"{quantity(line.quantity)} {line.unit_of_measure}",
                money(line.duty_paid),
                money(line.refund),
            ]
            for line in lines
        ],
    )

    missing = [line for line in lines if not line.certification_on_file]
    if missing:
        document.paragraph(
            "ANALYST REVIEW: "
            + ", ".join(f"{line.entry_number} line {line.line_number}" for line in missing)
            + " have no certification of origin on file. Section 1520(d) requires the "
            "certification to be in the importer's possession at the time the claim is "
            "made; filing without it is a claim CBP will deny, and the year cannot be "
            "reclaimed."
        )
    return document.render()


def build_fta_packet(
    request: PacketRequest, lines: Sequence[FtaClaimLine], agreement: FtaAgreement
) -> FilingPacket:
    """Render a §1520(d) post-importation preference claim."""
    if request.jurisdiction is not Jurisdiction.US:
        msg = f"a section 1520(d) claim is a US filing; got {request.jurisdiction}"
        raise ValueError(msg)

    packet = FilingPacket(
        claim_id=request.claim_id,
        jurisdiction=request.jurisdiction,
        citations=fta_citations(agreement),
    )

    if not lines:
        packet.warnings.append("no entry lines; the claim would seek nothing")

    late = [line for line in lines if request.prepared_on > line.deadline]
    if late:
        packet.warnings.append(
            f"{len(late)} lines are past the one-year §1520(d) deadline and must be "
            "removed before filing; the statute admits no extension"
        )

    missing = [line for line in lines if not line.certification_on_file]
    if missing:
        packet.warnings.append(
            f"{len(missing)} lines have no certification of origin on file; §1520(d) "
            "requires it in the importer's possession when the claim is made"
        )

    negative = [line for line in lines if line.refund < 0]
    if negative:
        msg = (
            "preferential duty exceeds duty paid on "
            f"{sorted({line.line_number for line in negative})}; the preference would "
            "cost more than the rate already assessed, which means the wrong rate was used"
        )
        raise ValueError(msg)

    packet.artifacts.append(
        PacketArtifact(
            filename=f"cbp1520d-{request.claim_id}.pdf",
            media_type=PDF_MEDIA_TYPE,
            content=render_fta_claim(request, lines, agreement),
        )
    )
    packet.artifacts.append(
        PacketArtifact(
            filename=f"cbp1520d-{request.claim_id}.json",
            media_type=JSON_MEDIA_TYPE,
            content=json.dumps(
                build_fta_payload(request, lines, agreement), indent=2, ensure_ascii=False
            ).encode("utf-8"),
        )
    )
    return packet


LANES = {
    RecoveryLane.POST_SUMMARY_CORRECTION: "build_psc_packet",
    RecoveryLane.FTA_RETROACTIVE: "build_fta_packet",
}
"""The two lanes this module covers, for the router's benefit.

Named rather than bound to the functions because both take lane-specific arguments the
generic `PacketRequest` does not carry — a PSC needs the entry being corrected, a
§1520(d) claim needs the agreement. The router dispatches to `build_us_packet` for
drawback and raises a directing error for these two rather than silently rendering the
wrong lane.
"""

"""ZATCA e-Services refund request payload.

The GCC output is JSON, not a PDF, because that is what the portal takes: a refund request
against a re-export *Bayan* linked to its import *Bayan*. Arabic survives a JSON payload
intact, which is the other reason the KSA lane never goes through the PDF path — the base-14
fonts there cannot encode it, and a form full of replacement characters is a form that
describes different goods.

Every claim-level figure carries the provision that authorises it, inline, as a `citations`
block. Two kinds appear:

- GCC Common Customs Law and Rules of Implementation citations — verified, emitted with
  their article numbers.
- ZATCA Resolution 28624 procedural citations — **`ANALYST_REVIEW` placeholders**, because
  the article numbers are not obtainable from any published source (`COMPLIANCE-GCC.md`
  §8.4). A payload containing one sets `requiresAnalystReview` and must not be transmitted.

The placeholder is deliberate and load-bearing. A missing article number invites a request
for information; a guessed one is a misstatement to the authority. The system's rule is
that no figure and no citation is ever originated by the software, and this is where that
rule meets the output.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from drawbridge_schemas.jurisdiction import Jurisdiction
from services.packager.src import citations as cite
from services.packager.src.packet import (
    FilingPacket,
    PacketArtifact,
    PacketRequest,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

JSON_MEDIA_TYPE = "application/json"

PAYLOAD_SCHEMA = "drawbridge/zatca-refund-request/1"

# Every procedural step of a KSA filing that would carry a 28624 article number. All open.
PROCEDURAL_STEPS = (
    "refund_request",
    "reexport_bayan_link",
    "goods_identification",
    "supporting_documents",
    "refund_settlement",
)

# Supporting documents ZATCA expects with a refund request. Named from the Art. 16 controls
# they evidence rather than from a 28624 list, because the 28624 list is exactly what is not
# obtainable — the requirement is real, its enumeration is the open part.
REQUIRED_DOCUMENTS = (
    ("import_bayan", "Original import declaration (Bayan) with duty payment receipt"),
    ("reexport_bayan", "Re-export declaration (Bayan) carrying the import declaration number"),
    ("commercial_invoice", "Commercial invoice for the re-exported goods"),
    ("packing_list", "Packing list identifying the consignment"),
    ("bill_of_lading", "Transport document evidencing departure"),
    ("proof_of_purchase", "Proof of purchase, where the claimant is not the importer of record"),
)


def _decimal(value: Decimal, places: str = "0.01") -> str:
    """Money and quantities as strings, never as JSON numbers.

    A JSON number is a double by specification, and 19531.25 survives that while
    19531.255 does not. The whole claim has to reproduce to the halala, so the wire format
    is a string and the receiving system parses it as a decimal or it parses it wrongly.
    """
    return str(value.quantize(Decimal(places)))


def _citation_block(citations: Sequence[cite.Citation]) -> list[dict[str, Any]]:
    return [citation.as_dict() for citation in citations]


def claim_citations(request: PacketRequest) -> list[cite.Citation]:
    """Every authority the KSA packet rests on, verified and open together.

    Ordered statute first, then procedure. The open ones are not segregated to the end:
    they appear where they apply, so a reader working through the payload meets each
    placeholder in the context that needs it.
    """
    found: list[cite.Citation] = [
        cite.GCC_ART_97,
        cite.GCC_IMPL_ART_15C,
        cite.GCC_IMPL_ART_16_2,
        cite.GCC_ART_28,
        cite.GCC_IMPL_ART_16_3A,
        cite.GCC_IMPL_ART_16_3B,
        cite.GCC_IMPL_ART_16_5,
        cite.GCC_IMPL_ART_16_6,
        cite.GCC_ART_174,
    ]
    if any(line.is_partial_shipment for line in request.lines):
        found.insert(6, cite.GCC_IMPL_ART_16_4)
    found.extend(cite.zatca_procedural(step) for step in PROCEDURAL_STEPS)
    return found


def build_payload(request: PacketRequest) -> dict[str, Any]:
    """The refund-request body for ZATCA e-Services."""
    citations = claim_citations(request)
    open_ = cite.open_citations(citations)
    claimant = request.claimant

    return {
        "schema": PAYLOAD_SCHEMA,
        "requestType": "CUSTOMS_DUTY_REFUND",
        "basis": "GCC_COMMON_CUSTOMS_LAW_ART_97",
        "claimReference": request.claim_id,
        "preparedOn": request.prepared_on.isoformat(),
        "currency": str(request.currency).upper(),
        "claimant": {
            "name": claimant.name,
            "identifier": claimant.identifier,
            "identifierType": "CR",
            "address": {
                "line1": claimant.address_line1,
                "line2": claimant.address_line2 or None,
                "city": claimant.city,
                "postalCode": claimant.postal_code or None,
                "country": claimant.country,
            },
            "contact": {
                "email": claimant.contact_email or None,
                "phone": claimant.contact_phone or None,
            },
            "brokerIdentifier": claimant.broker_identifier or None,
        },
        "period": {
            "from": request.period_start.isoformat(),
            "to": request.period_end.isoformat(),
            "filingDeadline": request.filing_deadline.isoformat(),
        },
        "declarations": [
            {
                "reExportDeclaration": {
                    "number": line.export_reference,
                    "lineNumber": line.export_line_number,
                    "date": line.export_date.isoformat(),
                    "destinationCountry": line.destination_country,
                    "hsCode": line.export_hts,
                    "quantity": _decimal(line.quantity_designated, "0.0001"),
                    "unitOfMeasure": line.unit_of_measure,
                    "goodsCondition": "UNUSED_UNALTERED",
                },
                # Rules of Implementation Art. 15(c). Without this object the request has
                # no theory at all, which is why it is not optional in the payload.
                "linkedImportDeclaration": {
                    "number": line.import_declaration,
                    "lineNumber": line.import_line_number,
                    "date": line.import_date.isoformat(),
                    "dutyPaymentDate": (
                        line.duty_payment_date.isoformat() if line.duty_payment_date else None
                    ),
                    "hsCode": line.import_hts,
                    "portOfEntry": line.port_of_entry or None,
                    "dutyPaid": _decimal(line.duty_paid),
                },
                "consignment": {
                    "consignmentId": line.consignment_id,
                    "isPartShipment": line.is_partial_shipment,
                    # Whether the platform accepts a below-quantity link is undetermined
                    # (§8.2). Flagged on the line so a partial claim is visibly the one
                    # carrying the open platform question, not silently identical to a
                    # full one.
                    "platformBehaviourUndetermined": line.is_partial_shipment,
                },
                "amounts": {
                    "dutyAttributable": _decimal(line.duty_allocated),
                    "refundClaimed": _decimal(line.refund_amount),
                },
                "matchTheory": line.theory,
            }
            for line in request.lines
        ],
        "totals": {
            "declarationCount": len(request.lines),
            "dutyAttributable": _decimal(request.total_duty_allocated),
            "refundClaimed": _decimal(request.total_refund),
            # Art. 16 §6 — duties actually paid, with no percentage haircut. This is not
            # the US 99%, and printing a rate here rather than assuming one is what keeps
            # the two lanes from being read as the same claim in different currencies.
            "refundRate": "1.00",
            "refundRateCitation": "GCC Rules of Implementation Art. 16 §6",
        },
        "settlement": {
            "iban": request.refund_account_iban or None,
            "note": (
                "Refund is settled after re-export and verification of the re-export "
                "documents (Rules of Implementation Art. 16 §7)."
            ),
        },
        "supportingDocuments": [
            {"code": code, "description": description, "attached": False}
            for code, description in REQUIRED_DOCUMENTS
        ],
        "citations": _citation_block(citations),
        "requiresAnalystReview": bool(open_),
        "analystReview": {
            "openCitations": len(open_),
            "blocking": bool(open_),
            "reason": cite.UNOBTAINABLE_REASON if open_ else None,
            "instruction": (
                "Supply the ZATCA Resolution 28624 article numbers before transmission. "
                "Do not submit with placeholders in place: a procedural article asserted "
                "wrongly is a misstatement to the authority."
                if open_
                else None
            ),
        },
        "notes": request.notes or None,
    }


def build_ksa_packet(request: PacketRequest) -> FilingPacket:
    """Render the KSA filing packet: one refund-request JSON payload."""
    if request.jurisdiction is not Jurisdiction.KSA:
        msg = f"build_ksa_packet received a {request.jurisdiction} claim"
        raise ValueError(msg)

    payload = build_payload(request)
    citations = claim_citations(request)

    packet = FilingPacket(
        claim_id=request.claim_id,
        jurisdiction=request.jurisdiction,
        citations=citations,
    )

    if not request.lines:
        packet.warnings.append("no linked declarations; the refund request would claim nothing")
    if not request.refund_account_iban:
        packet.warnings.append("no settlement IBAN; ZATCA cannot pay an approved refund")
    if any(line.is_partial_shipment for line in request.lines):
        packet.warnings.append(
            "partial consignment claimed; Fasah behaviour on a below-quantity link is "
            "undetermined (COMPLIANCE-GCC.md §8.2) pending the sandbox probe"
        )
    if packet.requires_analyst_review:
        packet.warnings.append(
            f"{len(packet.open_citations)} ZATCA procedural citations are unresolved; "
            "the packet is blocked from transmission"
        )

    packet.artifacts.append(
        PacketArtifact(
            filename=f"zatca-refund-{request.claim_id}.json",
            media_type=JSON_MEDIA_TYPE,
            # ensure_ascii=False so the Arabic in any narrative field stays Arabic rather
            # than becoming a wall of \\u escapes that no reviewer can check.
            content=json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        )
    )
    return packet

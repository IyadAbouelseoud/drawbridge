"""Assembling a filing packet from a persisted claim.

The packager renders and decides nothing; it needs a `PacketRequest` whose every figure is
already resolved. This module is the join that produces one: claim, refund lines, and the
entry and export lines each refund line points at.

**Why it reads from the database rather than taking the match result.** A packet must be
reproducible years after the workflow run that produced it. Building it from rows means a
packet regenerated in 2030 renders from the same stored facts as the one filed in 2026;
building it from a workflow payload means it renders from whatever n8n still has, which is
nothing.

**Why the claimant is supplied by the caller.** Filing identity — EIN, commercial
registration, broker code, bank account for a ZATCA settlement — is tenant profile data
this schema does not yet model. Inventing a placeholder here would put a fabricated
identifier on a document addressed to a customs authority, so the caller passes it and the
packager prints exactly what it was given.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from drawbridge_schemas.claim import RecoveryLane
from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from services.api.src.ledger import record
from services.api.src.models import Claim, EntryLine, ExportLine, RefundLine
from services.packager.src.packet import Claimant, PacketLine, PacketRequest
from services.packager.src.router import build_packet

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from services.packager.src.packet import FilingPacket


class PackagingError(RuntimeError):
    """The claim cannot be packaged as it stands."""


# States a packet may be built from. `approved` is the ordinary one; `packaged` is allowed
# so a packet can be regenerated — the artifacts are derived data and rebuilding them is
# not a state change. `analyst_review` and earlier are refused: a packet built from
# unreviewed figures looks exactly like one built from reviewed figures.
_PACKAGEABLE = frozenset({"approved", "packaged", "handed_off"})

# The §1313 subsection printed on the 7551, from the drawback type the claim was filed
# under. Absent from the map means the lane is not drawback and the router will say so.
_PROVISION: dict[str, str] = {
    "unused_direct_identity": "19 U.S.C. 1313(j)(1)",
    "unused_substitution": "19 U.S.C. 1313(j)(2)",
    "manufacturing_direct_identity": "19 U.S.C. 1313(a)",
    "manufacturing_substitution": "19 U.S.C. 1313(b)",
    "rejected_merchandise": "19 U.S.C. 1313(c)",
}


def _lines(session: Session, claim_id: UUID) -> list[PacketLine]:
    rows = session.execute(
        select(RefundLine, EntryLine, ExportLine)
        .join(EntryLine, RefundLine.import_line_id == EntryLine.line_id)
        .join(ExportLine, RefundLine.export_line_id == ExportLine.line_id)
        .where(RefundLine.claim_id == claim_id)
        .order_by(EntryLine.declaration_number, EntryLine.line_number)
    ).all()

    if not rows:
        msg = f"claim {claim_id} has no refund lines to package"
        raise PackagingError(msg)

    return [
        PacketLine(
            import_declaration=entry.declaration_number,
            import_line_number=entry.line_number,
            import_date=entry.import_date,
            import_hts=entry.hts_code,
            description=entry.description,
            quantity_designated=refund.quantity,
            unit_of_measure=entry.unit_of_measure,
            duty_paid=entry.duty_paid,
            duty_allocated=refund.duty_component,
            refund_amount=refund.refund_amount,
            export_reference=export.reference,
            export_line_number=export.line_number,
            export_date=export.export_date,
            export_hts=export.hts_code,
            destination_country=export.destination_country,
            theory=refund.theory,
            duty_payment_date=entry.duty_payment_date,
            port_of_entry=entry.port_of_entry,
            substitution_key=refund.substitution_key,
            consignment_id=export.consignment_id,
            is_partial_shipment=export.is_partial_shipment,
        )
        for refund, entry, export in rows
    ]


def build_request(
    session: Session,
    *,
    claim_id: UUID,
    claimant: Claimant,
    prepared_on: date | None = None,
    manufacturer: Claimant | None = None,
    refund_account_iban: str = "",
    port_code: str = "",
    notes: str = "",
) -> PacketRequest:
    """Read one claim back out as a packet request.

    Refuses a claim that has not been approved. The check is here rather than in the
    caller because a caller that forgets it produces a document that is indistinguishable
    from a reviewed one.
    """
    claim = session.get(Claim, claim_id)
    if claim is None:
        msg = f"no claim {claim_id}"
        raise PackagingError(msg)
    if claim.state not in _PACKAGEABLE:
        msg = (
            f"claim {claim_id} is in state {claim.state!r}; a packet may only be built "
            f"from {sorted(_PACKAGEABLE)}"
        )
        raise PackagingError(msg)

    return PacketRequest(
        claim_id=str(claim.claim_id),
        tenant_id=str(claim.tenant_id),
        jurisdiction=Jurisdiction(claim.jurisdiction),
        currency=Currency(claim.currency),
        claimant=claimant,
        lines=_lines(session, claim_id),
        period_start=claim.period_start,
        period_end=claim.period_end,
        filing_deadline=claim.filing_deadline,
        prepared_on=prepared_on or date.today(),
        lane=RecoveryLane(claim.lane),
        drawback_provision=_PROVISION.get(claim.drawback_type or "", ""),
        port_code=port_code,
        manufacturer=manufacturer,
        refund_account_iban=refund_account_iban,
        notes=notes,
    )


def build(
    session: Session,
    *,
    claim_id: UUID,
    claimant: Claimant,
    prepared_on: date | None = None,
    **options: Any,
) -> FilingPacket:
    """Assemble and render. The one call the API route makes."""
    request = build_request(
        session,
        claim_id=claim_id,
        claimant=claimant,
        prepared_on=prepared_on,
        **options,
    )
    packet = build_packet(request)

    claim = session.get(Claim, claim_id)
    if claim is not None:
        # What was filed, and whether it could be. `transmittable` is recorded because a
        # packet blocked by an open citation and a packet that was never built look the
        # same from outside, and only one of them is a compliance posture.
        record(
            session,
            tenant_id=claim.tenant_id,
            claim_id=claim_id,
            event_type="packet_built",
            actor="pipeline",
            subject=claim.lane,
            payload={
                "artifacts": [a.filename for a in packet.artifacts],
                "transmittable": not packet.requires_analyst_review,
                "open_citations": [f"{c.authority} {c.article}" for c in packet.open_citations],
                "total_refund": str(claim.total_refund),
            },
        )
    return packet

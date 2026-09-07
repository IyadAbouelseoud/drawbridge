"""The packet contract — what the packager is handed, and what it hands back.

The packager renders. It does not decide, compute, or discover: every figure it prints
arrives already quantified by the matcher and already checked by the rules engine. That is
the same rule the LLM operates under, applied one layer further out, and it is why a
packet can be regenerated years later and reproduce byte-for-byte from stored input.

One thing it *does* decide: whether the packet may be transmitted. A packet carrying an
open citation (`citations.CitationStatus.ANALYST_REVIEW`) sets `requires_analyst_review`
and must not be sent to a customs authority until the citation is closed. That check lives
here rather than in the caller, because a caller that forgets it produces a packet that
looks finished.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from services.packager.src.citations import Citation, open_citations

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class Claimant:
    """Who is claiming, as the authority needs to see them."""

    name: str
    identifier: str
    """US: IRS/EIN with suffix. KSA: commercial registration or TIN."""

    address_line1: str
    city: str
    country: str
    postal_code: str = ""
    address_line2: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    broker_identifier: str = ""
    """Filing agent, where one is used. Drawbridge never files; a licensed filer does."""

    def address_block(self) -> str:
        parts = [self.address_line1, self.address_line2, self.city, self.postal_code]
        return ", ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class PacketLine:
    """One import-to-export designation as it appears on the packet.

    Flat, already-resolved strings and Decimals. The packager does no lookups: a line that
    reaches here without its export reference is a bug upstream, not something to go and
    fetch.
    """

    import_declaration: str
    import_line_number: int
    import_date: date
    import_hts: str
    description: str
    quantity_designated: Decimal
    unit_of_measure: str
    duty_paid: Decimal
    duty_allocated: Decimal
    refund_amount: Decimal

    export_reference: str
    export_line_number: int
    export_date: date
    export_hts: str
    destination_country: str | None
    theory: str

    duty_payment_date: date | None = None
    port_of_entry: str = ""
    substitution_key: str | None = None
    bom_path: str | None = None
    """Route through the bill of materials for a manufacturing designation, leaf last.

    Present only on §1313(a)/(b) lines. It is what lets an auditor reproduce the
    multiplier: the compounded consumption is a property of the route, and a claim that
    prints only the leaf cannot show how the number was reached."""

    consignment_id: str | None = None
    is_partial_shipment: bool = False


@dataclass(frozen=True, slots=True)
class PacketRequest:
    """Everything needed to render one filing packet."""

    claim_id: str
    tenant_id: str
    jurisdiction: Jurisdiction
    currency: Currency
    claimant: Claimant
    lines: Sequence[PacketLine]
    period_start: date
    period_end: date
    filing_deadline: date
    prepared_on: date

    drawback_provision: str = ""
    """US only: the §1313 subsection claimed, printed on the 7551."""

    port_code: str = ""
    manufacturer: Claimant | None = None
    """7552 transferor, where the claim rests on a certificate of manufacture."""

    refund_account_iban: str = ""
    """KSA only. ZATCA settles an approved refund to a registered bank account."""

    notes: str = ""

    @property
    def total_duty_allocated(self) -> Decimal:
        return sum((line.duty_allocated for line in self.lines), Decimal("0.00"))

    @property
    def total_refund(self) -> Decimal:
        return sum((line.refund_amount for line in self.lines), Decimal("0.00"))

    @property
    def total_quantity(self) -> Decimal:
        return sum((line.quantity_designated for line in self.lines), Decimal("0"))


@dataclass(frozen=True, slots=True)
class PacketArtifact:
    """One rendered file in the packet."""

    filename: str
    media_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class FilingPacket:
    """The rendered output, plus whether it may actually be filed."""

    claim_id: str
    jurisdiction: Jurisdiction
    artifacts: list[PacketArtifact] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def open_citations(self) -> list[Citation]:
        return open_citations(self.citations)

    @property
    def requires_analyst_review(self) -> bool:
        """Whether an analyst must clear this packet before it is transmitted.

        True whenever any citation is open. There is no severity gradient: a refund
        request naming a procedural article it cannot support is a misstatement to the
        authority regardless of how many others are sound.
        """
        return bool(self.open_citations)

    def artifact(self, filename: str) -> PacketArtifact:
        for item in self.artifacts:
            if item.filename == filename:
                return item
        held = [a.filename for a in self.artifacts]
        msg = f"packet has no artifact {filename!r}; it holds {held}"
        raise KeyError(msg)

    def manifest(self) -> dict[str, Any]:
        """What was produced and what still blocks it.

        Stored alongside the artifacts. An auditor asking why a packet sat unfiled for
        three weeks gets the answer from the manifest rather than from a chat log.
        """
        return {
            "claim_id": self.claim_id,
            "jurisdiction": self.jurisdiction,
            "artifacts": [
                {"filename": a.filename, "media_type": a.media_type, "bytes": a.size}
                for a in self.artifacts
            ],
            "citations": [c.as_dict() for c in self.citations],
            "open_citations": len(self.open_citations),
            "requires_analyst_review": self.requires_analyst_review,
            "warnings": list(self.warnings),
        }


def money(value: Decimal) -> str:
    """Two decimal places, always. A customs figure never prints as '1250.5'."""
    return f"{value.quantize(Decimal('0.01')):,.2f}"


def quantity(value: Decimal) -> str:
    """Four decimal places, matching the Numeric(18,4) quantity column.

    Trailing zeros are kept rather than stripped: '100.0000' and '100' describe the same
    quantity, but only the first shows the precision the designation was recorded at.
    """
    return f"{value.quantize(Decimal('0.0001')):,.4f}"

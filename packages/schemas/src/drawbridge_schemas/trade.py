"""Trade primitives: entry lines, export lines, and the matches between them.

Dual-jurisdiction as of week 2. Fields that exist in only one customs union are optional
and documented with the provision that requires them; the rules engine enforces presence
per jurisdiction rather than the schema forcing every tenant to carry both shapes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drawbridge_schemas.jurisdiction import (
    ClockAnchor,
    Currency,
    Jurisdiction,
    JurisdictionProfile,
    MatchTheory,
    profile_for,
)
from drawbridge_schemas.provenance import Provenance

# Money is Decimal throughout. Never float — a claim is a dollar figure filed with a
# customs authority and must reproduce to the cent.
Money = Annotated[Decimal, Field(max_digits=16, decimal_places=2)]


def _untraceable_figures(record: BaseModel, fields: tuple[str, ...]) -> list[str]:
    """Figure fields carrying a value with no box behind it.

    The rule enforced by callers: a record read off a paginated document — a 7501, a
    *Bayan*, an invoice — must carry a `ProvenanceSpan` for every figure it states. A
    record fed in from EDI or an ERP export has no pages, so its figures are addressed by
    `field_path` on the record's spans instead, which is what week 9's pipeline supplies.

    Zero and None are exempt. A duty of 0.00 on a line that paid no duty is the field's
    default rather than a figure someone read, and demanding coordinates for it would
    mean fabricating a box that points at whitespace — which is the failure this whole
    mechanism exists to prevent.
    """
    provenance: Provenance = record.provenance  # type: ignore[attr-defined]
    missing = []
    for name in fields:
        value = getattr(record, name, None)
        if value is None or value == 0:
            continue
        if provenance.figure(name) is None:
            missing.append(name)
    return missing


class HTSCode(BaseModel):
    """Harmonized tariff code.

    US HTS is 10 digits. Saudi/GCC declarations carry HS codes of 8 or 12 digits
    depending on the schedule version, so length is validated per jurisdiction rather
    than by a single pattern.

    US drawback substitution under TFTEA pivots on the first 8 digits, which is what
    `substitution_key` exposes. The GCC has no substitution theory at all — see
    docs/COMPLIANCE-GCC.md §2.2.
    """

    model_config = ConfigDict(frozen=True)

    code: Annotated[str, Field(pattern=r"^\d{6,12}$")]

    @property
    def substitution_key(self) -> str:
        """First 8 digits — the TFTEA substitution unit."""
        return self.code[:8]

    @property
    def heading(self) -> str:
        """First 4 digits. Meaningful in every HS-derived schedule."""
        return self.code[:4]

    @property
    def hs6(self) -> str:
        """First 6 digits — the internationally harmonised portion."""
        return self.code[:6]

    def substitutable_with(self, other: HTSCode) -> bool:
        """Whether TFTEA substitution could pair these two codes.

        True does not authorise a match. Substitution is a US-only theory; callers must
        gate on `JurisdictionProfile.permits(MatchTheory.HTS_SUBSTITUTION)` first.
        """
        return self.substitution_key == other.substitution_key


class EntryLine(BaseModel):
    """One line of an import declaration.

    US: CBP 7501 / ACE ES-001 entry summary line.
    KSA: ZATCA customs declaration (*Bayan*) line.
    """

    model_config = ConfigDict(frozen=True)

    line_id: UUID
    tenant_id: UUID
    jurisdiction: Jurisdiction = Jurisdiction.US
    currency: Currency = Currency.USD

    declaration_number: str = Field(
        min_length=3,
        description="US: CBP entry number (NNN-NNNNNNN-N). KSA: Bayan declaration number. "
        "Formats differ, so shape validation lives in the per-jurisdiction ingest parser.",
    )
    line_number: int = Field(ge=1)

    import_date: date
    declaration_date: date
    duty_payment_date: date | None = Field(
        default=None,
        description="Date duties were actually paid. Anchors the GCC re-export window "
        "(Rules of Impl. Art. 16 §3(a)). Differs from import_date whenever payment is "
        "postponed — ZATCA permits up to 30 days against guarantee.",
    )

    port_of_entry: str = Field(description="US: 4-digit CBP port code. KSA: ZATCA port.")
    country_of_origin: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]

    hts: HTSCode
    description: str
    quantity: Decimal
    unit_of_measure: str

    entered_value: Money
    duty_paid: Money
    mpf_paid: Money = Decimal("0.00")
    hmf_paid: Money = Decimal("0.00")
    section_301_duty: Money = Decimal("0.00")
    other_duty: Money = Decimal("0.00")

    # Consumption taxes. Tracked for reconciliation, never part of a drawback base:
    # KSA import VAT (15%) is recovered through the VAT return as input tax, and excise
    # has its own refund procedure. See docs/COMPLIANCE-GCC.md §2.4.
    vat_paid: Money = Decimal("0.00")
    excise_paid: Money = Decimal("0.00")

    quantity_designated: Decimal = Field(
        default=Decimal("0"),
        description="Quantity already consumed by prior claims. Guards double-claiming.",
    )

    provenance: Provenance

    def recoverable_base(self, profile: JurisdictionProfile) -> Decimal:
        """Duty, plus fees where the jurisdiction allows them, eligible for refund.

        Consumption tax is excluded in both jurisdictions.
        """
        base = self.duty_paid + self.section_301_duty + self.other_duty
        if profile.includes_fees_in_base:
            base += self.mpf_paid + self.hmf_paid
        return base

    @property
    def eligibility_clock_start(self) -> date:
        """The date this line's re-export window runs from.

        Falls back to import_date when the payment date is unknown. That is the
        conservative direction: it can only shorten the window, never extend it beyond
        what the statute allows.
        """
        profile = profile_for(self.jurisdiction)
        if profile.clock_anchor is ClockAnchor.DUTY_PAYMENT_DATE:
            return self.duty_payment_date or self.import_date
        return self.import_date

    @property
    def quantity_available(self) -> Decimal:
        return self.quantity - self.quantity_designated

    #: Figures a customs authority can ask us to evidence, line by line.
    TRACEABLE_FIGURES: ClassVar[tuple[str, ...]] = (
        "quantity",
        "entered_value",
        "duty_paid",
        "mpf_paid",
        "hmf_paid",
        "section_301_duty",
        "other_duty",
        "vat_paid",
        "excise_paid",
    )

    @model_validator(mode="after")
    def _figures_carry_their_boxes(self) -> Self:
        """Every stated figure on a document-sourced line must cite a box.

        This is the CLAUDE.md invariant made structural rather than aspirational: until
        week 10 a line could carry one span covering the whole page and satisfy
        "traceable to a source-document span" without any figure being locatable. An
        analyst clicking a refund figure needs the rectangle, not the page.
        """
        if not self.provenance.cites_a_paginated_document:
            return self
        missing = _untraceable_figures(self, self.TRACEABLE_FIGURES)
        if missing:
            msg = (
                f"entry line {self.declaration_number}/{self.line_number} states "
                f"{missing} with no ProvenanceSpan; a figure read off a document must "
                "carry the box it was read from"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _designation_within_bounds(self) -> Self:
        if self.quantity_designated > self.quantity:
            msg = (
                f"declaration {self.declaration_number} line {self.line_number}: "
                f"designated {self.quantity_designated} exceeds imported {self.quantity}"
            )
            raise ValueError(msg)
        return self


class ValuationBasis(StrEnum):
    """Which valuation the declared figure was captured on.

    The GCC law carries two bases pointing in different directions, and confusing them
    is silent in both directions (docs/COMPLIANCE-GCC.md §8.1):

    - imports are valued CIF, to the port of destination (Valuation Art. 1(I)(5));
    - exports are valued per Art. 28 — declared value plus costs **to the customs
      office**, which is FOB plus the inland leg and excludes onward freight.

    The Art. 16 §2 threshold screens the *re-export* value, so it needs the Art. 28
    figure. A CIF number overstates it and passes claims that should fail; a bare FOB
    number understates it and fails claims that should pass.
    """

    ART_28_EXPORT = "art_28_export"
    """Declared value plus costs to the customs office. The basis the threshold wants."""

    FOB = "fob"
    """Free on board, excluding the inland leg. Understates the Art. 28 figure."""

    CIF = "cif"
    """Cost, insurance and freight. The import basis; overstates a re-export value."""

    UNKNOWN = "unknown"
    """Source did not say. Routes to review rather than being screened on a guess."""


class ExportLine(BaseModel):
    """One line of an export, re-export, or destruction event.

    The GCC linkage fields are what make an Article 97 claim possible at all: Rules of
    Implementation Art. 15(c) requires the import declaration number to be affixed to the
    re-export declaration, and ZATCA Resolution 28624 makes that link the basis of the
    refund.
    """

    model_config = ConfigDict(frozen=True)

    line_id: UUID
    tenant_id: UUID
    jurisdiction: Jurisdiction = Jurisdiction.US

    reference: str = Field(description="BOL / AWB / re-export declaration number")
    line_number: int = Field(ge=1)

    export_date: date
    destination_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    is_destruction: bool = False

    hts: HTSCode
    description: str
    quantity: Decimal
    unit_of_measure: str
    declared_value: Money | None = Field(
        default=None,
        description="Value of the re-exported goods, on the Art. 28 basis: declared "
        "value plus costs to the customs office. Screened against the GCC USD 5,000 "
        "minimum (Rules of Impl. Art. 16 §2) before any extraction spend.",
    )
    valuation_basis: ValuationBasis = Field(
        default=ValuationBasis.ART_28_EXPORT,
        description="Which basis declared_value was captured on. Anything other than "
        "ART_28_EXPORT means the threshold would be screened on the wrong footing — see "
        "docs/COMPLIANCE-GCC.md §8.1.",
    )

    quantity_claimed: Decimal = Field(
        default=Decimal("0"),
        description="Quantity already matched by prior claims.",
    )

    # --- GCC linkage ---
    linked_import_declaration: str | None = Field(
        default=None,
        description="Import declaration number carried on the re-export declaration "
        "(Rules of Impl. Art. 15(c)). Without it a GCC claim has no theory.",
    )
    consignment_id: str | None = Field(
        default=None,
        description="Groups part shipments proven to belong to a single consignment "
        "(Rules of Impl. Art. 16 §4).",
    )
    is_partial_shipment: bool = False
    unused_and_unaltered: bool = Field(
        default=True,
        description="Art. 16 §5 — not locally used after import, in the same condition "
        "as imported. False voids a GCC drawback claim outright.",
    )

    provenance: Provenance

    @property
    def quantity_available(self) -> Decimal:
        return self.quantity - self.quantity_claimed

    #: Export-side figures. `quantity_claimed` is a claim decision, not an extraction.
    TRACEABLE_FIGURES: ClassVar[tuple[str, ...]] = ("quantity", "declared_value")

    @model_validator(mode="after")
    def _figures_carry_their_boxes(self) -> Self:
        """As `EntryLine._figures_carry_their_boxes`.

        The export side matters as much as the import side under GCC Rules of Impl.
        Art. 16 §2, where the re-export value is what the USD 5,000 minimum is tested
        against — a threshold decision resting on a figure nobody can locate is a
        decision that cannot be defended.
        """
        if not self.provenance.cites_a_paginated_document:
            return self
        missing = _untraceable_figures(self, self.TRACEABLE_FIGURES)
        if missing:
            msg = (
                f"export line {self.reference}/{self.line_number} states {missing} with "
                "no ProvenanceSpan; a figure read off a document must carry the box it "
                "was read from"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _destination_or_destruction(self) -> Self:
        if not self.is_destruction and self.destination_country is None:
            msg = (
                f"export {self.reference} line {self.line_number}: "
                "needs destination or destruction flag"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _gcc_requires_declaration_link(self) -> Self:
        if self.jurisdiction is Jurisdiction.KSA and self.linked_import_declaration is None:
            msg = (
                f"re-export {self.reference} line {self.line_number}: GCC claims require "
                "linked_import_declaration (Rules of Implementation Art. 15(c))"
            )
            raise ValueError(msg)
        return self


class LineMatch(BaseModel):
    """An import line paired to an export line under a specific statutory theory.

    US matches come from the CP-SAT allocator over a substitution-eligible pool; GCC
    matches come from a declaration-linkage walk. `theory` records which, and is
    validated against the jurisdiction profile before quantification.
    """

    model_config = ConfigDict(frozen=True)

    import_line_id: UUID
    export_line_id: UUID
    quantity: Decimal = Field(gt=0)

    theory: MatchTheory = Field(description="The statutory theory this pairing rests on.")
    substitution_key: Annotated[str, Field(pattern=r"^\d{8}$")] | None = None
    linked_import_declaration: str | None = Field(
        default=None, description="Required for MatchTheory.DECLARATION_LINKAGE."
    )

    duty_allocated: Money = Field(description="Pro-rata duty attributed to this quantity")
    refund_amount: Money = Field(
        description="duty_allocated x the jurisdiction refund rate, rounded to the cent"
    )

    days_clock_start_to_export: int = Field(
        ge=0,
        description="Days from the jurisdiction's clock anchor to export. US anchors on "
        "the import date; GCC on the duty-payment date.",
    )

    @model_validator(mode="after")
    def _theory_carries_its_evidence(self) -> Self:
        if self.theory is MatchTheory.HTS_SUBSTITUTION and self.substitution_key is None:
            msg = "substitution match requires substitution_key"
            raise ValueError(msg)
        if (
            self.theory is MatchTheory.DECLARATION_LINKAGE
            and self.linked_import_declaration is None
        ):
            msg = "declaration-linkage match requires linked_import_declaration"
            raise ValueError(msg)
        return self

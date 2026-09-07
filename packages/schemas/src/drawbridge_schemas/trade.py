"""Trade primitives: entry lines, export lines, and the matches between them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drawbridge_schemas.provenance import Provenance

# Money is Decimal throughout. Never float — a claim is a dollar figure filed with a
# federal agency and must reproduce to the cent.
Money = Annotated[Decimal, Field(max_digits=16, decimal_places=2)]


class HTSCode(BaseModel):
    """Harmonized Tariff Schedule code.

    US HTS is 10 digits. Drawback substitution under TFTEA pivots on the first 8, so
    `substitution_key` is the comparison unit for §1313(j)(2) matching — never the full 10.
    """

    model_config = ConfigDict(frozen=True)

    code: Annotated[str, Field(pattern=r"^\d{10}$")]

    @property
    def substitution_key(self) -> str:
        """First 8 digits — the TFTEA substitution unit."""
        return self.code[:8]

    @property
    def heading(self) -> str:
        """First 4 digits."""
        return self.code[:4]

    def substitutable_with(self, other: HTSCode) -> bool:
        return self.substitution_key == other.substitution_key


class EntryLine(BaseModel):
    """One line of an import entry summary (CBP 7501 / ACE ES-001).

    `duty_paid` is the recoverable base. Fees (MPF, HMF) are tracked separately because
    their drawback treatment differs from ad valorem duty.
    """

    model_config = ConfigDict(frozen=True)

    line_id: UUID
    tenant_id: UUID
    entry_number: Annotated[str, Field(pattern=r"^[A-Z0-9]{3}-?\d{7}-?\d$")]
    line_number: int = Field(ge=1)

    import_date: date
    entry_summary_date: date
    port_of_entry: Annotated[str, Field(pattern=r"^\d{4}$")]
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

    quantity_designated: Decimal = Field(
        default=Decimal("0"),
        description="Quantity already consumed by prior claims. Guards double-claiming.",
    )

    provenance: Provenance

    @property
    def total_recoverable_base(self) -> Decimal:
        """Duty + fees eligible for 99% refund."""
        return self.duty_paid + self.mpf_paid + self.hmf_paid + self.section_301_duty

    @property
    def quantity_available(self) -> Decimal:
        return self.quantity - self.quantity_designated

    @model_validator(mode="after")
    def _designation_within_bounds(self) -> Self:
        if self.quantity_designated > self.quantity:
            msg = (
                f"entry {self.entry_number} line {self.line_number}: designated "
                f"{self.quantity_designated} exceeds imported {self.quantity}"
            )
            raise ValueError(msg)
        return self


class ExportLine(BaseModel):
    """One line of an export or destruction event — the drawback-triggering side."""

    model_config = ConfigDict(frozen=True)

    line_id: UUID
    tenant_id: UUID
    reference: str = Field(description="BOL / AWB / destruction certificate number")
    line_number: int = Field(ge=1)

    export_date: date
    destination_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    is_destruction: bool = False

    hts: HTSCode
    description: str
    quantity: Decimal
    unit_of_measure: str

    quantity_claimed: Decimal = Field(
        default=Decimal("0"),
        description="Quantity already matched by prior claims.",
    )

    provenance: Provenance

    @property
    def quantity_available(self) -> Decimal:
        return self.quantity - self.quantity_claimed

    @model_validator(mode="after")
    def _destination_or_destruction(self) -> Self:
        if not self.is_destruction and self.destination_country is None:
            msg = (
                f"export {self.reference} line {self.line_number}: "
                "needs destination or destruction flag"
            )
            raise ValueError(msg)
        return self


class LineMatch(BaseModel):
    """An import line paired to an export line under a specific §1313 theory.

    Produced by the CP-SAT allocator. `quantity` is the allocated amount, which may be a
    partial slice of either side.
    """

    model_config = ConfigDict(frozen=True)

    import_line_id: UUID
    export_line_id: UUID
    quantity: Decimal = Field(gt=0)

    is_direct_identity: bool = Field(
        description="True = §1313(j)(1) same-article. False = §1313(j)(2) substitution."
    )
    substitution_key: Annotated[str, Field(pattern=r"^\d{8}$")] | None = None

    duty_allocated: Money = Field(description="Pro-rata duty attributed to this quantity")
    refund_amount: Money = Field(description="99% of duty_allocated, rounded to the cent")

    days_import_to_export: int = Field(
        ge=0, description="Must be <= 1826 (5 years) for §1313(j) eligibility"
    )

    @model_validator(mode="after")
    def _substitution_key_present_when_needed(self) -> Self:
        if not self.is_direct_identity and self.substitution_key is None:
            msg = "substitution match requires substitution_key"
            raise ValueError(msg)
        return self

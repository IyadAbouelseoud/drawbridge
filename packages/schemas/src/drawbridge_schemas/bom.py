"""Bill of materials — the bridge between raw material imports and finished-good exports.

Under §1313(j) an allocation of *q* imported units satisfies *q* exported units: the
exchange rate between the two sides is 1. Under §1313(a)/(b) it is the BOM multiplier,
so the units no longer cancel.

TFTEA requires a bill of materials or formula with the claim, identifying merchandise and
article by 8-digit HTS subheading and the quantity of merchandise. The designated quantity
must be the quantity **actually used** to produce the exported article — not purchased,
not on hand.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drawbridge_schemas.provenance import Provenance
from drawbridge_schemas.trade import HTSCode


class ManufacturingBasis(StrEnum):
    """Which §1313 manufacturing provision the component rests on."""

    DIRECT_IDENTITY = "direct_identity"
    """§1313(a) — the same imported merchandise is used in the manufacture."""

    SUBSTITUTION = "substitution"
    """§1313(b) — merchandise under the same 8-digit HTS subheading is substituted."""


class BomComponent(BaseModel):
    """One component line of a finished good's bill of materials.

    `quantity_per_unit` is the quantity embodied in one finished unit. `yield_rate` is the
    fraction of input that survives the process. Consumption is the first divided by the
    second, and both halves are kept so a derivation trail can show an auditor which is
    which — claiming only the embodied quantity under-claims, and claiming input without
    evidencing yield over-claims.
    """

    model_config = ConfigDict(frozen=True)

    component_hts: HTSCode
    description: str

    quantity_per_unit: Annotated[Decimal, Field(gt=0)]
    """Component units embodied in one finished unit."""

    unit_of_measure: str

    yield_rate: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("1")
    """Fraction of input surviving the process. 0.95 means 5% scrap or process loss."""

    basis: ManufacturingBasis = ManufacturingBasis.SUBSTITUTION

    relative_value_share: Annotated[Decimal, Field(gt=0, le=1)] | None = None
    """Where one process yields several products, the share of component cost attributed
    to this output by value at the time of separation (19 CFR 190 subpart B). None for
    single-output processes, where the question does not arise."""

    provenance: Provenance | None = None

    @property
    def consumption_per_unit(self) -> Decimal:
        """Component units consumed per finished unit, net of yield loss."""
        return self.quantity_per_unit / self.yield_rate

    def required_for(self, finished_quantity: Decimal) -> Decimal:
        """Component quantity actually used to produce `finished_quantity` units.

        This is the ceiling on what may be designated — TFTEA ties the designated
        quantity to the quantity actually used.
        """
        required = self.consumption_per_unit * finished_quantity
        if self.relative_value_share is not None:
            required *= self.relative_value_share
        return required


class BillOfMaterials(BaseModel):
    """The components of one finished good, as kept in the normal course of business.

    Flattened to a single level at ingest. A component that is itself manufactured from
    other imports is a week 6+ concern needing real ERP integration; the model here holds
    either way, since a flattened BOM is a special case of a nested one.
    """

    model_config = ConfigDict(frozen=True)

    bom_id: UUID
    tenant_id: UUID
    finished_good_hts: HTSCode
    finished_good_description: str
    finished_unit_of_measure: str

    components: tuple[BomComponent, ...] = Field(min_length=1)

    effective_from: str | None = None
    """ERP revision marker. A BOM that changed mid-period cannot be applied to exports
    manufactured before the change."""

    def component_for(self, hts: HTSCode) -> BomComponent | None:
        """The component an import line could satisfy, under either manufacturing basis.

        Direct identity needs the full tariff code; substitution needs the 8-digit
        subheading, which is what §1313(b) pivots on.
        """
        for component in self.components:
            if component.basis is ManufacturingBasis.DIRECT_IDENTITY:
                if component.component_hts.code == hts.code:
                    return component
            elif component.component_hts.substitutable_with(hts):
                return component
        return None

    @model_validator(mode="after")
    def _value_shares_are_coherent(self) -> Self:
        """Relative-value shares, where present, cannot exceed the whole.

        A process whose joint products claim more than 100% of a component's cost is
        double-counting that component across outputs.
        """
        shares = [
            c.relative_value_share for c in self.components if c.relative_value_share is not None
        ]
        if shares and sum(shares) > Decimal("1"):
            msg = (
                f"relative value shares sum to {sum(shares)}, exceeding 1 — joint "
                "products would double-count component cost (19 CFR 190 subpart B)"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _components_are_distinct(self) -> Self:
        keys = [c.component_hts.code for c in self.components]
        if len(keys) != len(set(keys)):
            msg = "duplicate component HTS codes in one bill of materials"
            raise ValueError(msg)
        return self

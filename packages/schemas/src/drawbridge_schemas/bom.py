"""Bill of materials — the bridge between raw material imports and finished-good exports.

Under §1313(j) an allocation of *q* imported units satisfies *q* exported units: the
exchange rate between the two sides is 1. Under §1313(a)/(b) it is the BOM multiplier,
so the units no longer cancel.

TFTEA requires a bill of materials or formula with the claim, identifying merchandise and
article by 8-digit HTS subheading and the quantity of merchandise. The designated quantity
must be the quantity **actually used** to produce the exported article — not purchased,
not on hand.

**Nesting (week 6).** A component may itself be a subassembly manufactured from imported
parts. A flattened BOM cannot express that: it would have to pre-multiply the yields, and
pre-multiplication throws away which stage lost the material. The auditor's question is
not "what is the multiplier" but "where did the 8% go", and only the tree answers it.

Yield compounds down the tree. A finished good needing one subassembly at 90% yield, where
that subassembly needs two castings at 80% yield, consumes 2 / 0.8 / 0.9 = 2.7778 castings
per finished unit — not 2 / (0.8 x 0.9) applied at one level, which happens to give the
same number here but stops doing so the moment a relative-value share enters at an
intermediate level.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drawbridge_schemas.provenance import Provenance
from drawbridge_schemas.trade import HTSCode

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


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

    sub_components: tuple[BomComponent, ...] = ()
    """Components of this component, where it is a subassembly manufactured in-house.

    Empty for a purchased or imported part, which is the leaf case and the only one that
    can be designated against an import line. A node with children is a manufacturing
    stage: it consumes its children and is itself consumed by its parent, but nothing was
    imported *as* the subassembly, so it carries no designation of its own."""

    @property
    def is_leaf(self) -> bool:
        """Whether this node is a purchased part rather than an in-house stage.

        Only leaves are designatable. An intermediate node is a stage in the process, and
        designating against it would claim duty on an article that was never imported.
        """
        return not self.sub_components

    @property
    def consumption_per_unit(self) -> Decimal:
        """Component units consumed per unit of the *parent*, net of this node's yield.

        Local to one level. The compounded figure across a nested tree is
        `consumption_per_finished_unit`, which walks the path.
        """
        return self.quantity_per_unit / self.yield_rate

    def required_for(self, finished_quantity: Decimal) -> Decimal:
        """Component quantity used to produce `finished_quantity` units of the parent.

        This is the ceiling on what may be designated — TFTEA ties the designated
        quantity to the quantity actually used.
        """
        required = self.consumption_per_unit * finished_quantity
        if self.relative_value_share is not None:
            required *= self.relative_value_share
        return required

    def walk(self, path: tuple[BomComponent, ...] = ()) -> Iterator[tuple[BomComponent, ...]]:
        """Every root-to-node path through this subtree, this node's path first.

        Paths rather than bare nodes because the multiplier is a property of the *route*:
        the same casting reached through two different subassemblies is consumed at two
        different rates, and a caller handed only the node cannot tell them apart.
        """
        here = (*path, self)
        yield here
        for child in self.sub_components:
            yield from child.walk(here)

    @model_validator(mode="after")
    def _sub_components_are_distinct(self) -> Self:
        keys = [c.component_hts.code for c in self.sub_components]
        if len(keys) != len(set(keys)):
            msg = (
                f"component {self.component_hts.code} lists a duplicate sub-component "
                "HTS code; the two lines would each claim the full quantity"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _sub_component_value_shares_are_coherent(self) -> Self:
        shares = [
            c.relative_value_share
            for c in self.sub_components
            if c.relative_value_share is not None
        ]
        if shares and sum(shares) > Decimal("1"):
            msg = (
                f"sub-component relative value shares under {self.component_hts.code} "
                f"sum to {sum(shares)}, exceeding 1 (19 CFR 190 subpart B)"
            )
            raise ValueError(msg)
        return self


class BillOfMaterials(BaseModel):
    """The components of one finished good, as kept in the normal course of business.

    Nested as of week 6. `components` are the immediate children of the finished good;
    each may carry `sub_components` to whatever depth the process actually has. A
    single-level BOM is the depth-1 case of the same structure, so nothing that consumed
    a flat BOM has to change.

    Only **leaves** are designatable. An intermediate node is an in-house manufacturing
    stage: it was produced, not imported, so there is no entry line to designate against
    it. `designatable_paths` is what the matcher consumes, and it yields leaves only.
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

    def walk(self) -> Iterator[tuple[BomComponent, ...]]:
        """Every root-to-node path in the tree, depth-first, in declaration order.

        Deterministic order matters: the matcher builds a CP-SAT model from these paths,
        and a model whose variables are created in a different order can solve to a
        different optimum among equally-valued allocations.
        """
        for component in self.components:
            yield from component.walk()

    def designatable_paths(self) -> Iterator[tuple[BomComponent, ...]]:
        """Paths ending at a leaf — the only nodes an import line may be designated to."""
        for path in self.walk():
            if path[-1].is_leaf:
                yield path

    @property
    def depth(self) -> int:
        """Longest root-to-leaf path length. 1 for a flat bill of materials."""
        return max((len(path) for path in self.walk()), default=0)

    def component_for(self, hts: HTSCode) -> BomComponent | None:
        """The leaf component an import line could satisfy, under either basis.

        Retained for the single-level callers. Where the tree is nested this loses the
        route — and with it the compounded multiplier — so the matcher uses `path_for`
        instead. Kept because a depth-1 BOM has exactly one route per component, and
        there the two agree.
        """
        path = self.path_for(hts)
        return path[-1] if path else None

    def path_for(self, hts: HTSCode) -> tuple[BomComponent, ...] | None:
        """The root-to-leaf route an import line satisfies, under either basis.

        Direct identity needs the full tariff code; substitution needs the 8-digit
        subheading, which is what §1313(b) pivots on. Direct identity is searched across
        the whole tree before substitution is considered anywhere in it: a route needing
        no commercial-interchangeability narrative beats one that does, regardless of
        which is shallower.
        """
        for wanted in (ManufacturingBasis.DIRECT_IDENTITY, ManufacturingBasis.SUBSTITUTION):
            for path in self.designatable_paths():
                leaf = path[-1]
                if leaf.basis is not wanted:
                    continue
                if wanted is ManufacturingBasis.DIRECT_IDENTITY:
                    if leaf.component_hts.code == hts.code:
                        return path
                elif leaf.component_hts.substitutable_with(hts):
                    return path
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


def consumption_per_finished_unit(path: Sequence[BomComponent]) -> Decimal:
    """Leaf units consumed per **finished** unit, compounded along the whole route.

    Each level contributes `quantity_per_unit / yield_rate`, and a relative-value share at
    any level applies at that level. The product is taken exactly, in Decimal, and is not
    rounded here — rounding is the caller's decision at the point of designation, where
    the direction matters.

    A one-element path is the flat case and returns that component's own consumption,
    which is why the depth-1 behaviour is unchanged.
    """
    total = Decimal("1")
    for node in path:
        total *= node.consumption_per_unit
        if node.relative_value_share is not None:
            total *= node.relative_value_share
    return total


def required_for_path(path: Sequence[BomComponent], finished_quantity: Decimal) -> Decimal:
    """Leaf quantity actually used to produce `finished_quantity` finished units.

    The TFTEA ceiling on designation. Truncated to four decimal places, matching the
    Numeric(18,4) quantity column, and truncated **down**: designating a hair less than
    was used is a smaller claim, while designating a hair more is an overclaim, and only
    one of those is a finding on audit.
    """
    exact = consumption_per_finished_unit(path) * finished_quantity
    return exact.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def path_key(path: Sequence[BomComponent]) -> str:
    """Stable identifier for a route through the tree.

    The same leaf reached through two different subassemblies is two different component
    slots consumed at two different rates, so the designation ceiling must be enforced per
    route rather than per leaf. Joining the codes along the path is what separates them.
    """
    return ">".join(node.component_hts.code for node in path)

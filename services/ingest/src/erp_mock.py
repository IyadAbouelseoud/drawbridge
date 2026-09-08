"""ERP bill-of-materials source, and a mock that stands in for a real one.

Week 6 made `BillOfMaterials` a tree and taught the CP-SAT matcher to designate against
routes rather than leaves. Nothing yet feeds it a real nested bill, because that means an
SAP or Oracle connector and a customer willing to open one. This module fixes the
*contract* so the connector, when it arrives, changes the source and not the shape.

**What an ERP actually returns.** Not a tree. SAP's CS_BOM_EXPL / STPO and Oracle's
BOM_COMPONENTS both return a flat parent-child list: every row names its parent, its
component, a quantity per parent unit, and a scrap or yield figure. The tree is implied by
the parent pointers, and reconstructing it is this module's real work.

Three things that flat list will do to you, all of which `explode` handles rather than
assumes away:

- **Scrap, not yield.** ERPs record component scrap as a percentage — 5% scrap, not 0.95
  yield. The conversion is `yield = 1 - scrap/100`, and getting it backwards understates
  consumption by roughly the scrap figure on every level, compounding with depth.
- **Phantom assemblies.** A phantom is a grouping level that is never stocked or built; it
  exists to organise the bill. It has no imported article behind it, so it must collapse
  into its parent rather than becoming a designation level.
- **Cycles.** A misconfigured bill can list a part as its own ancestor. An unguarded
  recursive explosion follows it until the stack ends.

Nothing here invents a figure. Quantities and scrap come from the source rows; what this
module does is reshape and validate, and refuse the bill when the source is incoherent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from drawbridge_schemas.bom import BillOfMaterials, BomComponent, ManufacturingBasis
from drawbridge_schemas.trade import HTSCode

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# Deterministic BOM ids from (tenant, finished good, revision). A bill re-pulled from the
# ERP must land on the same id, or a claim's stored reference stops resolving.
_BOM_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

# Depth beyond which a bill is treated as malformed rather than merely deep. Real
# manufacturing bills reach five or six levels; twenty means a cycle the parent-pointer
# guard somehow missed, or a bill nobody should be designating against unexamined.
MAX_DEPTH = 20


class ErpError(RuntimeError):
    """The source bill could not be turned into a designatable structure."""


class BomCycleError(ErpError):
    """A component is its own ancestor.

    Fatal rather than truncated. A cycle means the ERP data is wrong, and silently cutting
    the loop would produce a plausible multiplier from an incoherent bill.
    """


@dataclass(frozen=True, slots=True)
class ErpBomRow:
    """One parent-child row, in the shape SAP and Oracle both return.

    `parent_part` is None for a top-level component of the finished good. `scrap_percent`
    is the ERP's own convention — a percentage of input lost, not a yield.
    """

    finished_part: str
    """The ERP part number of the finished good this bill belongs to."""

    component_part: str
    parent_part: str | None
    quantity_per_parent: Decimal
    unit_of_measure: str
    component_hts: str
    description: str
    scrap_percent: Decimal = Decimal("0")
    is_phantom: bool = False
    """A grouping level that is never stocked. Collapses into its parent."""

    is_purchased: bool = True
    """False for an in-house stage. Only purchased parts can have been imported."""

    relative_value_share: Decimal | None = None
    """19 CFR 190 subpart B, where one process yields several products."""

    @property
    def yield_rate(self) -> Decimal:
        """Fraction of input surviving, converted from the ERP's scrap percentage.

        Clamped into (0, 1]. A scrap figure of 100% or more would mean nothing survives,
        which is not a bill of materials; a negative one is a data error. Both are refused
        upstream in `explode` rather than silently clamped here.
        """
        return Decimal("1") - (self.scrap_percent / Decimal("100"))


class ErpBomSource(ABC):
    """Where nested bills come from."""

    @abstractmethod
    def rows_for(self, tenant_id: UUID, finished_part: str) -> Sequence[ErpBomRow]:
        """Every parent-child row of one finished good's bill, at any depth."""

    @abstractmethod
    def finished_parts(self, tenant_id: UUID) -> Sequence[str]:
        """Finished goods this source can supply a bill for."""


def explode(
    rows: Sequence[ErpBomRow],
    *,
    tenant_id: UUID,
    finished_good_hts: str,
    finished_good_description: str,
    finished_unit_of_measure: str,
    revision: str | None = None,
) -> BillOfMaterials:
    """Reconstruct the tree from a flat parent-child list.

    The ERP's ordering is not trusted: rows arrive in whatever order the extract produced,
    so children are indexed by parent first and the tree is built top-down from that index.
    Ordering *within* a level is preserved as given, because the CP-SAT model is built from
    the walk and a reordered model can settle on a different optimum among equally-valued
    allocations.

    Phantoms collapse. A phantom's children reattach to the phantom's parent with their
    quantities multiplied through the phantom's own — the phantom was never built, so its
    quantity is a multiplier on the level below rather than a stage of its own.
    """
    if not rows:
        msg = "ERP returned no rows for this bill"
        raise ErpError(msg)

    _validate(rows)

    by_parent: dict[str | None, list[ErpBomRow]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_part, []).append(row)

    top = by_parent.get(None, [])
    if not top:
        msg = (
            "no top-level rows: every row names a parent, so the bill has no attachment "
            "point to the finished good"
        )
        raise ErpError(msg)

    components = tuple(
        _build(row, by_parent, ancestors=(), depth=1) for row in _collapse(top, by_parent)
    )

    return BillOfMaterials(
        bom_id=uuid5(_BOM_NAMESPACE, f"{tenant_id}:{rows[0].finished_part}:{revision or ''}"),
        tenant_id=tenant_id,
        finished_good_hts=HTSCode(code=finished_good_hts),
        finished_good_description=finished_good_description,
        finished_unit_of_measure=finished_unit_of_measure,
        components=components,
        effective_from=revision,
    )


def _validate(rows: Sequence[ErpBomRow]) -> None:
    """Refuse a bill the arithmetic cannot survive.

    Each check names what the ERP got wrong, because the fix is in the source system and
    whoever reads this error is going to have to go and find it there.
    """
    parts = {row.finished_part for row in rows}
    if len(parts) > 1:
        msg = f"rows span more than one finished good: {sorted(parts)}"
        raise ErpError(msg)

    for row in rows:
        if row.quantity_per_parent <= 0:
            msg = (
                f"component {row.component_part} has quantity {row.quantity_per_parent}; "
                "a component consumed in non-positive quantity is not a component"
            )
            raise ErpError(msg)
        if not (Decimal("0") <= row.scrap_percent < Decimal("100")):
            msg = (
                f"component {row.component_part} has scrap {row.scrap_percent}%; "
                "scrap must be in [0, 100) — at 100% nothing survives the process"
            )
            raise ErpError(msg)


def _collapse(
    level: Sequence[ErpBomRow], by_parent: dict[str | None, list[ErpBomRow]]
) -> list[ErpBomRow]:
    """Replace phantoms at this level with their children, quantities multiplied through.

    A phantom is a grouping row the ERP never builds or stocks. Left in place it would
    become a designation level with no imported article behind it, which is exactly what
    week 6's leaf rule refuses — so it is removed here, at ingest, rather than producing a
    tree the matcher has to work around.
    """
    out: list[ErpBomRow] = []
    for row in level:
        if not row.is_phantom:
            out.append(row)
            continue

        children = by_parent.get(row.component_part, [])
        if not children:
            msg = (
                f"phantom {row.component_part} has no children; a phantom with nothing "
                "beneath it contributes no component and cannot be designated against"
            )
            raise ErpError(msg)

        for child in _collapse(children, by_parent):
            out.append(
                ErpBomRow(
                    finished_part=child.finished_part,
                    component_part=child.component_part,
                    parent_part=row.parent_part,
                    # The phantom's own quantity and yield become a multiplier on the
                    # level below, since the phantom itself is never made.
                    quantity_per_parent=(
                        child.quantity_per_parent * row.quantity_per_parent / row.yield_rate
                    ),
                    unit_of_measure=child.unit_of_measure,
                    component_hts=child.component_hts,
                    description=child.description,
                    scrap_percent=child.scrap_percent,
                    is_phantom=False,
                    is_purchased=child.is_purchased,
                    relative_value_share=child.relative_value_share,
                )
            )
    return out


def _build(
    row: ErpBomRow,
    by_parent: dict[str | None, list[ErpBomRow]],
    *,
    ancestors: tuple[str, ...],
    depth: int,
) -> BomComponent:
    """One node and everything beneath it."""
    if row.component_part in ancestors:
        chain = " > ".join([*ancestors, row.component_part])
        msg = f"component {row.component_part} is its own ancestor: {chain}"
        raise BomCycleError(msg)
    if depth > MAX_DEPTH:
        msg = f"bill exceeds {MAX_DEPTH} levels at {row.component_part}; refusing to explode"
        raise ErpError(msg)

    children = _collapse(by_parent.get(row.component_part, []), by_parent)
    sub_components = tuple(
        _build(child, by_parent, ancestors=(*ancestors, row.component_part), depth=depth + 1)
        for child in children
    )

    return BomComponent(
        component_hts=HTSCode(code=row.component_hts),
        description=row.description,
        quantity_per_unit=row.quantity_per_parent,
        unit_of_measure=row.unit_of_measure,
        yield_rate=row.yield_rate,
        # Direct identity where the ERP says the part was purchased under that exact
        # tariff code; substitution otherwise. An in-house stage is neither — it is not a
        # leaf and is never designated against, so its basis is inert.
        basis=(
            ManufacturingBasis.DIRECT_IDENTITY
            if row.is_purchased and not sub_components
            else ManufacturingBasis.SUBSTITUTION
        ),
        relative_value_share=row.relative_value_share,
        sub_components=sub_components,
    )


# ------------------------------------------------------------------------------- mock


@dataclass
class MockErpSource(ErpBomSource):
    """A stand-in ERP holding hand-written bills.

    Shaped like a real extract, not like a convenient fixture: rows are flat and unordered,
    quantities are per-parent, loss is expressed as scrap percentage, and one bill carries
    a phantom level so the collapse path is exercised by the default data rather than only
    by a test that remembers to.

    Bills are keyed by `(tenant, part)`. A real connector is tenant-scoped — two customers
    can hold the same part number for different articles — and a mock that ignored tenancy
    would pass every test and then leak one customer's bill into another's claim the day a
    real connector replaced it.
    """

    bills: dict[tuple[UUID, str], list[ErpBomRow]] = field(default_factory=dict)

    def rows_for(self, tenant_id: UUID, finished_part: str) -> Sequence[ErpBomRow]:
        try:
            return self.bills[(tenant_id, finished_part)]
        except KeyError:
            held = list(self.finished_parts(tenant_id))
            msg = (
                f"no bill for part {finished_part!r} under tenant {tenant_id}; "
                f"this source holds {held} for that tenant"
            )
            raise ErpError(msg) from None

    def finished_parts(self, tenant_id: UUID) -> Sequence[str]:
        return sorted(part for tenant, part in self.bills if tenant == tenant_id)

    def load(self, rows: Iterable[dict[str, Any]], *, tenant_id: UUID) -> None:
        """Ingest rows in the shape an ERP extract delivers them: dicts, unordered."""
        for raw in rows:
            row = ErpBomRow(
                finished_part=str(raw["finished_part"]),
                component_part=str(raw["component_part"]),
                parent_part=(
                    str(raw["parent_part"]) if raw.get("parent_part") not in (None, "") else None
                ),
                quantity_per_parent=Decimal(str(raw["quantity_per_parent"])),
                unit_of_measure=str(raw["unit_of_measure"]),
                component_hts=str(raw["component_hts"]),
                description=str(raw["description"]),
                scrap_percent=Decimal(str(raw.get("scrap_percent", "0"))),
                is_phantom=bool(raw.get("is_phantom", False)),
                is_purchased=bool(raw.get("is_purchased", True)),
                relative_value_share=(
                    Decimal(str(raw["relative_value_share"]))
                    if raw.get("relative_value_share") is not None
                    else None
                ),
            )
            self.bills.setdefault((tenant_id, row.finished_part), []).append(row)


# A four-level industrial bill: a finished pump assembly, a drive subassembly, a housing
# stage, and imported steel billet at the bottom, plus a phantom fastener kit that must
# collapse. Rows are deliberately in extract order — parents and children interleaved.
SAMPLE_BILL: list[dict[str, Any]] = [
    {
        "finished_part": "PUMP-8000",
        "component_part": "DRIVE-ASSY",
        "parent_part": None,
        "quantity_per_parent": "1",
        "unit_of_measure": "PCE",
        "component_hts": "8483409000",
        "description": "Gear drive subassembly, manufactured in house",
        "scrap_percent": "10",
        "is_purchased": False,
    },
    {
        "finished_part": "PUMP-8000",
        "component_part": "STEEL-BILLET",
        "parent_part": "HOUSING-STG",
        "quantity_per_parent": "3",
        "unit_of_measure": "KGM",
        "component_hts": "7207200000",
        "description": "Semi-finished steel billet, imported",
        "scrap_percent": "50",
    },
    {
        "finished_part": "PUMP-8000",
        "component_part": "FASTENER-KIT",
        "parent_part": None,
        "quantity_per_parent": "1",
        "unit_of_measure": "KIT",
        "component_hts": "7318159000",
        "description": "Phantom fastener kit, never stocked",
        "is_phantom": True,
    },
    {
        "finished_part": "PUMP-8000",
        "component_part": "HOUSING-STG",
        "parent_part": "DRIVE-ASSY",
        "quantity_per_parent": "2",
        "unit_of_measure": "PCE",
        "component_hts": "8483900000",
        "description": "Machined housing, manufactured in house",
        "scrap_percent": "25",
        "is_purchased": False,
    },
    {
        "finished_part": "PUMP-8000",
        "component_part": "BOLT-M12",
        "parent_part": "FASTENER-KIT",
        "quantity_per_parent": "8",
        "unit_of_measure": "PCE",
        "component_hts": "7318159000",
        "description": "M12 hex bolt, imported",
        "scrap_percent": "0",
    },
]


def sample_source(tenant_id: UUID) -> MockErpSource:
    """A `MockErpSource` holding `SAMPLE_BILL` for one tenant."""
    source = MockErpSource()
    source.load(SAMPLE_BILL, tenant_id=tenant_id)
    return source


def fetch(
    source: ErpBomSource,
    *,
    tenant_id: UUID,
    finished_part: str,
    finished_good_hts: str,
    finished_good_description: str,
    finished_unit_of_measure: str = "PCE",
    revision: str | None = None,
) -> BillOfMaterials:
    """Pull one bill and explode it into the schema the matcher consumes."""
    return explode(
        source.rows_for(tenant_id, finished_part),
        tenant_id=tenant_id,
        finished_good_hts=finished_good_hts,
        finished_good_description=finished_good_description,
        finished_unit_of_measure=finished_unit_of_measure,
        revision=revision,
    )

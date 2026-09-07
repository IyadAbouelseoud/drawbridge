"""Known-answer fixtures for recursive bill-of-materials explosion.

The arithmetic is stated by hand in each test and must reproduce exactly. A BOM multiplier
that is wrong by a percent is a designation that is wrong by a percent, and TFTEA ties the
designated quantity to the quantity *actually used* — over-designating is an overclaim CBP
finds on audit, under-designating leaves money unrecovered and never surfaces at all.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from drawbridge_schemas.bom import (
    BillOfMaterials,
    BomComponent,
    ManufacturingBasis,
    consumption_per_finished_unit,
    path_key,
    required_for_path,
)
from drawbridge_schemas.trade import HTSCode

pytestmark = pytest.mark.golden


def component(
    code: str,
    *,
    quantity: str,
    yield_rate: str = "1",
    share: str | None = None,
    basis: ManufacturingBasis = ManufacturingBasis.SUBSTITUTION,
    children: tuple[BomComponent, ...] = (),
) -> BomComponent:
    return BomComponent(
        component_hts=HTSCode(code=code),
        description=f"component {code}",
        quantity_per_unit=Decimal(quantity),
        unit_of_measure="PCE",
        yield_rate=Decimal(yield_rate),
        relative_value_share=Decimal(share) if share else None,
        basis=basis,
        sub_components=children,
    )


def bom(*components: BomComponent) -> BillOfMaterials:
    return BillOfMaterials(
        bom_id=uuid4(),
        tenant_id=uuid4(),
        finished_good_hts=HTSCode(code="8479899900"),
        finished_good_description="finished assembly",
        finished_unit_of_measure="PCE",
        components=components,
    )


class TestFlatBomIsUnchanged:
    """Depth 1 must behave exactly as it did before nesting existed.

    The nesting change is worthless if it quietly moves the numbers on the single-level
    claims already in the system.
    """

    def test_single_level_multiplier(self) -> None:
        leaf = component("7326908688", quantity="4", yield_rate="0.8")
        tree = bom(leaf)
        path = tree.path_for(HTSCode(code="7326908688"))
        assert path is not None
        assert len(path) == 1
        # 4 embodied / 0.8 yield = 5 consumed per finished unit.
        assert consumption_per_finished_unit(path) == Decimal("5")
        assert required_for_path(path, Decimal("250")) == Decimal("1250.0000")

    def test_depth_is_one(self) -> None:
        assert bom(component("7326908688", quantity="1")).depth == 1

    def test_component_for_agrees_with_path_for(self) -> None:
        leaf = component("7326908688", quantity="4", yield_rate="0.8")
        tree = bom(leaf)
        found = tree.component_for(HTSCode(code="7326908688"))
        assert found is leaf
        assert found.required_for(Decimal("250")) == Decimal("1250")


class TestTwoLevelNesting:
    """One subassembly above one imported component."""

    @pytest.fixture
    def tree(self) -> BillOfMaterials:
        casting = component("7325990000", quantity="2", yield_rate="0.8")
        gearbox = component("8483409000", quantity="1", yield_rate="0.9", children=(casting,))
        return bom(gearbox)

    def test_yield_compounds_down_the_route(self, tree: BillOfMaterials) -> None:
        path = tree.path_for(HTSCode(code="7325990000"))
        assert path is not None
        # Per finished unit: 1 gearbox / 0.9 = 1.1111 gearboxes, each needing
        # 2 castings / 0.8 = 2.5 castings. 1.1111... x 2.5 = 2.7777...
        assert consumption_per_finished_unit(path) == pytest.approx(
            Decimal("2.777777777777777777777777778")
        )

    def test_designation_ceiling_truncates_down(self, tree: BillOfMaterials) -> None:
        path = tree.path_for(HTSCode(code="7325990000"))
        assert path is not None
        # 2.7777... x 100 = 277.7777... Truncated down: designating a hair less than was
        # used is a smaller claim; a hair more is an overclaim.
        assert required_for_path(path, Decimal("100")) == Decimal("277.7777")

    def test_the_subassembly_itself_is_not_designatable(self, tree: BillOfMaterials) -> None:
        """A gearbox made in-house was never imported, so nothing may be designated to it."""
        assert tree.path_for(HTSCode(code="8483409000")) is None
        assert [path_key(p) for p in tree.designatable_paths()] == ["8483409000>7325990000"]

    def test_depth(self, tree: BillOfMaterials) -> None:
        assert tree.depth == 2


class TestThreeLevelNesting:
    """Three stages, each losing material. The compounding has to survive the depth."""

    @pytest.fixture
    def tree(self) -> BillOfMaterials:
        billet = component("7207200000", quantity="3", yield_rate="0.5")
        housing = component("8483900000", quantity="2", yield_rate="0.75", children=(billet,))
        drive = component("8483409000", quantity="1", yield_rate="0.9", children=(housing,))
        return bom(drive)

    def test_multiplier(self, tree: BillOfMaterials) -> None:
        path = tree.path_for(HTSCode(code="7207200000"))
        assert path is not None
        assert path_key(path) == "8483409000>8483900000>7207200000"
        # (1/0.9) x (2/0.75) x (3/0.5) = 1.1111... x 2.6666... x 6 = 17.7777...
        assert required_for_path(path, Decimal("1")) == Decimal("17.7777")
        assert required_for_path(path, Decimal("90")) == Decimal("1600.0000")

    def test_depth(self, tree: BillOfMaterials) -> None:
        assert tree.depth == 3


class TestSharedLeafAcrossRoutes:
    """The same imported part reached through two different subassemblies.

    This is the case a flattened BOM cannot express and the case where keying a designation
    ceiling on the leaf rather than the route silently over-designates.
    """

    @pytest.fixture
    def tree(self) -> BillOfMaterials:
        # Two distinct fastener nodes with the same HTS, reached through two subassemblies
        # consuming them at different rates.
        left = component(
            "8483409000",
            quantity="1",
            yield_rate="0.9",
            children=(component("7318159000", quantity="4", yield_rate="1"),),
        )
        right = component(
            "8501310000",
            quantity="2",
            yield_rate="1",
            children=(component("7318159000", quantity="6", yield_rate="0.75"),),
        )
        return bom(left, right)

    def test_both_routes_are_designatable_and_distinct(self, tree: BillOfMaterials) -> None:
        keys = [path_key(p) for p in tree.designatable_paths()]
        assert keys == ["8483409000>7318159000", "8501310000>7318159000"]
        assert len(set(keys)) == 2

    def test_the_two_routes_consume_at_different_rates(self, tree: BillOfMaterials) -> None:
        routes = {path_key(p): consumption_per_finished_unit(p) for p in tree.designatable_paths()}
        # 1/0.9 x 4 = 4.4444...
        assert routes["8483409000>7318159000"] == pytest.approx(Decimal("4.444444444444444"))
        # 2 x 6/0.75 = 16
        assert routes["8501310000>7318159000"] == Decimal("16")


class TestRelativeValueShare:
    """19 CFR 190 subpart B apportionment, applied at the level it is declared on."""

    def test_share_scales_the_route(self) -> None:
        leaf = component("2905110000", quantity="10", yield_rate="1", share="0.6")
        stage = component("2905190000", quantity="1", yield_rate="1", children=(leaf,))
        tree = bom(stage)
        path = tree.path_for(HTSCode(code="2905110000"))
        assert path is not None
        assert consumption_per_finished_unit(path) == Decimal("6")

    def test_joint_product_shares_cannot_exceed_the_whole(self) -> None:
        with pytest.raises(ValueError, match="exceeding 1"):
            component(
                "2905190000",
                quantity="1",
                children=(
                    component("2905110000", quantity="1", share="0.7"),
                    component("2905120000", quantity="1", share="0.5"),
                ),
            )


class TestBasisPreference:
    """Direct identity beats substitution anywhere in the tree, regardless of depth."""

    def test_a_deeper_direct_identity_route_wins_over_a_shallow_substitution_one(self) -> None:
        shallow = component("8471300100", quantity="1", basis=ManufacturingBasis.SUBSTITUTION)
        deep = component(
            "8473301180",
            quantity="1",
            children=(
                component("8471300150", quantity="2", basis=ManufacturingBasis.DIRECT_IDENTITY),
            ),
        )
        tree = bom(shallow, deep)
        # 8471300150 shares the 8-digit key 84713001 with the shallow node, so both routes
        # are reachable. Direct identity needs no interchangeability narrative and wins.
        path = tree.path_for(HTSCode(code="8471300150"))
        assert path is not None
        assert path_key(path) == "8473301180>8471300150"


class TestStructuralGuards:
    def test_duplicate_sub_component_codes_are_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate sub-component"):
            component(
                "8483409000",
                quantity="1",
                children=(
                    component("7318159000", quantity="1"),
                    component("7318159000", quantity="2"),
                ),
            )

    def test_walk_is_deterministic(self) -> None:
        tree = bom(
            component(
                "8483409000",
                quantity="1",
                children=(component("7318159000", quantity="1"),),
            ),
            component("8501310000", quantity="1"),
        )
        first = [path_key(p) for p in tree.walk()]
        second = [path_key(p) for p in tree.walk()]
        assert first == second
        assert first == ["8483409000", "8483409000>7318159000", "8501310000"]

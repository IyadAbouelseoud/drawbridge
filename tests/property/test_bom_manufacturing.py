"""Manufacturing drawback — BOM explosion under 19 U.S.C. §1313(a)/(b).

The property that matters: under §1313(j) allocating *q* import units satisfies *q*
export units, but under manufacturing the units do not cancel. Exporting one finished
good consumes `quantity_per_unit / yield` of each component, and the designated quantity
must never exceed the quantity actually used — TFTEA ties designation to actual use, so
over-designation is a false claim and under-designation forfeits refund.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_DOWN, Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from drawbridge_schemas.bom import BillOfMaterials, BomComponent, ManufacturingBasis
from drawbridge_schemas.jurisdiction import US_PROFILE, Currency, Jurisdiction, MatchTheory
from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Provenance,
    Span,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode
from services.matcher.src.base import MatchRequest, SolverStatus
from services.matcher.src.us_substitution import UsSubstitutionMatcher

TENANT = UUID("00000000-0000-0000-0000-0000000000a1")
IMPORT_DATE = date(2023, 1, 10)
EXPORT_DATE = date(2023, 9, 15)
AS_OF = date(2024, 6, 1)

# Finished good and two distinct raw-material components.
FINISHED = "94036080"
STEEL = "72104900"
RESIN = "39074000"


def _provenance() -> Provenance:
    ref = DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.CBP_7501,
        sha256="c" * 64,
        object_key="tenants/a1/7501/m.pdf",
    )
    return Provenance(
        spans=(Span(document=ref, page=1, bbox=(0.0, 0.0, 10.0, 10.0)),),
        confidence=Confidence(score=0.99, method="pdfplumber-native"),
    )


def _material(hts: str, quantity: Decimal, duty: Decimal) -> EntryLine:
    return EntryLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        declaration_number=f"MFG-{uuid4().hex[:7].upper()}-1",
        line_number=1,
        import_date=IMPORT_DATE,
        declaration_date=IMPORT_DATE,
        port_of_entry="2704",
        country_of_origin="CN",
        hts=HTSCode(code=hts),
        description="raw material",
        quantity=quantity,
        unit_of_measure="KG",
        entered_value=Decimal("50000.00"),
        duty_paid=duty,
        provenance=_provenance(),
    )


def _finished_export(quantity: Decimal, hts: str = FINISHED) -> ExportLine:
    return ExportLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.US,
        reference=f"BOL{uuid4().hex[:8].upper()}",
        line_number=1,
        export_date=EXPORT_DATE,
        destination_country="DE",
        hts=HTSCode(code=hts),
        description="finished good",
        quantity=quantity,
        unit_of_measure="NO",
        provenance=_provenance(),
    )


def _bom(*components: BomComponent, finished: str = FINISHED) -> BillOfMaterials:
    return BillOfMaterials(
        bom_id=uuid4(),
        tenant_id=TENANT,
        finished_good_hts=HTSCode(code=finished),
        finished_good_description="finished good",
        finished_unit_of_measure="NO",
        components=components,
    )


def _component(
    hts: str,
    per_unit: Decimal,
    yield_rate: Decimal = Decimal("1"),
    basis: ManufacturingBasis = ManufacturingBasis.SUBSTITUTION,
    share: Decimal | None = None,
) -> BomComponent:
    return BomComponent(
        component_hts=HTSCode(code=hts),
        description="component",
        quantity_per_unit=per_unit,
        unit_of_measure="KG",
        yield_rate=yield_rate,
        basis=basis,
        relative_value_share=share,
    )


def _run(imports, exports, boms=None):
    return UsSubstitutionMatcher().match(
        MatchRequest(
            imports=imports,
            exports=exports,
            profile=US_PROFILE,
            as_of=AS_OF,
            boms=boms or {},
        )
    )


class TestBomArithmetic:
    """The multiplier itself, before any solver is involved."""

    def test_consumption_is_embodied_quantity_over_yield(self) -> None:
        # 2 kg embodied per unit at 80% yield means 2.5 kg consumed.
        component = _component(STEEL, Decimal("2"), Decimal("0.8"))
        assert component.consumption_per_unit == Decimal("2.5")

    def test_full_yield_leaves_the_multiplier_alone(self) -> None:
        component = _component(STEEL, Decimal("3"))
        assert component.consumption_per_unit == Decimal("3")
        assert component.required_for(Decimal("100")) == Decimal("300")

    def test_scrap_increases_the_designatable_quantity(self) -> None:
        """Claiming only the embodied quantity under-claims the refund."""
        lossy = _component(STEEL, Decimal("2"), Decimal("0.8"))
        lossless = _component(STEEL, Decimal("2"))
        assert lossy.required_for(Decimal("100")) > lossless.required_for(Decimal("100"))
        assert lossy.required_for(Decimal("100")) == Decimal("250")

    def test_relative_value_share_apportions_joint_products(self) -> None:
        # 19 CFR 190 subpart B — distribute by value at the time of separation.
        component = _component(STEEL, Decimal("4"), share=Decimal("0.6"))
        assert component.required_for(Decimal("100")) == Decimal("240")

    def test_value_shares_over_one_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="double-count"):
            _bom(
                _component(STEEL, Decimal("1"), share=Decimal("0.7")),
                _component(RESIN, Decimal("1"), share=Decimal("0.6")),
            )

    def test_duplicate_components_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate component"):
            _bom(_component(STEEL, Decimal("1")), _component(STEEL, Decimal("2")))

    def test_zero_yield_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _component(STEEL, Decimal("1"), Decimal("0"))


class TestBomAllocation:
    """Worked cases with hand-computed answers."""

    def test_allocates_by_multiplier_not_one_to_one(self) -> None:
        """100 finished units, 3 kg of steel each, so 300 kg is designatable.

        Duty is 3,000.00 on 1,000 kg, i.e. 3.00/kg. Designating 300 kg recovers 900.00.
        A §1313(j)-style 1:1 allocation would have designated 100 kg and recovered 300.00.
        """
        steel = _material(STEEL, Decimal("1000"), Decimal("3000.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3")))

        result = _run([steel], [export], {FINISHED: bom})

        assert result.status is SolverStatus.OPTIMAL
        assert len(result.matches) == 1
        assert result.matches[0].quantity == Decimal("300")
        assert result.total_duty_allocated == Decimal("900.00")
        assert result.matches[0].theory is MatchTheory.MANUFACTURING_SUBSTITUTION

    def test_yield_loss_increases_the_allocation(self) -> None:
        """3 kg embodied at 75% yield is 4 kg consumed; 100 units means 400 kg."""
        steel = _material(STEEL, Decimal("1000"), Decimal("3000.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3"), Decimal("0.75")))

        result = _run([steel], [export], {FINISHED: bom})

        assert result.matches[0].quantity == Decimal("400")
        assert result.total_duty_allocated == Decimal("1200.00")

    def test_multi_component_bom_draws_from_several_import_pools(self) -> None:
        """One finished good, two components, two separate raw-material imports.

        50 units x 2 kg steel  = 100 kg at 5.00/kg = 500.00
        50 units x 4 kg resin  = 200 kg at 2.00/kg = 400.00
                                                     ------
                                                     900.00
        """
        steel = _material(STEEL, Decimal("500"), Decimal("2500.00"))  # 5.00/kg
        resin = _material(RESIN, Decimal("500"), Decimal("1000.00"))  # 2.00/kg
        export = _finished_export(Decimal("50"))
        bom = _bom(
            _component(STEEL, Decimal("2")),
            _component(RESIN, Decimal("4")),
        )

        result = _run([steel, resin], [export], {FINISHED: bom})

        assert result.status is SolverStatus.OPTIMAL
        assert len(result.matches) == 2
        by_import = {str(m.import_line_id): m.quantity for m in result.matches}
        assert by_import[str(steel.line_id)] == Decimal("100")
        assert by_import[str(resin.line_id)] == Decimal("200")
        assert result.total_duty_allocated == Decimal("900.00")

    def test_designation_never_exceeds_quantity_actually_used(self) -> None:
        """TFTEA ties the designated quantity to the quantity actually used.

        10,000 kg of steel on hand, but only 100 finished units exported at 3 kg each.
        Only 300 kg was used; designating more would be a false claim.
        """
        steel = _material(STEEL, Decimal("10000"), Decimal("30000.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3")))

        result = _run([steel], [export], {FINISHED: bom})

        assert result.matches[0].quantity == Decimal("300")
        assert result.matches[0].quantity < steel.quantity_available

    def test_insufficient_material_caps_at_what_was_imported(self) -> None:
        """200 kg imported, 300 kg required. Only what exists can be designated."""
        steel = _material(STEEL, Decimal("200"), Decimal("600.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3")))

        result = _run([steel], [export], {FINISHED: bom})

        assert result.matches[0].quantity == Decimal("200")
        assert result.total_duty_allocated == Decimal("600.00")

    def test_prefers_the_dearer_lot_of_the_same_component(self) -> None:
        """Two steel lots, only 300 kg designatable. The solver must take the dear one."""
        cheap = _material(STEEL, Decimal("500"), Decimal("500.00"))  # 1.00/kg
        dear = _material(STEEL, Decimal("500"), Decimal("5000.00"))  # 10.00/kg
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3")))

        result = _run([cheap, dear], [export], {FINISHED: bom})

        assert result.total_duty_allocated == Decimal("3000.00")
        assert len(result.matches) == 1
        assert result.matches[0].import_line_id == dear.line_id

    def test_direct_identity_basis_requires_the_full_code(self) -> None:
        """§1313(a) is the same merchandise; a different suffix is not it."""
        steel = _material("72104910", Decimal("1000"), Decimal("3000.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3"), basis=ManufacturingBasis.DIRECT_IDENTITY))

        result = _run([steel], [export], {FINISHED: bom})
        assert result.matches == ()

    def test_direct_identity_basis_matches_on_exact_code(self) -> None:
        steel = _material(STEEL, Decimal("1000"), Decimal("3000.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3"), basis=ManufacturingBasis.DIRECT_IDENTITY))

        result = _run([steel], [export], {FINISHED: bom})
        assert result.matches[0].theory is MatchTheory.MANUFACTURING_DIRECT_IDENTITY

    def test_material_not_in_the_bom_does_not_pair(self) -> None:
        unrelated = _material("85176201", Decimal("1000"), Decimal("3000.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3")))

        result = _run([unrelated], [export], {FINISHED: bom})
        assert result.status is SolverStatus.NO_CANDIDATES

    def test_without_a_bom_raw_material_cannot_reach_a_finished_good(self) -> None:
        """The BOM is what makes the pairing lawful. Absent one, there is no theory."""
        steel = _material(STEEL, Decimal("1000"), Decimal("3000.00"))
        export = _finished_export(Decimal("100"))

        result = _run([steel], [export], boms=None)
        assert result.status is SolverStatus.NO_CANDIDATES

    def test_bom_resolves_on_the_eight_digit_subheading(self) -> None:
        """A BOM recorded against a subheading covers its statistical suffixes."""
        steel = _material(STEEL, Decimal("1000"), Decimal("3000.00"))
        export = _finished_export(Decimal("100"), hts="94036080")
        bom = _bom(_component(STEEL, Decimal("3")))

        result = _run([steel], [export], {"94036080": bom})
        assert len(result.matches) == 1

    def test_unused_merchandise_is_unaffected_by_a_bom_on_file(self) -> None:
        """§1313(j) still applies where the exported article is the imported article."""
        widget = _material(FINISHED, Decimal("500"), Decimal("1000.00"))
        export = _finished_export(Decimal("100"))
        bom = _bom(_component(STEEL, Decimal("3")))

        result = _run([widget], [export], {FINISHED: bom})

        assert result.matches[0].theory is MatchTheory.DIRECT_IDENTITY
        assert result.matches[0].quantity == Decimal("100")


class TestBomProperties:
    matcher = UsSubstitutionMatcher()

    @settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        finished_qty=st.integers(min_value=1, max_value=200),
        per_unit=st.integers(min_value=1, max_value=10),
        yield_pct=st.integers(min_value=50, max_value=100),
        material_qty=st.integers(min_value=1, max_value=5000),
        duty_cents=st.integers(min_value=100, max_value=5_000_00),
    )
    def test_allocation_never_exceeds_quantity_used(
        self,
        finished_qty: int,
        per_unit: int,
        yield_pct: int,
        material_qty: int,
        duty_cents: int,
    ) -> None:
        """The TFTEA ceiling holds for every multiplier and yield.

        Over-designating is a false claim, so this is the constraint that must never
        bend regardless of how the numbers fall.
        """
        yield_rate = Decimal(yield_pct) / 100
        component = _component(STEEL, Decimal(per_unit), yield_rate)
        required = component.required_for(Decimal(finished_qty))

        steel = _material(STEEL, Decimal(material_qty), Decimal(duty_cents) / 100)
        export = _finished_export(Decimal(finished_qty))

        result = _run([steel], [export], {FINISHED: _bom(component)})

        allocated = sum((m.quantity for m in result.matches), Decimal("0"))
        assert allocated <= required
        assert allocated <= steel.quantity_available

        # And it takes everything up to the ceiling — leaving refund on the table is the
        # other failure mode. The ceiling is floored to the model's four-decimal
        # precision rather than rounded: a yield of 92% makes the required quantity
        # non-terminating, and rounding up would designate marginally more than was
        # actually used.
        ceiling = min(required, steel.quantity_available).quantize(
            Decimal("0.0001"), rounding=ROUND_DOWN
        )
        assert allocated == ceiling

    @settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        finished_qty=st.integers(min_value=1, max_value=100),
        per_unit=st.integers(min_value=1, max_value=8),
    )
    def test_manufacturing_theories_are_permitted_and_never_gcc(
        self, finished_qty: int, per_unit: int
    ) -> None:
        steel = _material(STEEL, Decimal("100000"), Decimal("10000.00"))
        export = _finished_export(Decimal(finished_qty))
        bom = _bom(_component(STEEL, Decimal(per_unit)))

        result = _run([steel], [export], {FINISHED: bom})

        for match in result.matches:
            assert US_PROFILE.permits(match.theory)
            assert match.theory is not MatchTheory.DECLARATION_LINKAGE

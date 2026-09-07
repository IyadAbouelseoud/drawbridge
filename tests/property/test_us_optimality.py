"""Property-based proof that the CP-SAT allocation is the mathematical maximum.

The claim under test is narrow and checkable: for any pool of import and export lines,
the total refundable duty the solver allocates equals the maximum achievable under the
capacity constraints. Proved two ways —

  1. against a brute-force optimum computed independently on small pools, and
  2. against the LP upper bound, which no feasible allocation can exceed.

A solver that quietly returns a good-but-not-optimal allocation forfeits real money on
every claim, and would be invisible without this.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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

# Two 8-digit substitution families. Lines sharing a family may substitute for each
# other; lines across families may not.
FAMILIES = ("84713001", "85176201")


def _provenance() -> Provenance:
    ref = DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.CBP_7501,
        sha256="a" * 64,
        object_key="tenants/a1/7501/p.pdf",
    )
    return Provenance(
        spans=(Span(document=ref, page=1, bbox=(0.0, 0.0, 10.0, 10.0)),),
        confidence=Confidence(score=0.99, method="pdfplumber-native"),
    )


def _entry(family: str, quantity: int, duty: Decimal, suffix: str = "00") -> EntryLine:
    return EntryLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        declaration_number=f"ABC-{quantity:07d}-1",
        line_number=1,
        import_date=IMPORT_DATE,
        declaration_date=IMPORT_DATE,
        port_of_entry="2704",
        country_of_origin="CN",
        hts=HTSCode(code=family + suffix),
        description="test article",
        quantity=Decimal(quantity),
        unit_of_measure="NO",
        entered_value=Decimal("100000.00"),
        duty_paid=duty,
        provenance=_provenance(),
    )


def _export(family: str, quantity: int, suffix: str = "50") -> ExportLine:
    return ExportLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.US,
        reference=f"BOL{uuid4().hex[:8].upper()}",
        line_number=1,
        export_date=EXPORT_DATE,
        destination_country="DE",
        hts=HTSCode(code=family + suffix),
        description="test article, re-exported",
        quantity=Decimal(quantity),
        unit_of_measure="NO",
        provenance=_provenance(),
    )


def _brute_force_maximum(imports: list[EntryLine], exports: list[ExportLine]) -> Decimal:
    """Independent optimum, computed without CP-SAT.

    Every eligible pairing is a transportable unit of quantity priced at the import
    line's per-unit duty. With no per-pair capacity beyond the two line capacities, the
    optimum is a greedy fill in descending duty order — the transportation polytope is
    integral, so greedy on a single price dimension is exact here.

    This is deliberately a different algorithm from the one under test. Reimplementing
    CP-SAT's reasoning would prove nothing.
    """
    remaining_import = {str(i.line_id): i.quantity_available for i in imports}
    remaining_export = {str(e.line_id): e.quantity_available for e in exports}

    pairs = []
    for entry in imports:
        per_unit = entry.recoverable_base(US_PROFILE) / entry.quantity
        for export in exports:
            if entry.hts.substitutable_with(export.hts):
                pairs.append((per_unit, str(entry.line_id), str(export.line_id)))

    # Descending duty; ties broken by id so the reference is deterministic too.
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    total = Decimal("0")
    for per_unit, import_id, export_id in pairs:
        take = min(remaining_import[import_id], remaining_export[export_id])
        if take <= 0:
            continue
        remaining_import[import_id] -= take
        remaining_export[export_id] -= take
        total += per_unit * take
    return total


def _lp_upper_bound(imports: list[EntryLine], exports: list[ExportLine]) -> Decimal:
    """Loose bound no feasible allocation can beat.

    Total exportable quantity, all of it priced at the single most valuable import line.
    """
    if not imports or not exports:
        return Decimal("0")
    best_rate = max(i.recoverable_base(US_PROFILE) / i.quantity for i in imports)
    export_capacity = sum(e.quantity_available for e in exports)
    import_capacity = sum(i.quantity_available for i in imports)
    return best_rate * min(export_capacity, import_capacity)


quantities = st.integers(min_value=1, max_value=500)
duties = st.integers(min_value=1, max_value=50_000).map(lambda cents: Decimal(cents) / 100)
families = st.sampled_from(FAMILIES)


@st.composite
def _pools(draw: st.DrawFn) -> tuple[list[EntryLine], list[ExportLine]]:
    n_imports = draw(st.integers(min_value=1, max_value=4))
    n_exports = draw(st.integers(min_value=1, max_value=4))
    imports = [_entry(draw(families), draw(quantities), draw(duties)) for _ in range(n_imports)]
    exports = [_export(draw(families), draw(quantities)) for _ in range(n_exports)]
    return imports, exports


class TestCpSatOptimality:
    matcher = UsSubstitutionMatcher()

    def _run(self, imports: list[EntryLine], exports: list[ExportLine]):
        return self.matcher.match(
            MatchRequest(
                imports=imports,
                exports=exports,
                profile=US_PROFILE,
                as_of=AS_OF,
                time_limit_seconds=10.0,
            )
        )

    @settings(
        max_examples=120,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(_pools())
    def test_allocation_equals_brute_force_optimum(
        self, pools: tuple[list[EntryLine], list[ExportLine]]
    ) -> None:
        imports, exports = pools
        result = self._run(imports, exports)

        expected = _brute_force_maximum(imports, exports)
        if expected == 0:
            assert result.total_duty_allocated == Decimal("0.00")
            return

        assert result.status is SolverStatus.OPTIMAL
        # Cent-level tolerance: the solver apportions per allocation and quantizes each,
        # while the reference accumulates at full precision. One cent per allocation.
        tolerance = Decimal("0.01") * max(len(result.matches), 1)
        assert abs(result.total_duty_allocated - expected) <= tolerance, (
            f"solver {result.total_duty_allocated} vs optimum {expected}"
        )

    @settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_pools())
    def test_never_exceeds_lp_upper_bound(
        self, pools: tuple[list[EntryLine], list[ExportLine]]
    ) -> None:
        """A solver that beats the bound is over-allocating, not out-performing."""
        imports, exports = pools
        result = self._run(imports, exports)
        assert result.total_duty_allocated <= _lp_upper_bound(imports, exports) + Decimal("0.01")

    @settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_pools())
    def test_no_line_is_over_allocated(
        self, pools: tuple[list[EntryLine], list[ExportLine]]
    ) -> None:
        """Capacity constraints hold: double-claiming a line is a false claim."""
        imports, exports = pools
        result = self._run(imports, exports)

        used_import: dict[str, Decimal] = {}
        used_export: dict[str, Decimal] = {}
        for match in result.matches:
            used_import[str(match.import_line_id)] = (
                used_import.get(str(match.import_line_id), Decimal("0")) + match.quantity
            )
            used_export[str(match.export_line_id)] = (
                used_export.get(str(match.export_line_id), Decimal("0")) + match.quantity
            )

        for entry in imports:
            assert used_import.get(str(entry.line_id), Decimal("0")) <= entry.quantity_available
        for export in exports:
            assert used_export.get(str(export.line_id), Decimal("0")) <= export.quantity_available

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_pools())
    def test_is_deterministic(self, pools: tuple[list[EntryLine], list[ExportLine]]) -> None:
        """Same input, same allocation.

        A claim reviewed on Monday must not differ from the same claim refiled on
        Tuesday — there is no explanation for that an auditor would accept.
        """
        imports, exports = pools
        first = self._run(imports, exports)
        second = self._run(imports, exports)

        assert first.total_duty_allocated == second.total_duty_allocated
        assert [
            (str(m.import_line_id), str(m.export_line_id), m.quantity) for m in first.matches
        ] == [(str(m.import_line_id), str(m.export_line_id), m.quantity) for m in second.matches]

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_pools())
    def test_never_emits_a_theory_the_jurisdiction_forbids(
        self, pools: tuple[list[EntryLine], list[ExportLine]]
    ) -> None:
        imports, exports = pools
        result = self._run(imports, exports)
        for match in result.matches:
            assert US_PROFILE.permits(match.theory)
            assert match.theory is not MatchTheory.DECLARATION_LINKAGE


class TestCpSatWorkedExamples:
    """Hand-computed cases where greedy-by-arrival forfeits money."""

    matcher = UsSubstitutionMatcher()

    def _run(self, imports, exports):
        return self.matcher.match(
            MatchRequest(imports=imports, exports=exports, profile=US_PROFILE, as_of=AS_OF)
        )

    def test_prefers_the_expensive_import_when_export_capacity_is_scarce(self) -> None:
        """100 units of export against two imports of 100 units each.

        Cheap line: 100 units carrying $100 duty  -> $1.00/unit
        Dear line:  100 units carrying $9,000     -> $90.00/unit

        Only 100 units can be exported. Consuming the cheap line first — which arrival
        order would do — recovers $100. The optimum is $9,000.
        """
        cheap = _entry(FAMILIES[0], 100, Decimal("100.00"))
        dear = _entry(FAMILIES[0], 100, Decimal("9000.00"))
        export = _export(FAMILIES[0], 100)

        result = self._run([cheap, dear], [export])

        assert result.status is SolverStatus.OPTIMAL
        assert result.total_duty_allocated == Decimal("9000.00")
        assert len(result.matches) == 1
        assert result.matches[0].import_line_id == dear.line_id

    def test_splits_one_export_across_two_imports_when_capacity_requires(self) -> None:
        """150 units exported, imports of 100 and 100 at different rates.

        Takes all 100 dear units at $50/unit and tops up 50 cheap units at $5/unit:
        100 x 50.00 + 50 x 5.00 = 5,000.00 + 250.00 = 5,250.00
        """
        dear = _entry(FAMILIES[0], 100, Decimal("5000.00"))
        cheap = _entry(FAMILIES[0], 100, Decimal("500.00"))
        export = _export(FAMILIES[0], 150)

        result = self._run([dear, cheap], [export])

        assert result.status is SolverStatus.OPTIMAL
        assert result.total_duty_allocated == Decimal("5250.00")
        assert len(result.matches) == 2
        assert sum(m.quantity for m in result.matches) == Decimal("150")

    def test_refund_is_ninety_nine_percent_of_allocated_duty(self) -> None:
        entry = _entry(FAMILIES[0], 100, Decimal("1000.00"))
        export = _export(FAMILIES[0], 100)

        result = self._run([entry], [export])

        assert result.total_duty_allocated == Decimal("1000.00")
        assert result.total_refund == Decimal("990.00")

    def test_different_substitution_families_do_not_pair(self) -> None:
        entry = _entry(FAMILIES[0], 100, Decimal("1000.00"))
        export = _export(FAMILIES[1], 100)

        result = self._run([entry], [export])

        assert result.status is SolverStatus.NO_CANDIDATES
        assert result.matches == ()
        assert result.rejections

    def test_export_beyond_five_year_window_is_rejected(self) -> None:
        entry = _entry(FAMILIES[0], 100, Decimal("1000.00"))
        export = _export(FAMILIES[0], 100)
        far = export.model_copy(update={"export_date": IMPORT_DATE + timedelta(days=5 * 365 + 10)})

        result = self._run([entry], [far])

        assert result.matches == ()
        assert result.rejections

    def test_direct_identity_preferred_over_substitution(self) -> None:
        """Same tariff code needs no interchangeability narrative."""
        entry = _entry(FAMILIES[0], 100, Decimal("1000.00"), suffix="00")
        export = _export(FAMILIES[0], 100, suffix="00")

        result = self._run([entry], [export])

        assert result.matches[0].theory is MatchTheory.DIRECT_IDENTITY
        assert result.matches[0].substitution_key is None

    def test_substitution_carries_its_key(self) -> None:
        entry = _entry(FAMILIES[0], 100, Decimal("1000.00"), suffix="00")
        export = _export(FAMILIES[0], 100, suffix="50")

        result = self._run([entry], [export])

        assert result.matches[0].theory is MatchTheory.HTS_SUBSTITUTION
        assert result.matches[0].substitution_key == FAMILIES[0]

    def test_fully_designated_import_yields_nothing(self) -> None:
        entry = _entry(FAMILIES[0], 100, Decimal("1000.00"))
        exhausted = entry.model_copy(update={"quantity_designated": Decimal("100")})
        export = _export(FAMILIES[0], 100)

        result = self._run([exhausted], [export])

        assert result.matches == ()

    def test_wrong_jurisdiction_is_refused(self) -> None:
        from drawbridge_schemas.jurisdiction import KSA_PROFILE

        entry = _entry(FAMILIES[0], 100, Decimal("1000.00"))
        export = _export(FAMILIES[0], 100)

        with pytest.raises(ValueError, match="handles"):
            self.matcher.match(
                MatchRequest(imports=[entry], exports=[export], profile=KSA_PROFILE, as_of=AS_OF)
            )

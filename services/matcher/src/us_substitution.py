"""Path A — US substitution allocation, 19 U.S.C. §1313(j).

Many-to-many: any import line may feed several exports, any export may draw from several
imports. With substitution the eligible pairs are every (import, export) sharing an
8-digit HTS key inside the window, so the search space is the product of two pools.

Greedy pairing leaves money on the table. Two imports of the same article can carry very
different per-unit duty — a Section 301 line and a pre-301 line differ by 25 points — so
consuming the cheap one first against a large export forfeits the difference. That is why
this is an optimisation and not a walk.

See docs/ARCHITECTURE.md §3.6.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from ortools.sat.python import cp_model

from drawbridge_schemas.bom import BillOfMaterials, BomComponent, ManufacturingBasis
from drawbridge_schemas.jurisdiction import Jurisdiction, MatchTheory
from drawbridge_schemas.trade import EntryLine, ExportLine, LineMatch
from services.matcher.src.base import (
    MatchRequest,
    MatchResult,
    MatchStrategy,
    Rejection,
    RejectionCode,
    SolverStatus,
)
from services.rules.src.deadlines import window_for

if TYPE_CHECKING:
    from collections.abc import Sequence

# Quantities enter the model as integers: CP-SAT is an integer solver, and duty
# apportionment has to reproduce to the cent. Four decimal places matches the Numeric(18,4)
# quantity column in services/api/src/models.py.
QUANTITY_SCALE = 10_000

# Duty per unit is carried in micro-units of currency so the objective can weight a
# pairing by its true value without floating point. Six places is enough to keep
# per-unit duty exact for realistic line quantities.
DUTY_SCALE = 1_000_000


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One eligible (import, export) pairing, pre-screened and pre-costed.

    Under §1313(j) the pairing is import article to exported article, one to one. Under
    §1313(a)/(b) it is raw material to a *component slot* of a finished good, and
    `component_key` names which slot — one finished good draws from several import pools
    at once, so the pairing is per component rather than per export line.
    """

    import_index: int
    export_index: int
    theory: MatchTheory
    substitution_key: str | None
    duty_per_unit_micros: int
    max_quantity_scaled: int
    days_clock_start_to_export: int

    component_key: str | None = None
    """8-digit component subheading for manufacturing pairings; None for §1313(j)."""

    component_required_scaled: int = 0
    """Component quantity actually used to produce the exported finished goods —
    the TFTEA ceiling on what may be designated. Zero for §1313(j)."""

    @property
    def is_manufacturing(self) -> bool:
        return self.component_key is not None


class UsSubstitutionMatcher(MatchStrategy):
    """CP-SAT allocation over a substitution-eligible pool."""

    jurisdiction = Jurisdiction.US

    def match(self, request: MatchRequest) -> MatchResult:
        self._guard_jurisdiction(request)
        started = time.monotonic()

        candidates, rejections = self._build_candidates(request)
        if not candidates:
            return MatchResult(
                jurisdiction=self.jurisdiction,
                status=SolverStatus.NO_CANDIDATES,
                rejections=tuple(rejections),
                candidate_pairs=0,
                wall_time_seconds=time.monotonic() - started,
                detail="no import/export pairing survived eligibility screening",
            )

        model, allocations = self._build_model(request, candidates)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = request.time_limit_seconds
        # Determinism: a claim reviewed on Monday must allocate identically when refiled
        # on Tuesday. Multiple workers make CP-SAT's search order nondeterministic.
        solver.parameters.num_workers = 1
        solver.parameters.random_seed = 0

        status = solver.solve(model)
        elapsed = time.monotonic() - started

        solver_status = _translate_status(status)
        if solver_status in {SolverStatus.INFEASIBLE, SolverStatus.ERROR}:
            return MatchResult(
                jurisdiction=self.jurisdiction,
                status=solver_status,
                rejections=tuple(rejections),
                candidate_pairs=len(candidates),
                wall_time_seconds=elapsed,
                detail=f"solver returned {solver.status_name(status)}",
            )

        matches = self._extract_matches(request, candidates, allocations, solver)
        return MatchResult(
            jurisdiction=self.jurisdiction,
            status=solver_status,
            matches=tuple(matches),
            rejections=tuple(rejections),
            candidate_pairs=len(candidates),
            wall_time_seconds=elapsed,
            detail=solver.status_name(status),
        )

    # ------------------------------------------------------------------ screening

    def _build_candidates(self, request: MatchRequest) -> tuple[list[_Candidate], list[Rejection]]:
        """Generate eligible pairings and record why the rest were excluded.

        Candidates are emitted in a stable order — import index, then export index — so
        the model is byte-identical across runs on identical input.
        """
        candidates: list[_Candidate] = []
        rejections: list[Rejection] = []
        profile = request.profile

        for export_index, export in enumerate(request.exports):
            if export.quantity_available <= 0:
                rejections.append(
                    Rejection(
                        export_line_id=str(export.line_id),
                        code=RejectionCode.EXPORT_EXHAUSTED,
                        citation="prior claim designation",
                        detail=(
                            f"export {export.reference} line {export.line_number} fully "
                            f"claimed ({export.quantity_claimed} of {export.quantity})"
                        ),
                    )
                )
                continue

            eligible_for_export = 0
            window_failures = 0
            key_failures = 0

            bom = request.bom_for(export)

            for import_index, entry in enumerate(request.imports):
                if entry.quantity_available <= 0:
                    continue

                theory, component = _theory_for(entry, export, bom)
                if theory is None:
                    key_failures += 1
                    continue
                if not profile.permits(theory):
                    rejections.append(
                        Rejection(
                            export_line_id=str(export.line_id),
                            import_line_id=str(entry.line_id),
                            code=RejectionCode.THEORY_NOT_PERMITTED,
                            citation=profile.refund_rate_citation,
                            detail=f"{theory} is not available in {profile.jurisdiction}",
                        )
                    )
                    continue

                clock_start = entry.eligibility_clock_start
                window = window_for(profile, clock_start, export.export_date)
                if not window.export_in_window(export.export_date):
                    window_failures += 1
                    continue
                if not window.filing_in_window(request.as_of):
                    rejections.append(
                        Rejection(
                            export_line_id=str(export.line_id),
                            import_line_id=str(entry.line_id),
                            code=RejectionCode.US_FILING_DEADLINE_PASSED,
                            citation=profile.claim_filing_deadline.citation,
                            detail=(
                                f"filing {request.as_of} is past the deadline "
                                f"{window.effective_filing_deadline} for export "
                                f"{export.export_date}"
                            ),
                        )
                    )
                    continue

                duty_micros = _duty_per_unit_micros(entry, profile)
                if duty_micros <= 0:
                    # Nothing recoverable on this import; pairing it wastes solver time
                    # and could displace a paying allocation.
                    continue

                if component is not None:
                    # Manufacturing: the import satisfies a component slot, and the
                    # ceiling is the quantity actually used to produce the exported
                    # finished goods rather than the exported quantity itself.
                    required = component.required_for(export.quantity_available)
                    max_quantity = min(entry.quantity_available, required)
                    component_key = component.component_hts.substitution_key
                    required_scaled = _scale_quantity(required)
                else:
                    max_quantity = min(entry.quantity_available, export.quantity_available)
                    component_key = None
                    required_scaled = 0

                if max_quantity <= 0:
                    continue

                candidates.append(
                    _Candidate(
                        import_index=import_index,
                        export_index=export_index,
                        theory=theory,
                        substitution_key=(
                            entry.hts.substitution_key
                            if theory
                            in {
                                MatchTheory.HTS_SUBSTITUTION,
                                MatchTheory.MANUFACTURING_SUBSTITUTION,
                            }
                            else None
                        ),
                        duty_per_unit_micros=duty_micros,
                        max_quantity_scaled=_scale_quantity(max_quantity),
                        days_clock_start_to_export=(export.export_date - clock_start).days,
                        component_key=component_key,
                        component_required_scaled=required_scaled,
                    )
                )
                eligible_for_export += 1

            if eligible_for_export == 0:
                rejections.append(
                    Rejection(
                        export_line_id=str(export.line_id),
                        code=(
                            RejectionCode.US_WINDOW_EXPIRED
                            if window_failures
                            else RejectionCode.SUBSTITUTION_KEY_MISMATCH
                            if key_failures
                            else RejectionCode.NO_ELIGIBLE_IMPORT
                        ),
                        citation=(
                            profile.reexport_window.citation
                            if window_failures
                            else "19 U.S.C. §1313(j)(2) — 8-digit HTS substitution"
                        ),
                        detail=(
                            f"export {export.reference} line {export.line_number} "
                            f"({export.hts.code}, {export.export_date}) matched no "
                            f"eligible import: {window_failures} outside the 5-year "
                            f"window, {key_failures} on a different 8-digit key"
                        ),
                    )
                )

        return candidates, rejections

    # ---------------------------------------------------------------------- model

    def _build_model(
        self, request: MatchRequest, candidates: Sequence[_Candidate]
    ) -> tuple[cp_model.CpModel, list[cp_model.IntVar]]:
        model = cp_model.CpModel()

        # x[c] = quantity (scaled) routed through candidate pairing c.
        allocations = [
            model.new_int_var(0, candidate.max_quantity_scaled, f"x_{index}")
            for index, candidate in enumerate(candidates)
        ]

        # An import line cannot be over-allocated across all the exports it feeds.
        by_import: dict[int, list[int]] = {}
        by_export: dict[int, list[int]] = {}
        for index, candidate in enumerate(candidates):
            by_import.setdefault(candidate.import_index, []).append(index)
            by_export.setdefault(candidate.export_index, []).append(index)

        for import_index, indices in by_import.items():
            capacity = _scale_quantity(request.imports[import_index].quantity_available)
            model.add(sum(allocations[i] for i in indices) <= capacity)

        # An export line cannot be claimed twice.
        #
        # For §1313(j) the ceiling is the exported quantity itself. For manufacturing the
        # units do not cancel — one finished unit consumes `quantity_per_unit / yield` of
        # each component — so the ceiling is per (export, component) and is the quantity
        # *actually used*, which is what TFTEA ties the designation to.
        for export_index, indices in by_export.items():
            manufacturing = [i for i in indices if candidates[i].is_manufacturing]
            unused = [i for i in indices if not candidates[i].is_manufacturing]

            if unused:
                capacity = _scale_quantity(request.exports[export_index].quantity_available)
                model.add(sum(allocations[i] for i in unused) <= capacity)

            by_component: dict[str, list[int]] = {}
            for index in manufacturing:
                key = candidates[index].component_key
                assert key is not None
                by_component.setdefault(key, []).append(index)

            for component_indices in by_component.values():
                required = candidates[component_indices[0]].component_required_scaled
                model.add(sum(allocations[i] for i in component_indices) <= required)

        # Maximise refundable duty, not matched quantity. Matching units maximises
        # paperwork; matching duty maximises the refund.
        model.maximize(
            sum(
                allocations[index] * candidate.duty_per_unit_micros
                for index, candidate in enumerate(candidates)
            )
        )
        return model, allocations

    # ------------------------------------------------------------------ extraction

    def _extract_matches(
        self,
        request: MatchRequest,
        candidates: Sequence[_Candidate],
        allocations: Sequence[cp_model.IntVar],
        solver: cp_model.CpSolver,
    ) -> list[LineMatch]:
        matches: list[LineMatch] = []
        profile = request.profile

        for candidate, variable in zip(candidates, allocations, strict=True):
            scaled = solver.value(variable)
            if scaled <= 0:
                continue

            entry = request.imports[candidate.import_index]
            export = request.exports[candidate.export_index]
            quantity = _unscale_quantity(scaled)

            duty_allocated = _apportion_duty(entry, quantity, profile)
            refund = (duty_allocated * profile.refund_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            matches.append(
                LineMatch(
                    import_line_id=entry.line_id,
                    export_line_id=export.line_id,
                    quantity=quantity,
                    theory=candidate.theory,
                    substitution_key=candidate.substitution_key,
                    duty_allocated=duty_allocated,
                    refund_amount=refund,
                    days_clock_start_to_export=candidate.days_clock_start_to_export,
                )
            )
        return matches


# -------------------------------------------------------------------------- helpers


def _theory_for(
    entry: EntryLine, export: ExportLine, bom: BillOfMaterials | None
) -> tuple[MatchTheory | None, BomComponent | None]:
    """Which §1313 theory, if any, could pair these two lines, and under which component.

    Unused merchandise is tried first: where the imported article *is* the exported
    article, §1313(j) applies and no bill of materials is needed. Only when the codes do
    not line up does a BOM make the pairing possible, and then the claim is a
    manufacturing one under §1313(a)/(b).

    Direct identity is preferred over substitution in both families: the full tariff code
    matching needs no commercial-interchangeability narrative and survives a desk audit
    more easily.
    """
    if entry.hts.code == export.hts.code:
        return MatchTheory.DIRECT_IDENTITY, None
    if entry.hts.substitutable_with(export.hts):
        return MatchTheory.HTS_SUBSTITUTION, None

    if bom is not None:
        component = bom.component_for(entry.hts)
        if component is not None:
            theory = (
                MatchTheory.MANUFACTURING_DIRECT_IDENTITY
                if component.basis is ManufacturingBasis.DIRECT_IDENTITY
                else MatchTheory.MANUFACTURING_SUBSTITUTION
            )
            return theory, component

    return None, None


def _duty_per_unit_micros(entry: EntryLine, profile: object) -> int:
    """Recoverable duty per unit, in micro-units, for the objective function.

    Truncated rather than rounded: the objective must never claim more per unit than the
    line actually carries.
    """
    if entry.quantity <= 0:
        return 0
    base = entry.recoverable_base(profile)  # type: ignore[arg-type]
    per_unit = base / entry.quantity
    return int((per_unit * DUTY_SCALE).to_integral_value(rounding=ROUND_DOWN))


def _apportion_duty(entry: EntryLine, quantity: Decimal, profile: object) -> Decimal:
    """Pro-rata duty for an allocated quantity, quantized to the cent.

    Computed from the line total rather than from the per-unit micro figure used in the
    objective, so cent-level rounding is applied once, at the end.
    """
    if entry.quantity <= 0:
        return Decimal("0.00")
    base = entry.recoverable_base(profile)  # type: ignore[arg-type]
    share = (base * quantity) / entry.quantity
    return share.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _scale_quantity(quantity: Decimal) -> int:
    return int((quantity * QUANTITY_SCALE).to_integral_value(rounding=ROUND_DOWN))


def _unscale_quantity(scaled: int) -> Decimal:
    return (Decimal(scaled) / QUANTITY_SCALE).normalize()


def _translate_status(status: object) -> SolverStatus:
    """Map CP-SAT's status enum onto ours.

    Typed as `object` because ortools ships the status as a protobuf enum whose stub
    varies across releases; the match below is exhaustive either way.
    """
    match status:
        case cp_model.OPTIMAL:
            return SolverStatus.OPTIMAL
        case cp_model.FEASIBLE:
            return SolverStatus.FEASIBLE
        case cp_model.INFEASIBLE:
            return SolverStatus.INFEASIBLE
        case _:
            return SolverStatus.ERROR

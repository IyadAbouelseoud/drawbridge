"""The matching contract shared by both jurisdictions.

Two implementations sit behind this: `us_substitution.UsSubstitutionMatcher` solves a
combinatorial allocation with CP-SAT, and `gcc_linkage.GccLinkageMatcher` walks a
declaration link through statutory gates. They are different algorithms, not one
algorithm with a flag — see docs/ARCHITECTURE.md §3.5-3.8.

What they share is this contract, so nothing downstream of the matcher branches on
jurisdiction again.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from drawbridge_schemas.jurisdiction import Jurisdiction, JurisdictionProfile
from drawbridge_schemas.trade import EntryLine, ExportLine, LineMatch

if TYPE_CHECKING:
    from collections.abc import Sequence


class RejectionCode(StrEnum):
    """Why a candidate pairing or an export line was not claimable.

    Every code names the provision it failed. A claim that dies must be able to say why
    in the words of the statute — silent filtering makes an unclaimable position
    indistinguishable from an unexamined one.
    """

    # --- GCC, Rules of Implementation Art. 16 and Common Customs Law Art. 174 ---
    NO_DECLARATION_LINK = "no_declaration_link"
    """Art. 15(c) — re-export declaration carries no import declaration number."""

    UNKNOWN_IMPORT_DECLARATION = "unknown_import_declaration"
    """Art. 15(c) — the linked declaration does not resolve to a known import line."""

    BELOW_MINIMUM_VALUE = "below_minimum_value"
    """Art. 16 §2 — re-export value under USD 5,000."""

    REEXPORT_WINDOW_EXPIRED = "reexport_window_expired"
    """Art. 16 §3(a) — re-export beyond one Gregorian year of duty payment."""

    FILING_DEADLINE_PASSED = "filing_deadline_passed"
    """Art. 16 §3(b) — claim filed beyond six Gregorian months of re-export."""

    ABSOLUTE_BAR_PASSED = "absolute_bar_passed"
    """Common Customs Law Art. 174 — duty paid more than three years ago."""

    GOODS_USED_OR_ALTERED = "goods_used_or_altered"
    """Art. 16 §5 — goods locally used, or not in the condition imported."""

    CONSIGNMENT_MISMATCH = "consignment_mismatch"
    """Art. 16 §4 — part shipments not proven to belong to one consignment."""

    # --- shared ---
    NO_ELIGIBLE_IMPORT = "no_eligible_import"
    """No import line matched the export under any permitted theory."""

    IMPORT_EXHAUSTED = "import_exhausted"
    """Every eligible import line was fully designated by prior claims."""

    EXPORT_EXHAUSTED = "export_exhausted"
    """The export line was fully claimed by prior claims."""

    QUANTITY_UNAVAILABLE = "quantity_unavailable"
    """Eligible pairing existed but no quantity remained on either side."""

    THEORY_NOT_PERMITTED = "theory_not_permitted"
    """The only available pairing rests on a theory this jurisdiction forbids."""

    # --- US, 19 U.S.C. §1313 ---
    SUBSTITUTION_KEY_MISMATCH = "substitution_key_mismatch"
    """§1313(j)(2) — 8-digit HTS keys differ, so substitution is unavailable."""

    US_WINDOW_EXPIRED = "us_window_expired"
    """§1313(j) — export more than five years after import."""

    US_FILING_DEADLINE_PASSED = "us_filing_deadline_passed"
    """§1313(r) — claim filed more than three years after export."""


class SolverStatus(StrEnum):
    """Outcome of the matching run.

    `FEASIBLE` is not `OPTIMAL`. A feasible-but-not-proven-optimal allocation is a
    legitimate result but must never be presented as the maximum: it routes to
    ANALYST_REVIEW rather than straight to quantification.
    """

    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    NO_CANDIDATES = "no_candidates"
    TIMEOUT = "timeout"
    ERROR = "error"

    @property
    def is_trustworthy(self) -> bool:
        """Whether the result may proceed without analyst sign-off on the allocation."""
        return self in {SolverStatus.OPTIMAL, SolverStatus.NO_CANDIDATES}


@dataclass(frozen=True, slots=True)
class Rejection:
    """One rejected export line, with the provision it failed and the figures involved."""

    export_line_id: str
    code: RejectionCode
    citation: str
    detail: str
    import_line_id: str | None = None

    def __str__(self) -> str:
        return f"[{self.code}] {self.detail} ({self.citation})"


@dataclass(frozen=True, slots=True)
class MatchResult:
    """What a matcher returns.

    Accepted matches and explicit rejections, plus enough solver metadata to reproduce
    the run. `rejections` is not diagnostic noise — it is the record of positions
    examined and found unclaimable, which is what an auditor asks about.
    """

    jurisdiction: Jurisdiction
    status: SolverStatus
    matches: tuple[LineMatch, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    candidate_pairs: int = 0
    wall_time_seconds: float = 0.0
    detail: str = ""

    @property
    def total_duty_allocated(self) -> Decimal:
        return sum((m.duty_allocated for m in self.matches), Decimal("0.00"))

    @property
    def total_refund(self) -> Decimal:
        return sum((m.refund_amount for m in self.matches), Decimal("0.00"))

    @property
    def needs_analyst_review(self) -> bool:
        return not self.status.is_trustworthy

    def rejections_for(self, code: RejectionCode) -> tuple[Rejection, ...]:
        return tuple(r for r in self.rejections if r.code is code)


@dataclass(frozen=True, slots=True)
class MatchRequest:
    """Everything a matcher needs. No implicit context, no database access.

    Matchers are pure functions of their input so a claim can be re-run years later from
    the stored lines and reproduce the same allocation.
    """

    imports: Sequence[EntryLine]
    exports: Sequence[ExportLine]
    profile: JurisdictionProfile
    as_of: date
    """Filing date the deadlines are evaluated against."""

    time_limit_seconds: float = 30.0
    claimant_is_importer_of_record: bool = True
    """Art. 16 §1. False requires documented proof of purchase, checked upstream."""

    proof_of_purchase: bool = False


class MatchStrategy(ABC):
    """Jurisdiction-specific matching."""

    jurisdiction: Jurisdiction

    @abstractmethod
    def match(self, request: MatchRequest) -> MatchResult:
        """Pair import lines to export lines under this jurisdiction's statute."""

    def _guard_jurisdiction(self, request: MatchRequest) -> None:
        """Refuse a request routed to the wrong strategy.

        Cheap, and it catches the routing bug that would otherwise produce a plausible
        allocation under the wrong statute.
        """
        if request.profile.jurisdiction is not self.jurisdiction:
            msg = (
                f"{type(self).__name__} handles {self.jurisdiction}, but the request "
                f"carries profile for {request.profile.jurisdiction}"
            )
            raise ValueError(msg)


@dataclass(slots=True)
class _Ledger:
    """Running quantity availability during a match run.

    Prior claims are already netted off by `quantity_available`; this tracks what the
    current run has consumed on top of that, so a single run cannot over-allocate a line
    across several pairings.
    """

    remaining: dict[str, Decimal] = field(default_factory=dict)

    def seed_imports(self, imports: Sequence[EntryLine]) -> None:
        for line in imports:
            self.remaining[str(line.line_id)] = line.quantity_available

    def seed_exports(self, exports: Sequence[ExportLine]) -> None:
        for line in exports:
            self.remaining[str(line.line_id)] = line.quantity_available

    def available(self, line_id: str) -> Decimal:
        return self.remaining.get(line_id, Decimal("0"))

    def take(self, line_id: str, quantity: Decimal) -> None:
        self.remaining[line_id] = self.available(line_id) - quantity

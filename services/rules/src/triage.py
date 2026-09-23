"""Human-in-the-loop triage.

Decides whether a claim may proceed autonomously or must stop for an analyst. The rule
throughout: where proceeding would produce a plausible-looking figure with no basis, stop
and say why. A queued claim is visibly unfinished; a silently-wrong one is not.

n8n calls this at the gate after matching and suspends on whatever it returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from services.matcher.src.base import MatchResult, RejectionCode, SolverStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from drawbridge_schemas.provenance import Confidence

# Below this, an extracted figure is not trustworthy enough to reach a filing without a
# human having looked at it. Deliberately high: the cost of a review is minutes, and the
# cost of a wrong figure on a customs filing is the claim plus the relationship.
EXTRACTION_CONFIDENCE_FLOOR = 0.95

# A deadline this close needs a human to decide whether to rush or abandon, because the
# usual queue latency would consume the remaining window.
DEADLINE_URGENCY_DAYS = 30


class ReviewReason(StrEnum):
    """Why a claim stopped. Mirrors the ck_review_reason CHECK constraint."""

    SOLVER_NOT_OPTIMAL = "solver_not_optimal"
    SOLVER_INFEASIBLE = "solver_infeasible"
    LOW_EXTRACTION_CONFIDENCE = "low_extraction_confidence"
    THRESHOLD_NEAR_MISS = "threshold_near_miss"
    RATE_UNAVAILABLE = "rate_unavailable"
    UNKNOWN_FIELD_LABEL = "unknown_field_label"
    JURISDICTION_AMBIGUOUS = "jurisdiction_ambiguous"
    DEADLINE_IMMINENT = "deadline_imminent"
    # v1.1.0. A refund above the auto-approve ceiling; resolved only by an approver who
    # did not resolve the claim's other exceptions (services/api/src/gates.py).
    HIGH_VALUE_APPROVAL = "high_value_approval"
    # v1.1.0. Text on a source document addressed to an AI rather than to a customs
    # officer. Raised by the drafter, which then declines to draft (agent/injection.py).
    SUSPECTED_PROMPT_INJECTION = "suspected_prompt_injection"
    # v1.1.0. A pipeline run that crashed. Until now these were filed as
    # `solver_infeasible`, which is a statement about the matcher that was never true.
    PIPELINE_FAILURE = "pipeline_failure"


class Severity(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    BLOCKING = "blocking"
    """Nothing downstream may run until this clears."""


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One reason a claim was suspended, ready to become a review_queue row."""

    reason: ReviewReason
    severity: Severity
    summary: str
    citation: str | None = None
    payload: dict[str, object] | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "citation": self.citation,
            "payload": self.payload or {},
        }


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    """Whether the pipeline may continue, and everything that stopped it."""

    items: tuple[ReviewItem, ...]

    @property
    def requires_review(self) -> bool:
        return bool(self.items)

    @property
    def is_blocking(self) -> bool:
        return any(i.severity is Severity.BLOCKING for i in self.items)

    @property
    def highest_severity(self) -> Severity:
        order = (Severity.LOW, Severity.NORMAL, Severity.HIGH, Severity.BLOCKING)
        return max((i.severity for i in self.items), key=order.index, default=Severity.LOW)


def triage(
    result: MatchResult,
    *,
    confidences: Sequence[Confidence] = (),
    filing_deadline: date | None = None,
    as_of: date | None = None,
    confidence_floor: float = EXTRACTION_CONFIDENCE_FLOOR,
    refund: tuple[Decimal, str] | None = None,
    refund_usd: Decimal | None = None,
    approval_ceiling_usd: Decimal | None = None,
) -> TriageVerdict:
    """Decide whether a matched claim may proceed without a human."""
    items: list[ReviewItem] = []

    items.extend(_solver_items(result))
    items.extend(_extraction_items(confidences, confidence_floor))
    items.extend(_valuation_items(result))
    items.extend(_deadline_items(filing_deadline, as_of))
    items.extend(_approval_items(refund, refund_usd, approval_ceiling_usd))

    return TriageVerdict(tuple(items))


def _solver_items(result: MatchResult) -> list[ReviewItem]:
    """A feasible allocation is not a proven-maximum allocation.

    Presenting one as the other would quietly under-claim, and nothing downstream could
    tell the difference.
    """
    if result.status is SolverStatus.INFEASIBLE:
        return [
            ReviewItem(
                reason=ReviewReason.SOLVER_INFEASIBLE,
                severity=Severity.BLOCKING,
                summary=(
                    f"matcher returned infeasible over {result.candidate_pairs} candidate "
                    f"pairings: {result.detail}"
                ),
                payload={"candidate_pairs": result.candidate_pairs},
            )
        ]

    if result.status.is_trustworthy:
        return []

    return [
        ReviewItem(
            reason=ReviewReason.SOLVER_NOT_OPTIMAL,
            severity=Severity.HIGH,
            summary=(
                f"matcher status {result.status} is not a proven maximum; the allocation "
                f"may under-claim ({result.detail})"
            ),
            payload={
                "status": result.status.value,
                "candidate_pairs": result.candidate_pairs,
                "wall_time_seconds": round(result.wall_time_seconds, 3),
                "total_refund": str(result.total_refund),
            },
        )
    ]


def _extraction_items(confidences: Sequence[Confidence], floor: float) -> list[ReviewItem]:
    flagged = [c for c in confidences if c.needs_review or c.score < floor]
    if not flagged:
        return []

    worst = min(flagged, key=lambda c: c.score)
    return [
        ReviewItem(
            reason=ReviewReason.LOW_EXTRACTION_CONFIDENCE,
            severity=Severity.HIGH,
            summary=(
                f"{len(flagged)} extracted field(s) below the {floor:.0%} confidence "
                f"floor; lowest {worst.score:.0%} via {worst.method}"
            ),
            payload={
                "floor": floor,
                "flagged": len(flagged),
                "lowest_score": worst.score,
                "lowest_method": worst.method,
            },
        )
    ]


def _valuation_items(result: MatchResult) -> list[ReviewItem]:
    """GCC threshold and exchange-rate exceptions.

    Near misses are rejections, not acceptances — Art. 16 §2 says "shall not be less
    than". Queueing them makes the near miss visible instead of the claim vanishing.
    """
    items: list[ReviewItem] = []

    near_misses = [
        r
        for r in result.rejections_for(RejectionCode.BELOW_MINIMUM_VALUE)
        if "[near_miss]" in r.detail
    ]
    if near_misses:
        items.append(
            ReviewItem(
                reason=ReviewReason.THRESHOLD_NEAR_MISS,
                severity=Severity.NORMAL,
                summary=(
                    f"{len(near_misses)} re-export(s) fell within 10% of the USD 5,000 "
                    "minimum and were rejected; confirm the valuation basis before "
                    "abandoning them"
                ),
                citation="GCC Rules of Implementation Art. 16 §2",
                payload={"details": [r.detail for r in near_misses[:20]]},
            )
        )

    unavailable = result.rejections_for(RejectionCode.RATE_UNAVAILABLE)
    if unavailable:
        items.append(
            ReviewItem(
                reason=ReviewReason.RATE_UNAVAILABLE,
                severity=Severity.BLOCKING,
                summary=(
                    f"{len(unavailable)} re-export(s) could not be screened against the "
                    "Art. 16 §2 threshold: no exchange rate on file as at the "
                    "duty-payment date"
                ),
                citation="GCC Rules of Implementation of Valuation Art. 1(I)(6)",
                payload={"details": [r.detail for r in unavailable[:20]]},
            )
        )

    return items


def _approval_items(
    refund: tuple[Decimal, str] | None,
    refund_usd: Decimal | None,
    ceiling_usd: Decimal | None,
) -> list[ReviewItem]:
    """A refund large enough that finding nothing to question is not enough on its own.

    The approval gate (`services/api/src/gates.py`) refuses to let the pipeline approve a
    claim above the ceiling without this row resolved by an approver. Raising it here, at
    triage, is what makes the ordinary n8n path suspend for that approver instead of
    running to the gate and failing there. A currency with no fixed conversion counts as
    above the ceiling: the safe direction for an unknown is a person.
    """
    if refund is None or ceiling_usd is None:
        return []
    amount, currency = refund
    if refund_usd is not None and refund_usd <= ceiling_usd:
        return []
    return [
        ReviewItem(
            reason=ReviewReason.HIGH_VALUE_APPROVAL,
            severity=Severity.HIGH,
            summary=(
                f"refund of {amount} {currency} exceeds the auto-approve ceiling of USD "
                f"{ceiling_usd}; an approver who did not resolve this claim's other "
                "exceptions must approve it"
            ),
            payload={
                "total_refund": str(amount),
                "currency": currency,
                "refund_usd": str(refund_usd) if refund_usd is not None else None,
                "ceiling_usd": str(ceiling_usd),
            },
        )
    ]


def _deadline_items(filing_deadline: date | None, as_of: date | None) -> list[ReviewItem]:
    if filing_deadline is None or as_of is None:
        return []

    remaining = (filing_deadline - as_of).days
    if remaining > DEADLINE_URGENCY_DAYS:
        return []

    severity = Severity.BLOCKING if remaining < 0 else Severity.HIGH
    verb = "expired" if remaining < 0 else "closes"
    return [
        ReviewItem(
            reason=ReviewReason.DEADLINE_IMMINENT,
            severity=severity,
            summary=(
                f"filing window {verb} {filing_deadline} — {abs(remaining)} day(s) "
                f"{'ago' if remaining < 0 else 'remaining'}; normal queue latency would "
                "consume it"
            ),
            payload={"filing_deadline": filing_deadline.isoformat(), "days": remaining},
        )
    ]


def confidence_summary(confidences: Sequence[Confidence]) -> dict[str, object]:
    """Aggregate extraction confidence, for the workflow to log against the run."""
    if not confidences:
        return {"count": 0, "min": None, "mean": None}
    scores = [c.score for c in confidences]
    return {
        "count": len(scores),
        "min": min(scores),
        "mean": float(sum(Decimal(str(s)) for s in scores) / len(scores)),
        "below_floor": sum(1 for s in scores if s < EXTRACTION_CONFIDENCE_FLOOR),
    }

"""Claim aggregate and its state machine.

Postgres owns this state machine. n8n observes transitions and fires side effects; it
never holds claim state. See docs/ARCHITECTURE.md §4 and §8.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from drawbridge_schemas.trade import LineMatch, Money

# CBP refunds 99% of duties, taxes and fees on qualifying drawback claims.
DRAWBACK_REFUND_RATE = Decimal("0.99")

# 19 U.S.C. §1313(j): export must occur within 5 years of import.
MAX_IMPORT_TO_EXPORT_DAYS = 1826

# Claim must be filed within 3 years of the export date.
MAX_EXPORT_TO_FILING_DAYS = 1095


class RecoveryLane(StrEnum):
    """Which statutory refund path a claim travels."""

    DRAWBACK = "drawback"
    POST_SUMMARY_CORRECTION = "post_summary_correction"
    FTA_RETROACTIVE = "fta_retroactive"


class DrawbackType(StrEnum):
    """Sub-theory within the drawback lane."""

    UNUSED_DIRECT_IDENTITY = "unused_direct_identity"
    UNUSED_SUBSTITUTION = "unused_substitution"
    MANUFACTURING_DIRECT_IDENTITY = "manufacturing_direct_identity"
    MANUFACTURING_SUBSTITUTION = "manufacturing_substitution"
    REJECTED_MERCHANDISE = "rejected_merchandise"


class ClaimState(StrEnum):
    """Explicit state machine. Transitions are validated by `ClaimState.can_move_to`."""

    INTAKE = "intake"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    CLASSIFYING = "classifying"
    CLASSIFIED = "classified"
    MATCHING = "matching"
    MATCHED = "matched"
    QUANTIFIED = "quantified"
    ANALYST_REVIEW = "analyst_review"
    APPROVED = "approved"
    PACKAGED = "packaged"
    HANDED_OFF = "handed_off"
    FILED = "filed"
    PAID = "paid"
    REJECTED = "rejected"
    EXPIRED = "expired"

    def can_move_to(self, target: ClaimState) -> bool:
        return target in _TRANSITIONS.get(self, frozenset())


# Any working state may branch to ANALYST_REVIEW on exception; review returns to the
# stage that raised it or moves forward to APPROVED.
_WORKING = (
    ClaimState.EXTRACTING,
    ClaimState.CLASSIFYING,
    ClaimState.MATCHING,
    ClaimState.QUANTIFIED,
)

_TRANSITIONS: dict[ClaimState, frozenset[ClaimState]] = {
    ClaimState.INTAKE: frozenset({ClaimState.EXTRACTING, ClaimState.REJECTED}),
    ClaimState.EXTRACTING: frozenset(
        {ClaimState.EXTRACTED, ClaimState.ANALYST_REVIEW, ClaimState.REJECTED}
    ),
    ClaimState.EXTRACTED: frozenset({ClaimState.CLASSIFYING}),
    ClaimState.CLASSIFYING: frozenset(
        {ClaimState.CLASSIFIED, ClaimState.ANALYST_REVIEW, ClaimState.REJECTED}
    ),
    ClaimState.CLASSIFIED: frozenset({ClaimState.MATCHING}),
    ClaimState.MATCHING: frozenset(
        {ClaimState.MATCHED, ClaimState.ANALYST_REVIEW, ClaimState.REJECTED}
    ),
    ClaimState.MATCHED: frozenset({ClaimState.QUANTIFIED}),
    ClaimState.QUANTIFIED: frozenset({ClaimState.ANALYST_REVIEW, ClaimState.EXPIRED}),
    ClaimState.ANALYST_REVIEW: frozenset({ClaimState.APPROVED, ClaimState.REJECTED, *_WORKING}),
    ClaimState.APPROVED: frozenset({ClaimState.PACKAGED}),
    ClaimState.PACKAGED: frozenset({ClaimState.HANDED_OFF}),
    ClaimState.HANDED_OFF: frozenset({ClaimState.FILED, ClaimState.REJECTED}),
    ClaimState.FILED: frozenset({ClaimState.PAID, ClaimState.REJECTED}),
    ClaimState.PAID: frozenset(),
    ClaimState.REJECTED: frozenset(),
    ClaimState.EXPIRED: frozenset(),
}


class RefundLine(BaseModel):
    """One quantified refund component, traceable to the match that produced it."""

    model_config = ConfigDict(frozen=True)

    match: LineMatch
    duty_component: Money
    mpf_component: Money = Decimal("0.00")
    hmf_component: Money = Decimal("0.00")

    @property
    def refund(self) -> Decimal:
        base = self.duty_component + self.mpf_component + self.hmf_component
        return (base * DRAWBACK_REFUND_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Claim(BaseModel):
    """The aggregate handed to a licensed filer.

    Drawbridge never files with CBP. This object is the filing-ready packet's data core;
    `services/packager` renders it to CBP 7551/7552, PSC, or §1520(d) forms.
    """

    model_config = ConfigDict(frozen=True)

    claim_id: UUID
    tenant_id: UUID
    state: ClaimState = ClaimState.INTAKE

    lane: RecoveryLane
    drawback_type: DrawbackType | None = None

    period_start: date
    period_end: date
    filing_deadline: date

    refund_lines: tuple[RefundLine, ...] = ()

    created_at: datetime
    updated_at: datetime

    @property
    def total_refund(self) -> Decimal:
        return sum((line.refund for line in self.refund_lines), Decimal("0.00"))

    @property
    def has_full_provenance(self) -> bool:
        """A claim missing any provenance span must not reach PACKAGED."""
        return all(
            m.match.import_line_id is not None and m.match.export_line_id is not None
            for m in self.refund_lines
        )

    @model_validator(mode="after")
    def _drawback_type_matches_lane(self) -> Self:
        if self.lane is RecoveryLane.DRAWBACK and self.drawback_type is None:
            msg = "drawback lane requires a drawback_type"
            raise ValueError(msg)
        if self.lane is not RecoveryLane.DRAWBACK and self.drawback_type is not None:
            msg = f"drawback_type is meaningless on lane {self.lane}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _period_ordered(self) -> Self:
        if self.period_end < self.period_start:
            msg = "period_end precedes period_start"
            raise ValueError(msg)
        return self

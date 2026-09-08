"""Claim aggregate and its state machine.

Postgres owns this state machine. n8n observes transitions and fires side effects; it
never holds claim state. See docs/ARCHITECTURE.md §4 and §8.

Dual-jurisdiction as of week 2: refund rate, deadlines and eligibility gates come from
the jurisdiction profile, never from a module constant. See docs/COMPLIANCE-GCC.md.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from drawbridge_schemas.jurisdiction import (
    Currency,
    Jurisdiction,
    JurisdictionProfile,
    profile_for,
)
from drawbridge_schemas.trade import LineMatch, Money


class RecoveryLane(StrEnum):
    """Which statutory refund path a claim travels."""

    # United States
    DRAWBACK = "drawback"
    POST_SUMMARY_CORRECTION = "post_summary_correction"
    FTA_RETROACTIVE = "fta_retroactive"

    # Saudi Arabia / GCC
    GCC_REEXPORT_DRAWBACK = "gcc_reexport_drawback"
    """GCC Common Customs Law Art. 97 + Rules of Implementation Art. 16."""

    KSA_ORIGIN_REFUND = "ksa_origin_refund"
    """Ministerial Decision 3852 — duty refunded once GCC origin is established."""


LANES_BY_JURISDICTION: dict[Jurisdiction, frozenset[RecoveryLane]] = {
    Jurisdiction.US: frozenset(
        {
            RecoveryLane.DRAWBACK,
            RecoveryLane.POST_SUMMARY_CORRECTION,
            RecoveryLane.FTA_RETROACTIVE,
        }
    ),
    Jurisdiction.KSA: frozenset(
        {RecoveryLane.GCC_REEXPORT_DRAWBACK, RecoveryLane.KSA_ORIGIN_REFUND}
    ),
}


class DrawbackType(StrEnum):
    """Sub-theory within a drawback lane.

    The substitution variants are US-only; the GCC recognises no substitution
    (docs/COMPLIANCE-GCC.md §2.2).
    """

    UNUSED_DIRECT_IDENTITY = "unused_direct_identity"
    UNUSED_SUBSTITUTION = "unused_substitution"
    MANUFACTURING_DIRECT_IDENTITY = "manufacturing_direct_identity"
    MANUFACTURING_SUBSTITUTION = "manufacturing_substitution"
    REJECTED_MERCHANDISE = "rejected_merchandise"

    GCC_UNUSED_REEXPORT = "gcc_unused_reexport"
    """Art. 16 §5 — not locally used, same condition as imported."""


_SUBSTITUTION_TYPES = frozenset(
    {DrawbackType.UNUSED_SUBSTITUTION, DrawbackType.MANUFACTURING_SUBSTITUTION}
)


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
    # QUANTIFIED -> APPROVED is the automated lane, added in week 9. Until then every
    # claim was routed through ANALYST_REVIEW, which meant a clean high-confidence claim
    # occupied a human's queue to be waved through — and a queue of claims that never
    # need attention is a queue people stop reading.
    #
    # The guarantee that edge could have destroyed is preserved elsewhere rather than
    # dropped: `analyst.transition_claim` refuses any move into APPROVED while the claim
    # carries an unresolved `review_queue` row, whoever is moving it. So approval without
    # a human happens only where triage found nothing for a human to do, and the
    # transition row records `pipeline` as the actor, so which claims took this lane is a
    # query rather than an inference.
    ClaimState.QUANTIFIED: frozenset(
        {ClaimState.ANALYST_REVIEW, ClaimState.APPROVED, ClaimState.EXPIRED}
    ),
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
    """One quantified refund component, traceable to the match that produced it.

    The refund rate is a jurisdiction property (US 99%, GCC 100% of duty actually paid),
    so it is passed in rather than read from a constant.
    """

    model_config = ConfigDict(frozen=True)

    match: LineMatch
    duty_component: Money
    mpf_component: Money = Decimal("0.00")
    hmf_component: Money = Decimal("0.00")

    def refund(self, profile: JurisdictionProfile) -> Decimal:
        base = self.duty_component
        if profile.includes_fees_in_base:
            base += self.mpf_component + self.hmf_component
        return (base * profile.refund_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Claim(BaseModel):
    """The aggregate handed to a licensed filer.

    Drawbridge never files with a customs authority in any jurisdiction. This object is
    the filing-ready packet's data core; `services/packager` renders it to CBP
    7551/7552/PSC/1520(d) or to a ZATCA e-Services refund request.
    """

    model_config = ConfigDict(frozen=True)

    claim_id: UUID
    tenant_id: UUID
    state: ClaimState = ClaimState.INTAKE

    jurisdiction: Jurisdiction = Jurisdiction.US
    currency: Currency = Currency.USD
    lane: RecoveryLane
    drawback_type: DrawbackType | None = None

    period_start: date
    period_end: date
    filing_deadline: date

    refund_lines: tuple[RefundLine, ...] = ()

    created_at: datetime
    updated_at: datetime

    @property
    def profile(self) -> JurisdictionProfile:
        return profile_for(self.jurisdiction)

    @property
    def total_refund(self) -> Decimal:
        profile = self.profile
        return sum((line.refund(profile) for line in self.refund_lines), Decimal("0.00"))

    @property
    def theories_permitted(self) -> bool:
        """Whether every match rests on a theory this jurisdiction actually allows.

        Guards the failure that would otherwise be silent and expensive: a substitution
        match reaching a GCC claim, where no substitution theory exists.
        """
        profile = self.profile
        return all(profile.permits(line.match.theory) for line in self.refund_lines)

    @model_validator(mode="after")
    def _lane_belongs_to_jurisdiction(self) -> Self:
        permitted = LANES_BY_JURISDICTION[self.jurisdiction]
        if self.lane not in permitted:
            msg = f"lane {self.lane} is not available in jurisdiction {self.jurisdiction}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _drawback_type_matches_lane(self) -> Self:
        drawback_lanes = {RecoveryLane.DRAWBACK, RecoveryLane.GCC_REEXPORT_DRAWBACK}
        if self.lane in drawback_lanes and self.drawback_type is None:
            msg = f"lane {self.lane} requires a drawback_type"
            raise ValueError(msg)
        if self.lane not in drawback_lanes and self.drawback_type is not None:
            msg = f"drawback_type is meaningless on lane {self.lane}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _no_substitution_outside_us(self) -> Self:
        if self.jurisdiction is not Jurisdiction.US and self.drawback_type in _SUBSTITUTION_TYPES:
            msg = (
                f"{self.drawback_type} is a US-only theory; {self.jurisdiction} "
                "recognises no substitution drawback"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _currency_matches_jurisdiction(self) -> Self:
        expected = profile_for(self.jurisdiction).currency
        if self.currency is not expected:
            msg = f"jurisdiction {self.jurisdiction} files in {expected}, not {self.currency}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _period_ordered(self) -> Self:
        if self.period_end < self.period_start:
            msg = "period_end precedes period_start"
            raise ValueError(msg)
        return self

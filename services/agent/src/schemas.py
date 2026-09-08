"""Output contracts for the agent layer.

Every generation in this service is schema-constrained. Not for tidiness — because an
LLM writing free prose into a customs filing is an unbounded liability, and a schema is
the only thing that makes the output *checkable* rather than merely plausible.

Two rules run through all of these:

**No model-originated figures.** Every quantity, value and rate arrives from the
deterministic core. The narrative fields are prose only, and `grounding.py` enforces it
by rejecting any digit in the generated text that is not traceable to the facts it was
given. This is `CLAUDE.md`'s rule made mechanical: the LLM writes judgment, never
arithmetic.

**No model-originated citations.** The agent selects from a closed set of statutory
references the rules engine already resolved. It cannot cite an authority that was not
handed to it, because a fabricated citation in an audit-facing memo is the single
highest-cost error this system could make.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Recommendation(StrEnum):
    """What the agent thinks the analyst should do. Advisory, never executed.

    The agent proposes; `mcp-claims` still requires a human to call
    `resolve_review_exception`. Nothing here moves a claim by itself.
    """

    APPROVE = "approve"
    """The exception looks resolvable on the record as it stands."""

    CORRECT = "correct"
    """A specific figure or field is wrong and the memo says which."""

    REJECT = "reject"
    """The claim fails on the merits, not on missing information."""

    GATHER = "gather"
    """Undecidable without a document or fact the file does not contain."""

    ESCALATE = "escalate"
    """Needs a licensed broker or counsel, not an analyst."""


class Strength(StrEnum):
    """How well the record supports the position, in the agent's reading."""

    STRONG = "strong"
    ADEQUATE = "adequate"
    WEAK = "weak"
    UNSUPPORTED = "unsupported"
    """The record does not support the position at all. A claim carrying this must not
    be filed on the strength of this memo."""


class InterchangeabilityMemo(BaseModel):
    """A drafted justification for a US §1313(j)(2) substitution pairing.

    Structured rather than a wall of prose so that each element can be checked against
    the record independently, and so a weak section is visible instead of buried.

    Note on the operative test: post-TFTEA, §1313(j)(2) substitution turns on the
    imported and substituted merchandise sharing an 8-digit HTS subheading, with the
    "other" proviso narrowing to the 10-digit statistical number where the 8-digit
    description begins with "other". Commercial interchangeability is *supporting*
    analysis — it is what persuades a reviewing officer the pairing is real — but it is
    not the statutory test, and `statutory_basis` states the test rather than the
    support.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statutory_basis: Annotated[str, Field(min_length=40, max_length=900)]
    """The operative test as applied to this pairing: the shared 8-digit subheading, and
    the "other" proviso where it bites."""

    interchangeability_analysis: Annotated[str, Field(min_length=60, max_length=1600)]
    """Why these goods are commercially equivalent — governmental and recognised industry
    standards, part numbers, tariff classification, relative value."""

    tariff_analysis: Annotated[str, Field(min_length=40, max_length=1200)]
    """What the shared subheading actually covers, and why both articles sit in it."""

    distinguishing_facts: list[Annotated[str, Field(min_length=8, max_length=400)]] = Field(
        default_factory=list, max_length=8
    )
    """Differences between the articles that a reviewing officer will notice. Recorded
    rather than omitted: a memo that lists only favourable facts reads as advocacy, and a
    difference CBP finds unaided is worse than one the claimant raised."""

    strength: Strength
    analyst_note: Annotated[str, Field(min_length=20, max_length=600)]
    """What the drafter could not settle from the record."""

    citations: list[str] = Field(default_factory=list, max_length=12)
    """Selected from the closed set supplied in the prompt. Never composed."""


class ExceptionMemo(BaseModel):
    """Pre-analysis of one `review_queue` row, drafted before an analyst opens it.

    The point is to shorten the analyst's read, not to replace it. The memo says what
    happened, what would settle it, and what the drafter would do — and the recommendation
    is inert until a human acts on it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    headline: Annotated[str, Field(min_length=12, max_length=160)]
    """One line. What an analyst sees in the queue list before opening anything."""

    what_happened: Annotated[str, Field(min_length=40, max_length=1200)]
    """The mechanism, in the vocabulary of the rule that fired."""

    why_it_matters: Annotated[str, Field(min_length=30, max_length=900)]
    """The consequence of getting it wrong, in money or in deadline."""

    recommendation: Recommendation
    rationale: Annotated[str, Field(min_length=30, max_length=1200)]

    checks: list[Annotated[str, Field(min_length=8, max_length=300)]] = Field(
        default_factory=list, max_length=8
    )
    """What the analyst should verify, ordered by what would change the answer fastest."""

    blocking_unknowns: list[Annotated[str, Field(min_length=8, max_length=300)]] = Field(
        default_factory=list, max_length=6
    )
    """Facts absent from the record without which the recommendation cannot be trusted.
    Non-empty is a normal and healthy outcome; a confident memo over a thin record is
    the failure mode."""

    citations: list[str] = Field(default_factory=list, max_length=12)


MEMO_TYPES: dict[str, type[BaseModel]] = {
    "interchangeability": InterchangeabilityMemo,
    "exception": ExceptionMemo,
}

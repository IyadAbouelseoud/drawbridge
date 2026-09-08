"""Drafting the substitution justification for a US §1313(j)(2) claim.

**What the statute actually requires.** Since TFTEA, unused-merchandise substitution
turns on classification: the substituted merchandise must be classifiable under the same
8-digit HTS subheading as the imported merchandise. Where that 8-digit subheading's
article description begins with "other", the test narrows — substitution then requires
the same 10-digit statistical reporting number, and that number must not itself begin
with "other". The pairing either satisfies this or it does not, and the matcher has
already decided which; nothing here can change that answer.

**What the memo is for, then.** Two things the classification test does not do. First,
CBP reviews substitution claims on their commercial reality, and a claimant who can show
the articles are genuinely equivalent — same governmental or industry standard, same
part, comparable value — is a claimant whose file survives scrutiny. Second, the
differences between the articles are going to be found by someone; a memo that names them
first is a stronger document than one that omits them and is caught.

So `statutory_basis` states the operative test and the agent may not soften it, while
`interchangeability_analysis` builds the supporting case. Conflating them would produce a
memo that reads as if commercial equivalence were the legal standard, which it has not
been since 2016.

The agent receives the pairing as the matcher resolved it and writes prose about it. It
does not decide eligibility, does not compute a refund, and — enforced by `grounding` —
cannot write a figure the matcher did not produce.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from services.agent.src import grounding
from services.agent.src.client import generate
from services.agent.src.schemas import InterchangeabilityMemo

SYSTEM = """You are a licensed customs drawback specialist drafting the substitution \
justification that accompanies a US drawback claim under 19 U.S.C. 1313(j)(2).

Write for a CBP drawback reviewing officer. Formal, specific, and defensible on audit \
four years from now.

Absolute constraints:

1. Every quantity, value, rate, date and code you write must appear verbatim in the FACTS \
block. You may not compute, round, approximate, convert or restate any figure. If a figure \
you want is absent, write about it qualitatively or omit the point.

2. You may cite only authorities listed in the FACTS block under "available_citations". \
Do not cite a CROSS ruling, a CFR section or a statute that is not on that list, and do \
not invent a ruling number.

3. state the operative test correctly: post-TFTEA 1313(j)(2) substitution requires the \
same 8-digit HTS subheading, narrowing to the 10-digit statistical reporting number where \
the 8-digit description begins with "other". Commercial interchangeability is supporting \
analysis, not the statutory test. Do not present it as the test.

4. Record differences between the articles in distinguishing_facts. A memo that omits an \
unfavourable fact is worth less than one that addresses it.

5. Where the record does not settle something, say so in analyst_note. Do not fill a gap \
with a confident sentence. An honest "the record does not establish X" is the useful \
output; a plausible assertion is the damaging one.

Set strength to UNSUPPORTED if the record does not carry the position. That verdict is a \
correct answer, not a failure."""

INSTRUCTION = (
    "Draft the substitution justification for the pairing below. Address the "
    "classification test first, then the commercial case, then what a reviewing officer "
    "would push back on."
)


def build_facts(
    *,
    imported: dict[str, Any],
    substituted: dict[str, Any],
    theory: str,
    subheading_8: str,
    subheading_begins_with_other: bool,
    statistical_10: str | None = None,
    quantity: str | None = None,
    duty_allocated: str | None = None,
    refund_amount: str | None = None,
    rulings: Sequence[dict[str, Any]] = (),
    citations: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble the record the drafter is allowed to see.

    Explicit rather than passing a domain object through: the fact set doubles as the
    grounding allowlist, so anything reachable here is a number the memo may contain.
    Handing over a whole `LineMatch` would silently widen that allowlist to every internal
    identifier it carries.
    """
    return {
        "statutory_theory": theory,
        "imported_merchandise": imported,
        "substituted_merchandise": substituted,
        "classification": {
            "shared_8_digit_subheading": subheading_8,
            "subheading_description_begins_with_other": subheading_begins_with_other,
            "statistical_reporting_number": statistical_10,
            "other_proviso_applies": subheading_begins_with_other,
        },
        "quantities": {
            "designated_quantity": quantity,
            "duty_allocated": duty_allocated,
            "refund_amount": refund_amount,
        },
        "supporting_rulings": list(rulings),
        "available_citations": list(citations),
    }


def draft(facts: dict[str, Any], **kwargs: Any) -> InterchangeabilityMemo:
    """Generate and ground-check one interchangeability memo.

    Raises `grounding.UngroundedFigureError` if the narrative contains a figure the facts do
    not carry, and `AgentRefusedError` if the output does not satisfy the schema. Both are
    surfaced rather than swallowed: a memo that fails either check must not reach a
    filing, and a caller silently substituting an empty memo would hide a real defect.
    """
    memo = generate(
        output_model=InterchangeabilityMemo,
        system=SYSTEM,
        facts=facts,
        instruction=INSTRUCTION,
        **kwargs,
    )
    allowed = grounding.allowed_figures(facts)
    grounding.check_model(memo, allowed)
    grounding.check_citations(memo.citations, facts)
    return memo

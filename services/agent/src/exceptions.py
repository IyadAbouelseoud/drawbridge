"""Pre-analysis for `review_queue` rows.

An analyst opening the queue currently reads a reason code, a summary, and whatever the
matcher dumped into `payload`. That is enough to work from and slow to work through:
reconstructing *why* the rule fired, and what would settle it, is the same reasoning
every time for a given reason code.

So the memo is drafted when the row is created, and is waiting when the analyst arrives.
It is advisory in the strict sense — `recommendation` is a field on a row, and only a
human calling `resolve_review_exception` moves anything. Nothing in this module writes
claim state.

**Per-reason framing.** Each `ReviewReason` gets its own guidance, because the
question genuinely differs. A near-miss on the GCC USD 5,000 threshold is an arithmetic
and valuation question with a bright-line answer; a superseded CROSS ruling is a question
about whether the superseding ruling changes the classification conclusion; a
low-confidence OCR field is a question about one glyph. A single generic prompt would
produce a memo that is fluent about all three and useful for none.

**What is deliberately withheld.** The prompt gets the payload minus keys named in
`REDACTED_KEYS`. The review payload is a dumping ground by design — it carries whatever
the matcher had — and the set of things that should not leave the deployment will grow
faster than anyone remembers to check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from services.agent.src import grounding
from services.agent.src.client import generate, redact
from services.agent.src.schemas import ExceptionMemo
from services.rules.src.triage import ReviewReason

if TYPE_CHECKING:
    from collections.abc import Sequence

# Never sent to the model. Not because any of these is presently sensitive, but because
# the payload is open-ended and an allow-by-default posture on an open-ended structure
# eventually sends something it should not.
REDACTED_KEYS: tuple[str, ...] = (
    "resume_token",
    "api_key",
    "credentials",
    "raw_document",
    "document_bytes",
    "tenant_contact",
)

SYSTEM = """You are a senior customs analyst triaging an exception in a duty-recovery \
system, writing a short memo for the analyst who will actually resolve it.

Your memo is advisory. You do not resolve anything; a human does. Write what you would \
tell a competent colleague who is about to open this file.

Absolute constraints:

1. Every figure you write — quantity, value, rate, date, confidence score, code — must \
appear verbatim in the FACTS block. Never compute, round, convert or approximate. If you \
want a figure that is not there, describe it qualitatively instead.

2. Cite only authorities listed under "available_citations". Never invent a CFR section, \
a ruling number or an article number.

3. If the record does not support a recommendation, say GATHER and list precisely what is \
missing in blocking_unknowns. A confident recommendation over a thin record is the worst \
output you can produce here — it is the one an analyst is most likely to accept without \
checking.

4. Be short. The analyst is reading this to decide where to look, not instead of looking.

5. Where a citation in the record is marked ANALYST_REVIEW, treat the article number as \
unknown and say so. Do not supply the number you think it probably is."""

# One framing per reason. The generic version of this prompt produces confident prose
# about the wrong question.
GUIDANCE: dict[ReviewReason, str] = {
    ReviewReason.SOLVER_NOT_OPTIMAL: (
        "The allocator returned a feasible but not provably optimal assignment. The "
        "question is whether the gap is material to the refund, and whether any "
        "designation in it would be hard to defend on its own terms."
    ),
    ReviewReason.SOLVER_INFEASIBLE: (
        "No assignment satisfied the constraints. The useful question is which "
        "constraint is binding — an expired window, an exhausted import line, a missing "
        "substitution key — because that determines whether the claim is dead or merely "
        "mis-scoped."
    ),
    ReviewReason.LOW_EXTRACTION_CONFIDENCE: (
        "A field came off the document below the confidence floor. Say which field, what "
        "the reading was, and what a wrong reading would do downstream. A misread digit "
        "in a duty amount and a misread character in a description are not the same "
        "severity."
    ),
    ReviewReason.THRESHOLD_NEAR_MISS: (
        "A GCC claim landed near the Article 16(2) USD 5,000 minimum. The question is "
        "whether the valuation basis and the conversion date are right, since both move "
        "the figure across the line, and whether other consignments are aggregable."
    ),
    ReviewReason.RATE_UNAVAILABLE: (
        "No duty rate was on file for the line. Establish whether the rate is genuinely "
        "absent from the schedule, or the code did not resolve, or the line carries a "
        "compound rate the parser does not represent."
    ),
    ReviewReason.UNKNOWN_FIELD_LABEL: (
        "Extraction met a field label it does not map. Whether this is a new form "
        "revision or a one-off scan artefact determines whether the fix is this claim or "
        "the extractor."
    ),
    ReviewReason.JURISDICTION_AMBIGUOUS: (
        "The claim did not route cleanly to a jurisdiction. This is consequential: the "
        "US and GCC lanes have different theories, clocks and thresholds, so routing it "
        "wrongly does not produce a slightly wrong claim, it produces an inapplicable one."
    ),
    ReviewReason.DEADLINE_IMMINENT: (
        "A statutory window is close. State the anchor date, the window, and what is "
        "still outstanding. Deadlines here do not extend, so sequencing advice is the "
        "most valuable thing in the memo."
    ),
}

INSTRUCTION_HEAD = "Draft the pre-analysis memo for the exception below."


def build_facts(
    *,
    reason: ReviewReason,
    severity: str,
    summary: str,
    payload: dict[str, Any],
    citation: str | None = None,
    citations: Sequence[str] = (),
    superseded_rulings: Sequence[dict[str, Any]] = (),
    ocr_readings: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The record the drafter sees, with the withheld keys already gone."""
    available = [str(c) for c in citations]
    if citation and citation not in available:
        available.append(citation)
    return {
        "reason": reason.value,
        "severity": severity,
        "summary": summary,
        "matcher_payload": redact(payload, REDACTED_KEYS),
        "superseded_rulings": list(superseded_rulings),
        "ocr_readings": list(ocr_readings),
        "available_citations": available,
    }


def draft(facts: dict[str, Any], **kwargs: Any) -> ExceptionMemo:
    """Generate and ground-check one exception memo."""
    reason = ReviewReason(facts["reason"])
    instruction = f"{INSTRUCTION_HEAD}\n\n{GUIDANCE[reason]}"

    memo = generate(
        output_model=ExceptionMemo,
        system=SYSTEM,
        facts=facts,
        instruction=instruction,
        **kwargs,
    )
    grounding.check_model(memo, grounding.allowed_figures(facts))
    grounding.check_citations(memo.citations, facts)
    return memo

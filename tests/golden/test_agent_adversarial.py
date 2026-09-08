"""The guards, attacked deliberately.

`test_agent.py` establishes that the guards work on the failures a fluent model actually
produces — a rounded figure, a transposed digit. This file is the adversarial half: every
memo here is written to get a fabrication past the guard, and every test asserts the
attempt fails *closed*, with a named exception rather than a degraded memo.

The distinction the file is built around: **fails closed** means no memo. Not a memo with
the offending sentence stripped, not a memo with a warning attached, not an empty memo
standing in for a real one. A caller receiving `None` where it expected a memo would write
NULL to `review_queue.agent_memo` and the analyst would read the row unaided, which is how
the system worked before the agent existed. A caller receiving a *repaired* memo would
read prose that a guard has already caught lying once.

Two error types, kept distinct because they mean different things about the model:
`UngroundedFigureError` — it invented arithmetic. `FabricatedCitationError` — it invented
an authority. The second is the more expensive error in an audit and the rate at which it
happens is worth watching separately, which is impossible if both arrive as `ValueError`.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.agent.src import exceptions, interchangeability
from services.agent.src.grounding import (
    FabricatedCitationError,
    UngroundedFigureError,
    allowed_figures,
)
from services.agent.src.schemas import ExceptionMemo, InterchangeabilityMemo
from services.rules.src.triage import ReviewReason
from tests.golden.test_agent import EXCEPTION_MEMO, FACTS, GOOD_MEMO, QUEUE_ROW, StubClient

# One stub, shared with test_agent.py. A second copy would drift, and the guardrail tests
# depend on the stub recording the *request* faithfully.


def _memo(**overrides: Any) -> dict[str, Any]:
    return {**GOOD_MEMO, **overrides}


def _exception_memo(**overrides: Any) -> dict[str, Any]:
    return {**EXCEPTION_MEMO, **overrides}


def _exception_facts(**overrides: Any) -> dict[str, Any]:
    facts = exceptions.build_facts(
        reason=ReviewReason(QUEUE_ROW["reason"]),
        severity=str(QUEUE_ROW["severity"]),
        summary=str(QUEUE_ROW["summary"]),
        payload=dict(QUEUE_ROW["payload"]),  # type: ignore[arg-type]
        citation=str(QUEUE_ROW["citation"]),
    )
    return {**facts, **overrides}


# --------------------------------------------------------------- fabricated figures

# Each entry is a hallucination aimed at a different field of the memo, because a guard
# that scans the first paragraph and stops is a guard that passes everything written
# after it. The figures are chosen to be plausible: an entry number in CBP's own format,
# a duty amount within a percent of the real one, a rate that reads like a tariff rate.
FABRICATED_FIGURES: list[tuple[str, str, str]] = [
    (
        "duty amount",
        "statutory_basis",
        "Both articles fall in HTS subheading 84821010 and the duty allocated to this "
        "designation is 4,913.75 under the post-TFTEA substitution standard.",
    ),
    (
        "entry number",
        "tariff_analysis",
        "Subheading 84821010 covers deep groove ball bearings, as entered on consumption "
        "entry 231-4471902-6 at the port of arrival.",
    ),
    (
        "ad valorem rate",
        "interchangeability_analysis",
        "The two articles are commercially equivalent bearings of one grade, and both "
        "carry the same general rate of 9.9 percent, so neither enjoys a duty advantage "
        "over the other in the market they compete in.",
    ),
    (
        "quantity",
        "analyst_note",
        "The record does not show whether all 12,750 units were of one production lot.",
    ),
    (
        "date",
        "distinguishing_facts",
        "The substituted article entered under a specification revised on 2024-03-17.",
    ),
]


class TestFabricatedFiguresFailClosed:
    @pytest.mark.parametrize(
        ("label", "field", "text"),
        [pytest.param(*case, id=f"{case[0]} in {case[1]}") for case in FABRICATED_FIGURES],
    )
    def test_an_invented_figure_is_refused_wherever_it_appears(
        self, label: str, field: str, text: str
    ) -> None:
        value: Any = [text] if field == "distinguishing_facts" else text
        client = StubClient(_memo(**{field: value}))

        with pytest.raises(UngroundedFigureError) as caught:
            interchangeability.draft(FACTS, client=client)

        assert caught.value.field.startswith(field), label
        assert caught.value.tokens

    def test_the_error_names_every_offending_token_not_just_the_first(self) -> None:
        """One round trip per token would make diagnosis proportional to the damage."""
        client = StubClient(
            _memo(
                analyst_note=(
                    "The designation covers 12,750 units against duty of 4,913.75, "
                    "neither of which the file confirms."
                )
            )
        )
        with pytest.raises(UngroundedFigureError) as caught:
            interchangeability.draft(FACTS, client=client)
        assert caught.value.tokens == ["12750", "4913.75"]

    def test_arabic_indic_digits_do_not_get_a_hallucination_past_the_fold(self) -> None:
        """The folding that lets a correct figure be restated must not become a bypass.

        `test_agent.py` proves ٤٨١٢٫٥٠ is accepted as a restatement of 4,812.50. The
        complement matters more: a *wrong* figure in the same script is still wrong, and
        a guard that normalised only in the permissive direction would be a hole shaped
        exactly like the ZATCA lane.
        """
        client = StubClient(
            _memo(analyst_note="The duty allocated to this designation is ٤٩١٣٫٧٥ per the file.")
        )
        with pytest.raises(UngroundedFigureError) as caught:
            interchangeability.draft(FACTS, client=client)
        assert "4913.75" in caught.value.tokens

    def test_a_figure_hidden_in_the_last_list_item_is_still_scanned(self) -> None:
        """Bullets are skimmed, which makes them the best place to hide a number."""
        client = StubClient(
            _memo(
                distinguishing_facts=[
                    "The substituted article carries a different part number.",
                    "Unit values differ, though modestly.",
                    "The substituted lot was invoiced at 4.97 per unit.",
                ]
            )
        )
        with pytest.raises(UngroundedFigureError) as caught:
            interchangeability.draft(FACTS, client=client)
        assert caught.value.field == "distinguishing_facts[2]"

    def test_an_exception_memo_is_held_to_the_same_standard(self) -> None:
        """The memo an analyst reads to decide, not the one an agency reads.

        A fabricated figure here never reaches a filing directly — it steers the person
        who signs one, which is not obviously better.
        """
        client = StubClient(
            _exception_memo(
                why_it_matters=(
                    "A misread digit here moves the refund by 8,240.00 and the error "
                    "would not surface until the agency reconciles the entry."
                )
            )
        )
        with pytest.raises(UngroundedFigureError):
            exceptions.draft(_exception_facts(), client=client)

    def test_a_figure_spelled_out_in_words_is_a_known_gap(self) -> None:
        """Recorded rather than claimed away.

        The guard scans numeric tokens. "four thousand nine hundred" carries no digit and
        passes. This is a real residual risk and the honest place for it is a test that
        names it — a suite that asserted total coverage would be making a claim the
        implementation does not support.

        It is bounded by the prompt: the model is given the figures as a JSON block and
        told to quote them, and prose-spelled customs amounts are not a form the source
        material uses. Closing it properly means a number-word parser per language, which
        is a larger piece of work than the risk currently justifies.
        """
        client = StubClient(
            _memo(analyst_note="The duty allocated is roughly four thousand nine hundred dollars.")
        )
        memo = interchangeability.draft(FACTS, client=client)
        assert isinstance(memo, InterchangeabilityMemo)


# ------------------------------------------------------------ fabricated authorities

# Each is a real-looking citation of a kind the system genuinely uses, and none was
# supplied. A plausible authority is the dangerous one: nobody checks 19 CFR §191.32
# because it reads exactly like something that would exist.
FABRICATED_CITATIONS: list[tuple[str, str]] = [
    ("non-existent CFR section", "19 CFR §190.203"),
    ("repealed part cited as current", "19 CFR §191.32"),
    ("invented CROSS ruling", "HQ H301884"),
    ("invented statutory subsection", "19 U.S.C. §1313(j)(4)"),
    ("unverified ZATCA article", "ZATCA Administrative Decision 28624 Art. 7"),
    ("invented GCC article", "GCC Common Customs Law Art. 103"),
]


class TestFabricatedAuthoritiesFailClosed:
    @pytest.mark.parametrize(
        ("label", "citation"),
        [pytest.param(*case, id=case[0]) for case in FABRICATED_CITATIONS],
    )
    def test_an_unsupplied_authority_is_refused(self, label: str, citation: str) -> None:
        client = StubClient(_memo(citations=["19 U.S.C. §1313(j)(2)", citation]))

        with pytest.raises(FabricatedCitationError) as caught:
            interchangeability.draft(FACTS, client=client)

        assert caught.value.invented == [citation], label

    @pytest.mark.parametrize(
        ("label", "citation"),
        [pytest.param(*case, id=case[0]) for case in FABRICATED_CITATIONS],
    )
    def test_the_exception_drafter_refuses_it_too(self, label: str, citation: str) -> None:
        """Both drafters, because the check used to live in only one.

        Week 8 shipped the citation check inside `interchangeability` alone; an exception
        memo could invent a CFR section freely. The check is shared now, and the way that
        stays true is a test that would fail if it were copied back.
        """
        client = StubClient(_exception_memo(citations=[citation]))
        with pytest.raises(FabricatedCitationError) as caught:
            exceptions.draft(_exception_facts(), client=client)
        assert caught.value.invented == [citation], label

    def test_the_error_reports_what_was_available(self) -> None:
        """So the failure can be read without re-running the generation.

        Whether the model invented an authority or the drafter forgot to supply a real one
        are different bugs with the same symptom, and only the available set tells them
        apart.
        """
        client = StubClient(_memo(citations=["19 CFR §190.203"]))
        with pytest.raises(FabricatedCitationError) as caught:
            interchangeability.draft(FACTS, client=client)
        assert "19 U.S.C. §1313(j)(2)" in caught.value.available

    def test_a_zatca_article_number_is_refusable_because_none_are_supplied(self) -> None:
        """The Resolution 28624 posture, enforced at the agent boundary.

        `COMPLIANCE-GCC.md` §8.4 records that the operative article numbers could not be
        obtained, so `services/packager/src/citations.py` emits an ANALYST_REVIEW
        placeholder instead. The agent must not quietly supply what the packager refuses
        to: an article number no one has verified, in a memo, reads as settled law.
        """
        facts = _exception_facts(
            available_citations=["GCC Common Customs Law Art. 97"],
            citation="GCC Common Customs Law Art. 97",
        )
        client = StubClient(
            _exception_memo(citations=["ZATCA Administrative Decision 28624 Art. 4"])
        )
        with pytest.raises(FabricatedCitationError):
            exceptions.draft(facts, client=client)

    def test_a_supplied_authority_still_passes(self) -> None:
        """The guard has to let correct output through or it will be turned off.

        Week 8 shipped a version that rejected a correctly-quoted `19 CFR §163.1` as three
        invented numbers. A guard that fails closed on correct work does not survive
        contact with the people using it.
        """
        memo = exceptions.draft(_exception_facts(), client=StubClient(EXCEPTION_MEMO))
        assert isinstance(memo, ExceptionMemo)
        assert memo.citations == ["19 CFR §163.1"]


# ------------------------------------------------------------------- failing closed


class TestNothingPartialEscapes:
    def test_a_rejected_memo_is_not_returned_in_any_form(self) -> None:
        """No repaired memo, no empty memo, no `None`. An exception.

        The tempting alternative — strip the bad sentence and return the rest — would
        hand an analyst prose that has already been caught fabricating once, with the
        evidence of that removed.
        """
        client = StubClient(_memo(analyst_note="Duty of 9,999.99 was allocated here."))
        with pytest.raises(UngroundedFigureError):
            interchangeability.draft(FACTS, client=client)

    def test_a_clean_narrative_with_one_bad_citation_is_still_refused(self) -> None:
        """Both guards run. Passing one is not passing."""
        client = StubClient(_memo(citations=["19 CFR §190.203"]))
        with pytest.raises(FabricatedCitationError):
            interchangeability.draft(FACTS, client=client)

    def test_the_two_failures_are_distinguishable_by_type(self) -> None:
        """Same base class for callers that treat them alike, distinct types for callers
        that do not — chiefly the queue worker, which records why a row went undrafted."""
        assert issubclass(UngroundedFigureError, ValueError)
        assert issubclass(FabricatedCitationError, ValueError)
        assert not issubclass(FabricatedCitationError, UngroundedFigureError)
        assert not issubclass(UngroundedFigureError, FabricatedCitationError)

    def test_the_allowlist_does_not_widen_when_the_prompt_does(self) -> None:
        """The guard reads the same structure the model reads.

        A fact added to the payload becomes quotable automatically; a fact *not* added
        stays unquotable. The alternative — a curated allowlist — drifts out of step with
        the prompt and then rejects correct memos, at which point it gets deleted.
        """
        narrow = allowed_figures(FACTS)
        widened = allowed_figures({**FACTS, "extra": {"port_code": "2704"}})
        assert "2704" not in narrow
        assert "2704" in widened

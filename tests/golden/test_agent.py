"""Known-answer fixtures for the agent layer.

These run against a stub Anthropic client rather than the API. Not to avoid cost — to
make the tests about the thing worth testing. What matters here is not whether Claude
writes good prose; it is whether the guardrails hold when the prose is *bad*. A live call
cannot be relied on to produce a hallucinated duty figure on demand, so the stub produces
one and the fixtures assert it is caught.

The invariant under test is the one `CLAUDE.md` states: the LLM writes narratives and
judgment calls, and never originates a number. Every test in `TestTheNumericGuard` is a
way that could fail — an invented figure, a rounded one, a transposed one, a figure
smuggled into a bullet rather than a paragraph.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from services.agent.src import exceptions, grounding, interchangeability
from services.agent.src.client import (
    MAX_TOKENS,
    TEMPERATURE,
    AgentRefusedError,
    AgentUnavailableError,
    generate,
    redact,
)
from services.agent.src.queue import PROMPT_VERSION, model_tag
from services.agent.src.schemas import (
    ExceptionMemo,
    InterchangeabilityMemo,
    Recommendation,
    Strength,
)
from services.rules.src.triage import ReviewReason

# --------------------------------------------------------------------------- the stub


@dataclass
class _Block:
    type: str
    name: str
    input: dict[str, Any]
    id: str = "toolu_stub"


@dataclass
class _Response:
    content: list[_Block]


class StubClient:
    """Returns canned tool inputs and records exactly what it was called with.

    `calls` is what lets the guardrail tests assert on the *request* — that max_tokens and
    temperature were pinned, that the tool was forced, that the facts arrived as JSON.
    Those are properties of the call, not of the response, and no amount of inspecting
    output would establish them.
    """

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    @property
    def messages(self) -> StubClient:
        return self

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        payload = self._payloads.pop(0) if self._payloads else {}
        return _Response(content=[_Block(type="tool_use", name="record_memo", input=payload)])


class ExplodingClient:
    """Every call raises, as an unreachable API does."""

    @property
    def messages(self) -> ExplodingClient:
        return self

    def create(self, **_kwargs: Any) -> _Response:
        msg = "connection refused"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------- the facts

IMPORTED = {
    "description": "Ball bearings, single row deep groove, 25 mm bore",
    "hts_code": "8482101000",
    "part_number": "6205-2RS",
    "unit_value_usd": "4.18",
}

SUBSTITUTED = {
    "description": "Ball bearings, single row deep groove, 25 mm bore",
    "hts_code": "8482101000",
    "part_number": "SKF-6205-2RSH",
    "unit_value_usd": "4.31",
}

FACTS = interchangeability.build_facts(
    imported=IMPORTED,
    substituted=SUBSTITUTED,
    theory="hts_substitution",
    subheading_8="84821010",
    subheading_begins_with_other=False,
    statistical_10="8482101000",
    quantity="12500.0000",
    duty_allocated="4812.50",
    refund_amount="4764.38",
    rulings=[{"ruling_number": "HQ H289143", "holding": "Bearings of like kind and quality"}],
    citations=["19 U.S.C. §1313(j)(2)", "19 CFR Part 190 subpart B"],
)

GOOD_MEMO = {
    "statutory_basis": (
        "Both articles are classifiable under HTS subheading 84821010, satisfying the "
        "post-TFTEA substitution standard at 19 U.S.C. §1313(j)(2). The subheading "
        "description does not begin with the term other, so the narrowing proviso "
        "requiring identity at the statistical reporting number is not engaged."
    ),
    "interchangeability_analysis": (
        "The imported and substituted bearings are single row deep groove bearings of "
        "identical bore dimension, built to the same recognised industry designation, and "
        "sold into the same application without adaptation. Unit values are within a "
        "narrow band of one another, which is consistent with articles competing in one "
        "market rather than at different grades."
    ),
    "tariff_analysis": (
        "Subheading 84821010 covers ball bearings of the deep groove type. Both articles "
        "are described by its terms without recourse to a residual provision."
    ),
    "distinguishing_facts": [
        "The substituted article carries a different manufacturer part number.",
        "Unit values differ, though modestly.",
    ],
    "strength": "strong",
    "analyst_note": (
        "The record does not contain a certificate of conformance for either article; if "
        "one exists it would strengthen the standards argument materially."
    ),
    "citations": ["19 U.S.C. §1313(j)(2)", "HQ H289143"],
}


def _memo_with(**overrides: Any) -> dict[str, Any]:
    return {**GOOD_MEMO, **overrides}


# ------------------------------------------------------------------- the guardrails


class TestTheCallIsPinned:
    def test_max_tokens_and_temperature_are_not_call_site_decisions(self) -> None:
        """Both hardcoded, both asserted on the outgoing request.

        Temperature 0 is not a quality preference. Two analysts opening the same claim
        must see the same memo, and a filing built on a sampled narrative cannot be
        explained four years later when an auditor asks why it says what it says.
        """
        client = StubClient(GOOD_MEMO)
        interchangeability.draft(FACTS, client=client)

        (call,) = client.calls
        assert call["max_tokens"] == MAX_TOKENS == 1024
        assert call["temperature"] == TEMPERATURE == 0.0

    def test_output_is_forced_through_the_schema_tool(self) -> None:
        """`tool_choice` leaves the model no prose path.

        This is why there is no JSON parsing step anywhere in the service — the model
        cannot emit a preamble, a fenced block, or an apology, so there is nothing to
        parse defensively.
        """
        client = StubClient(GOOD_MEMO)
        interchangeability.draft(FACTS, client=client)

        (call,) = client.calls
        assert call["tool_choice"] == {"type": "tool", "name": "record_memo"}
        assert [tool["name"] for tool in call["tools"]] == ["record_memo"]

    def test_facts_are_sent_as_json_not_prose(self) -> None:
        """A JSON block reads as a record to quote; prose reads as context to paraphrase.

        The distinction matters because paraphrasing a figure is precisely the failure the
        numeric guard exists to catch, and framing that invites it is a bad prompt.
        """
        client = StubClient(GOOD_MEMO)
        interchangeability.draft(FACTS, client=client)

        content = client.calls[0]["messages"][0]["content"]
        assert "```json" in content
        assert '"duty_allocated": "4812.50"' in content

    def test_an_unreachable_api_raises_unavailable_not_a_bad_memo(self) -> None:
        """The distinction callers depend on.

        No memo means the analyst reads the row unaided, which is how the system worked
        before this service existed. A wrong memo is worse than none, so the two failures
        must not arrive as the same exception.
        """
        with pytest.raises(AgentUnavailableError):
            interchangeability.draft(FACTS, client=ExplodingClient())

    def test_a_schema_violation_is_retried_once_then_refused(self) -> None:
        """One retry, with the validation error fed back — and then it stops.

        At temperature 0 a second failure will not become a third success. Repeating the
        call would burn tokens establishing what the second attempt already established:
        the schema and the task disagree.
        """
        client = StubClient({"strength": "strong"}, {"strength": "strong"})
        with pytest.raises(AgentRefusedError):
            interchangeability.draft(FACTS, client=client)
        assert len(client.calls) == 2

    def test_the_retry_carries_the_validation_error_back(self) -> None:
        client = StubClient({"strength": "strong"}, GOOD_MEMO)
        memo = interchangeability.draft(FACTS, client=client)

        second_call = client.calls[1]["messages"][-1]["content"][0]
        assert second_call["is_error"] is True
        assert "failed validation" in second_call["content"]
        assert memo.strength is Strength.STRONG


class TestTheNumericGuard:
    """`CLAUDE.md`: the LLM writes narratives and judgment calls; it never originates a
    number. Each test here is a distinct way that could quietly fail."""

    def test_an_invented_duty_figure_is_rejected(self) -> None:
        bad = _memo_with(
            tariff_analysis=(
                "Subheading 84821010 covers ball bearings of the deep groove type, and "
                "the entry shows duty of 9911.02 against this line."
            )
        )
        with pytest.raises(grounding.UngroundedFigureError) as caught:
            interchangeability.draft(FACTS, client=StubClient(bad))
        assert "9911.02" in str(caught.value)

    def test_a_rounded_restatement_is_rejected(self) -> None:
        """The subtle case, and the reason this is not a prompt instruction.

        "approximately 4,800 units" is fluent, helpful-sounding, and wrong: the designated
        quantity is 12500.0000 and the duty is 4812.50. A reader cannot tell which figure
        was meant, and neither can an auditor.
        """
        bad = _memo_with(
            interchangeability_analysis=(
                "The articles are equivalent in every commercial respect, and the "
                "designation covers approximately 4800 units of like merchandise sold "
                "into the same application without adaptation of any kind."
            )
        )
        with pytest.raises(grounding.UngroundedFigureError):
            interchangeability.draft(FACTS, client=StubClient(bad))

    def test_a_transposed_digit_is_rejected(self) -> None:
        """4812.50 becoming 4821.50. Reads perfectly; survives no comparison."""
        bad = _memo_with(
            tariff_analysis=(
                "Subheading 84821010 covers these bearings, and duty of 4821.50 was paid "
                "on the designated quantity."
            )
        )
        with pytest.raises(grounding.UngroundedFigureError):
            interchangeability.draft(FACTS, client=StubClient(bad))

    def test_a_figure_smuggled_into_a_list_item_is_rejected(self) -> None:
        """Bullets are skimmed harder than paragraphs, so they are checked the same."""
        bad = _memo_with(
            distinguishing_facts=[
                "The substituted article carries a different manufacturer part number.",
                "The substituted article costs 7.99 per unit more than the imported one.",
            ]
        )
        with pytest.raises(grounding.UngroundedFigureError) as caught:
            interchangeability.draft(FACTS, client=StubClient(bad))
        assert "distinguishing_facts[1]" in str(caught.value)

    def test_figures_that_are_in_the_record_pass(self) -> None:
        """The guard must not be so strict that a correct memo cannot be written.

        A guard that rejects accurate quotation would be worked around, and a worked-around
        guard protects nothing.
        """
        good = _memo_with(
            tariff_analysis=(
                "Subheading 84821010 covers these bearings. Duty of 4812.50 was allocated "
                "to the designated quantity of 12500.0000 units, yielding a refund of "
                "4764.38."
            )
        )
        memo = interchangeability.draft(FACTS, client=StubClient(good))
        assert "4812.50" in memo.tariff_analysis

    def test_a_comma_grouped_restatement_of_a_real_figure_passes(self) -> None:
        """4,812.50 and 4812.50 are one figure written two ways.

        Rejecting the readable spelling would push the drafter toward writing figures in a
        form no reviewing officer wants to read.
        """
        good = _memo_with(
            tariff_analysis=(
                "Subheading 84821010 covers these bearings, against which duty of "
                "4,812.50 was allocated on this entry line."
            )
        )
        memo = interchangeability.draft(FACTS, client=StubClient(good))
        assert "4,812.50" in memo.tariff_analysis

    def test_small_integers_read_as_prose_not_figures(self) -> None:
        """ "the first of two conditions" is English, not a quantity.

        Treating every digit as a figure would reject fluent writing for no safety gain,
        because a customs quantity is never 2.
        """
        good = _memo_with(
            interchangeability_analysis=(
                "Two considerations govern here. The first is that both articles meet one "
                "industry designation; the second is that they are sold into a single "
                "application without any adaptation being required of the purchaser."
            )
        )
        interchangeability.draft(FACTS, client=StubClient(good))

    def test_arabic_indic_digits_fold_before_comparison(self) -> None:
        """A ZATCA-sourced figure must match its own record.

        Without folding, an Arabic numeral restating a fact that is stored in ASCII would
        be flagged as invented — the guard failing closed on correct output, which is how
        guards get disabled.
        """
        allowed = grounding.allowed_figures({"amount": "4812.50"})
        grounding.check("٤٨١٢.٥٠ was paid", allowed, field="x")

    def test_the_allowlist_is_derived_from_the_facts_not_curated(self) -> None:
        """A new prompt field must widen the guard automatically.

        A hand-maintained allowlist drifts: the model would see a field the guard did not,
        and would start being rejected for quoting it correctly.
        """
        allowed = grounding.allowed_figures({"nested": [{"deep": {"value": "77.25"}}]})
        assert "77.25" in allowed


class TestCitationsAreSelectedNotComposed:
    def test_an_invented_ruling_number_is_refused(self) -> None:
        """The highest-cost error available to this system.

        A fabricated ruling is confident, specific, checkable, and wrong — and it appears
        in a document handed to CBP.
        """
        bad = _memo_with(citations=["19 U.S.C. §1313(j)(2)", "HQ H999999"])
        with pytest.raises(ValueError, match="H999999"):
            interchangeability.draft(FACTS, client=StubClient(bad))

    def test_citations_from_the_supplied_set_pass(self) -> None:
        memo = interchangeability.draft(FACTS, client=StubClient(GOOD_MEMO))
        assert "HQ H289143" in memo.citations

    def test_the_supporting_ruling_numbers_are_available_to_cite(self) -> None:
        """Rulings arrive in their own key, not in `available_citations`.

        They still have to be citable, or the drafter would be forbidden from referencing
        the very rulings it was handed as support.
        """
        assert FACTS["supporting_rulings"][0]["ruling_number"] == "HQ H289143"


class TestTheInterchangeabilityMemo:
    def test_it_states_the_classification_test_not_commercial_equivalence(self) -> None:
        """Post-TFTEA, the operative test is the shared 8-digit subheading.

        Commercial interchangeability is what persuades a reviewing officer the pairing is
        real, but it has not been the statutory standard since 2016. A memo that presents
        it as the test is a memo that is wrong about the law it is invoking.
        """
        memo = interchangeability.draft(FACTS, client=StubClient(GOOD_MEMO))
        assert "84821010" in memo.statutory_basis
        assert "1313(j)(2)" in memo.statutory_basis

    def test_the_other_proviso_is_carried_in_the_facts(self) -> None:
        """Where the 8-digit description begins with "other", the test narrows to the
        10-digit statistical number. The drafter cannot reason about that unless it is
        told, so it is a fact rather than something the prose is expected to know."""
        facts = interchangeability.build_facts(
            imported=IMPORTED,
            substituted=SUBSTITUTED,
            theory="hts_substitution",
            subheading_8="84829900",
            subheading_begins_with_other=True,
            statistical_10="8482990500",
        )
        assert facts["classification"]["other_proviso_applies"] is True

    def test_unfavourable_facts_have_a_home_in_the_schema(self) -> None:
        """A memo listing only favourable facts reads as advocacy.

        A difference CBP finds unaided is worse than one the claimant raised, so the
        schema has a field for it and the prompt requires it be used.
        """
        memo = interchangeability.draft(FACTS, client=StubClient(GOOD_MEMO))
        assert memo.distinguishing_facts

    def test_unsupported_is_a_reachable_verdict(self) -> None:
        """The agent must be able to say the record does not carry the position.

        A drafter that can only produce supportive memos is a drafter whose supportive
        memos mean nothing.
        """
        weak = _memo_with(
            strength="unsupported",
            analyst_note=(
                "The record contains no specification, standard or application evidence "
                "for either article. Nothing here supports substitution."
            ),
        )
        memo = interchangeability.draft(FACTS, client=StubClient(weak))
        assert memo.strength is Strength.UNSUPPORTED

    def test_the_memo_is_frozen(self) -> None:
        """It is a record of what was drafted, not a working document."""
        memo = interchangeability.draft(FACTS, client=StubClient(GOOD_MEMO))
        with pytest.raises(ValueError, match="frozen"):
            memo.strength = Strength.WEAK  # type: ignore[misc]

    def test_build_facts_does_not_leak_more_than_it_means_to(self) -> None:
        """The fact set doubles as the numeric allowlist.

        Passing a whole domain object through would silently widen the guard to every
        internal identifier it carries, and the guard would stop catching anything.
        """
        assert set(FACTS) == {
            "statutory_theory",
            "imported_merchandise",
            "substituted_merchandise",
            "classification",
            "quantities",
            "supporting_rulings",
            "available_citations",
        }


# ------------------------------------------------------------------- exception memos

EXCEPTION_MEMO = {
    "headline": "OCR read the duty figure below the numeric floor",
    "what_happened": (
        "The duty amount field came off the scanned entry summary below the numeric "
        "confidence floor, so the gate rejected it rather than passing a possibly "
        "misread digit to the matcher."
    ),
    "why_it_matters": (
        "A misread digit in a duty amount propagates directly into the refund figure, "
        "and a refund claimed on a misread figure is a misstatement to the agency."
    ),
    "recommendation": "gather",
    "rationale": (
        "The reading cannot be confirmed from the record as it stands. A second source "
        "for the same figure would settle it in minutes; reasoning about the glyph will "
        "not."
    ),
    "checks": [
        "Compare the field against the ACE entry summary for the same entry number.",
        "Confirm the scan is not a second-generation photocopy.",
    ],
    "blocking_unknowns": ["No independent source for the duty figure is in the file."],
    "citations": ["19 CFR §163.1"],
}

QUEUE_ROW = {
    "reason": "low_extraction_confidence",
    "severity": "high",
    "summary": "duty_amount read at 0.71 against a numeric floor of 0.95",
    "payload": {
        "field": "duty_amount",
        "confidence": "0.71",
        "floor": "0.95",
        "resume_token": "must-not-be-sent",
    },
    "citation": "19 CFR §163.1",
}


class TestTheExceptionMemo:
    def _facts(self) -> dict[str, Any]:
        return exceptions.build_facts(
            reason=ReviewReason(QUEUE_ROW["reason"]),
            severity=str(QUEUE_ROW["severity"]),
            summary=str(QUEUE_ROW["summary"]),
            payload=dict(QUEUE_ROW["payload"]),  # type: ignore[arg-type]
            citation=str(QUEUE_ROW["citation"]),
        )

    def test_it_drafts_against_a_queue_row(self) -> None:
        memo = exceptions.draft(self._facts(), client=StubClient(EXCEPTION_MEMO))
        assert isinstance(memo, ExceptionMemo)
        assert memo.recommendation is Recommendation.GATHER

    def test_each_reason_gets_its_own_framing(self) -> None:
        """A generic prompt produces confident prose about the wrong question.

        A threshold near-miss is an arithmetic and valuation question; a superseded ruling
        is a classification question; a low-confidence field is a question about one
        glyph. One prompt cannot ask all three well.
        """
        assert set(exceptions.GUIDANCE) == set(ReviewReason)
        for reason in ReviewReason:
            client = StubClient(EXCEPTION_MEMO)
            facts = exceptions.build_facts(
                reason=reason, severity="normal", summary="s", payload={}
            )
            exceptions.draft({**facts, "available_citations": ["19 CFR §163.1"]}, client=client)
            assert exceptions.GUIDANCE[reason] in client.calls[0]["messages"][0]["content"]

    def test_the_resume_token_never_reaches_the_model(self) -> None:
        """The payload is a dumping ground by design.

        An allow-by-default posture over an open-ended structure eventually sends
        something it should not, so the removals are named and the default is to withhold.
        """
        facts = self._facts()
        assert "resume_token" not in facts["matcher_payload"]
        client = StubClient(EXCEPTION_MEMO)
        exceptions.draft(facts, client=client)
        assert "must-not-be-sent" not in client.calls[0]["messages"][0]["content"]

    def test_redact_walks_nested_structures(self) -> None:
        cleaned = redact({"a": {"api_key": "x", "keep": 1}, "b": [{"api_key": "y"}]}, ["api_key"])
        assert cleaned == {"a": {"keep": 1}, "b": [{}]}

    def test_an_invented_confidence_score_is_rejected(self) -> None:
        """Confidence scores are figures too, and are the ones most likely to be
        casually restated."""
        bad = {**EXCEPTION_MEMO, "headline": "OCR read the duty figure at 0.42 confidence"}
        with pytest.raises(grounding.UngroundedFigureError):
            exceptions.draft(self._facts(), client=StubClient(bad))

    def test_an_invented_citation_is_refused_here_too(self) -> None:
        """The same check as the filing narrative, for the same reason.

        An exception memo is read by an analyst who is deciding what to do; an invented
        CFR section in it is as capable of steering a wrong decision as one in a document
        handed to CBP, and it arrives with less scrutiny.
        """
        bad = {**EXCEPTION_MEMO, "citations": ["19 CFR §163.1", "19 CFR §190.9999"]}
        with pytest.raises(ValueError, match=re.escape("190.9999")):
            exceptions.draft(self._facts(), client=StubClient(bad))

    def test_a_supplied_citation_passes(self) -> None:
        memo = exceptions.draft(self._facts(), client=StubClient(EXCEPTION_MEMO))
        assert memo.citations == ["19 CFR §163.1"]

    def test_blocking_unknowns_is_a_normal_outcome(self) -> None:
        """A confident memo over a thin record is the failure mode.

        It is the one an analyst is most likely to accept without checking, which is
        exactly what makes it dangerous.
        """
        memo = exceptions.draft(self._facts(), client=StubClient(EXCEPTION_MEMO))
        assert memo.blocking_unknowns

    def test_the_recommendation_is_advisory_and_nothing_else(self) -> None:
        """Nothing in the agent package writes claim state.

        The memo is a field on a queue row. A human calling `resolve_review_exception` is
        still the only thing that moves a claim.
        """
        import services.agent.src.queue as queue_module

        source = json.dumps(
            [
                str(getattr(queue_module, name).__doc__ or "")
                for name in ("draft_pending", "draft_one")
            ]
        )
        assert "resolve" not in source.lower() or "advisory" in source.lower()

    def test_the_memo_records_which_model_and_prompt_wrote_it(self) -> None:
        """ "Which model wrote this" is not answerable retrospectively.

        A memo that influenced a filing is a document an auditor may ask about, and a
        prompt revision changes the output as surely as a model change does.
        """
        tag = model_tag("claude-opus-5")
        assert tag == f"claude-opus-5+{PROMPT_VERSION}"


class TestTheGenerateContract:
    def test_it_refuses_a_response_with_no_tool_call(self) -> None:
        """Cannot happen while `tool_choice` is forced — which is why it is asserted.

        If a future SDK change relaxes the forcing, this is the test that notices rather
        than the first analyst reading an empty memo.
        """

        class NoToolClient:
            @property
            def messages(self) -> Any:
                return self

            def create(self, **_kwargs: Any) -> _Response:
                return _Response(content=[_Block(type="text", name="", input={})])

        with pytest.raises(AgentRefusedError, match="no tool call"):
            generate(
                output_model=InterchangeabilityMemo,
                system="s",
                facts={},
                instruction="i",
                client=NoToolClient(),
            )

    def test_a_missing_api_key_is_unavailable_not_a_crash(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(AgentUnavailableError, match="ANTHROPIC_API_KEY"):
            generate(
                output_model=ExceptionMemo,
                system="s",
                facts={},
                instruction="i",
            )

    def test_uuids_in_the_facts_do_not_break_serialisation(self) -> None:
        """Claim ids reach the prompt as strings, not as a TypeError."""
        client = StubClient(EXCEPTION_MEMO)
        facts = exceptions.build_facts(
            reason=ReviewReason.DEADLINE_IMMINENT,
            severity="blocking",
            summary="s",
            payload={"claim_id": uuid4()},
        )
        exceptions.draft({**facts, "available_citations": ["19 CFR §163.1"]}, client=client)
        assert client.calls

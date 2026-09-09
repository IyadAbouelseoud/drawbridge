"""Week 17: what the ancestor chain is for, and what re-ingesting has to invalidate.

The week 16 roadmap called the missing ancestor chain "the ceiling on retrieval". Week 17
repaired it and measured, and it is not the ceiling — see `docs/ARCHITECTURE.md` §22. What
survives the measurement is a narrower claim: a leaf that cannot identify a good on its own
needs its parent, and a leaf that can does not need its heading. These tests pin that
distinction, because it is the whole of the fix and it is one comparison in a loop.
"""

from __future__ import annotations

import json
import pathlib
import re

from services.ingest.src.tariff import (
    RETRIEVAL_TEXT_CAP,
    hierarchy_parts,
    retrieval_text,
)

BENCHMARK = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "tariff_benchmark.json"
TARIFF_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "services" / "ingest" / "src" / "tariff.py"
)

#: A leaf that says nothing, under a parent too long for the old budget. This is
#: 0301.93.02.90 with the species list shortened; the shape is what matters.
LONG_PARENT = "Carp (Cyprinus spp., Carassius spp., Ctenopharyngodon idellus, " + "x" * 160

#: A leaf that identifies the good without help, under a heading that does not. This is
#: 8471.30.01.00: the chain is two deep, so its only ancestor is the heading.
SELF_SUFFICIENT_LEAF = (
    "Portable automatic data processing machines, weighing not more than 10 kg, "
    "consisting of at least a central processing unit, a keyboard and a display"
)
SHARED_HEADING = (
    "Automatic data processing machines and units thereof; magnetic or optical readers, "
    "machines for transcribing data onto data media in coded form and machines for "
    "processing such data, not elsewhere specified or included"
)


class TestALeafThatCannotIdentifyAGoodGetsItsParent:
    """The defect: five characters of budget spent, the discriminator refused."""

    def test_a_bare_other_keeps_a_parent_that_exceeds_the_cap(self) -> None:
        text = retrieval_text(["Fish, fresh or chilled", LONG_PARENT, "Other"])
        assert text.startswith("Other, Carp (")
        assert len(LONG_PARENT) > RETRIEVAL_TEXT_CAP, "the parent must not merely fit"

    def test_without_the_parent_the_line_is_the_word_other(self) -> None:
        """What the corpus held for 1,821 lines, and why it could not be retrieved.

        The schedule has thousands of leaves reading "Other". Embedded alone they are the
        same point, and no ceiling, reranker or query separates a point from itself.
        """
        alone = retrieval_text(["Other"])
        assert alone == "Other"

    def test_the_grandparent_still_pays_the_cap(self) -> None:
        """Only the parent is exempt. Everything above it is budgeted as it always was."""
        text = retrieval_text(["Fish, fresh or chilled", LONG_PARENT, "Other"])
        assert "Fish, fresh or chilled" not in text


class TestALeafThatCanIdentifyAGoodIsNotGivenItsHeading:
    """The half that exempting the parent outright got wrong, kept as a test.

    Measured over the 50-query benchmark: exempting unconditionally scored 41/50 in the
    top ten against 42/50 for leaving it alone. A repair that made retrieval slightly
    worse, because it re-added the shared prefix the cap exists to keep out.
    """

    def test_a_two_deep_chain_does_not_get_the_heading(self) -> None:
        text = retrieval_text([SHARED_HEADING, SELF_SUFFICIENT_LEAF])
        assert text == SELF_SUFFICIENT_LEAF
        assert "magnetic or optical readers" not in text

    def test_a_three_deep_chain_does_get_its_intermediate_parent(self) -> None:
        """The same leaf length, the same oversized ancestor — and the opposite answer.

        The only difference is that the parent now discriminates between siblings instead
        of naming the group they all belong to. That is the whole rule.
        """
        text = retrieval_text(
            [SHARED_HEADING, "Other automatic data processing machines", SELF_SUFFICIENT_LEAF]
        )
        assert text.startswith(SELF_SUFFICIENT_LEAF)
        assert "Other automatic data processing machines" in text
        assert "magnetic or optical readers" not in text

    def test_a_short_leaf_with_a_short_parent_is_unaffected(self) -> None:
        """The case that always worked. It has to keep working to the byte."""
        assert retrieval_text(["Heading", "Parent", "Other"]) == "Other, Parent, Heading"


class TestTheChainIsBuiltBeforeItIsTruncated:
    """`hierarchy_parts` resolves the tree; `retrieval_text` decides what survives."""

    def test_an_indent_gap_does_not_reattach_a_child_to_a_stranger(self) -> None:
        rows = [(0, "Chapter"), (1, "Parent"), (3, "Child past a gap")]
        chains = dict(hierarchy_parts(rows))
        assert chains[2] == ["Chapter", "Parent", "Child past a gap"]

    def test_a_superior_row_supplies_text_without_being_classifiable(self) -> None:
        """Rows with no code carry the parent description for the lines beneath them."""
        rows = [(0, "Live horses"), (1, "Horses:"), (2, "Purebred breeding animals")]
        chains = dict(hierarchy_parts(rows))
        assert chains[2] == ["Live horses", "Horses:", "Purebred breeding animals"]


class TestARepairedCorpusInvalidatesItsOwnIndex:
    """The latent defect the repair exposed.

    `--reembed` keys on the model id, so a re-ingest that changes the text a line embeds
    from would have left every vector in place and reported full coverage. The corpus
    would have been updated and the index quietly not.
    """

    def test_the_upsert_nulls_the_embedding_when_the_text_changes(self) -> None:
        sql = TARIFF_SRC.read_text(encoding="utf-8")
        upsert = sql.split("ON CONFLICT (jurisdiction, source, code, revision)")[1]
        body = upsert.split('"""')[0]
        assert "embedding" in body
        assert "search_text IS DISTINCT FROM EXCLUDED.search_text" in body
        # Both columns, or a re-embed would find a row it thinks is already current.
        assert re.search(r"embedding\s*=\s*CASE", body)
        assert re.search(r"embedding_model_id\s*=\s*CASE", body)

    def test_it_does_not_null_the_embedding_when_the_text_is_unchanged(self) -> None:
        """Otherwise every ingest re-embeds 29,000 lines to change nothing."""
        sql = TARIFF_SRC.read_text(encoding="utf-8")
        upsert = sql.split("ON CONFLICT (jurisdiction, source, code, revision)")[1]
        assert "ELSE tariff_lines.embedding END" in upsert
        assert "ELSE tariff_lines.embedding_model_id END" in upsert


class TestTheBenchmarkIsBigEnoughToArgueWith:
    """Fifty queries, because ten made one query worth ten percentage points."""

    def test_it_holds_fifty_positives(self) -> None:
        data = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        assert len(data["positives"]) == 50

    def test_every_positive_carries_an_hs6_that_is_six_digits(self) -> None:
        data = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        for case in data["positives"]:
            assert re.fullmatch(r"\d{6}", case["expects_hs6"]), case["query"]

    def test_every_positive_expects_a_code_its_hs6_is_a_prefix_of(self) -> None:
        """A label that disagrees with itself would score whichever field was read."""
        data = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        for case in data["positives"]:
            assert case["expects"].startswith(case["expects_hs6"]), case["query"]

    def test_the_surviving_negatives_are_flagged_rather_than_named_in_a_script(
        self,
    ) -> None:
        """`calibrate_thresholds.py` matched one query by prose until week 17.

        Whether a negative survives a change of corpus is a property of the case — is
        this a good the schedule could ever carry? — so the fixture is where it belongs.
        """
        data = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        assert all("survives_full_schedule" in case for case in data["negatives"])
        surviving = [c for c in data["negatives"] if c["survives_full_schedule"]]
        assert len(surviving) >= 5, "the only negatives worth measuring at volume"

    def test_no_positive_is_also_a_negative(self) -> None:
        data = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        positives = {case["query"] for case in data["positives"]}
        negatives = {case["query"] for case in data["negatives"]}
        assert not positives & negatives

"""Calibration of the vector ceiling against a labelled set, and the finding it produced.

Week 8 set `FastEmbedEmbedder.vector_ceiling` to 0.75 from nine tariff lines and said in
the roadmap that nine was not enough to call it right. It was not: measured against
twenty labelled queries the value admitted eight of the ten hard negatives.

The more useful result is the one that cannot be fixed by moving the number.
`test_the_two_distance_ranges_overlap` asserts that the positive and negative
distance ranges *overlap* — the worst true positive sits further away than the nearest
thing the corpus cannot answer. A single distance threshold therefore cannot deliver
precision, at any value, and a test suite that only checked "the ceiling retrieves the
right answers" would have gone on reporting success while the ceiling let rubbish through.

So the ceiling here is calibrated for recall alone, and precision is measured where it is
actually decided — over the hits the system marks as needing no analyst. That measurement
needs the lexical path and therefore Postgres, and lives in
`tests/integration/test_tariff_benchmark.py`.

The fixture is checked in rather than generated: a benchmark that regenerates is a
benchmark whose results cannot be compared across weeks.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from services.classifier.src.embeddings import EmbeddingError, FastEmbedEmbedder
from services.classifier.src.search import CONFIRMATION_LEXICAL_FLOOR

BENCHMARK = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "tariff_benchmark.json"

# The calibrated value, restated here so the test fails loudly if the constant moves
# without the benchmark being re-run. A threshold that drifts without a measurement behind
# it is how week 8's ceiling came to suppress every semantic hit.
CALIBRATED_CEILING = 0.68

# Measured, not chosen: the worst distance at which a labelled positive's correct code is
# still retrieved. The ceiling must sit above it or recall is lost silently.
#
# Re-measured in week 15 when the corpus stopped being embedded with its own tariff code
# joined to the front. 0.625 -> 0.6348. These two numbers are *observations*, and moving
# one because a test failed would be exactly the habit the file exists to prevent — so
# what changed here is the text being embedded, and these are what the same measurement
# then returned.
WORST_TRUE_POSITIVE = 0.635

# Measured: the nearest a query the corpus cannot answer gets. The ceiling sits *above*
# this, which is the point of the whole file — see the module docstring.
#
# 0.508 -> 0.4969 for the same reason. Note which way each moved: the worst positive got
# *further* and the nearest negative got *closer*, so removing the code prefix widened the
# overlap rather than closing it. That is worth stating plainly, because the convenient
# reading of week 15 would be that fixing the embedding text fixed retrieval. It did not.
# It removed a defect that made every prior measurement unsound; the overlap this file has
# asserted since week 9 is still here, and a single distance threshold still cannot deliver
# precision at any value.
NEAREST_HARD_NEGATIVE = 0.497


def _load() -> dict[str, Any]:
    return json.loads(BENCHMARK.read_text(encoding="utf-8"))


DATA = _load()
CORPUS: list[dict[str, str]] = DATA["corpus"]
POSITIVES: list[dict[str, Any]] = DATA["positives"]
NEGATIVES: list[dict[str, Any]] = DATA["negatives"]


@pytest.fixture(scope="module")
def embedder() -> FastEmbedEmbedder:
    embedder = FastEmbedEmbedder()
    try:
        embedder.embed(["warm the model"])
    except EmbeddingError as exc:  # pragma: no cover - depends on network availability
        pytest.skip(f"fastembed model unavailable: {exc}")
    return embedder


@pytest.fixture(scope="module")
def corpus_vectors(embedder: FastEmbedEmbedder) -> list[list[float]]:
    """The corpus embedded exactly as `scripts/embed_corpus.py` embeds it.

    Same text composition, same model. A benchmark that embedded its corpus differently
    from production would measure a threshold nothing else uses.

    That sentence was true and was the reason the defect survived. Until week 15 both
    sides embedded `f"{code} {description}"`, so every document vector — here and in the
    database — began with a ten-digit tariff code that no query contains. The benchmark
    reproduced the defect faithfully, agreed with production, and reported a threshold
    measured over a corpus the queries could not reach. The one test whose job is to catch
    a mismatch between the two sides was built to match.

    The lesson is not that the fixture was wrong. It is that "the benchmark does what
    production does" is only worth having if what production does is checked separately —
    otherwise the two agree their way into the same mistake.
    """
    return embedder.embed([row["description_en"] for row in CORPUS])


def _ranked(
    embedder: FastEmbedEmbedder, vectors: list[list[float]], query: str
) -> list[tuple[str, float]]:
    """Cosine *distances* to every corpus line, nearest first — what pgvector returns."""
    query_vector = embedder.embed([query])[0]
    scored = [
        (row["code"], 1.0 - sum(a * b for a, b in zip(vector, query_vector, strict=True)))
        for row, vector in zip(CORPUS, vectors, strict=True)
    ]
    return sorted(scored, key=lambda pair: pair[1])


def _distance_to(ranked: list[tuple[str, float]], code: str) -> float:
    return next(distance for candidate, distance in ranked if candidate == code)


def _cases(rows: list[dict[str, Any]]) -> list[Any]:
    return [pytest.param(row, id=row["query"][:40]) for row in rows]


class TestTheBenchmarkIsWellFormed:
    """A benchmark nobody checked is a benchmark that measures whatever it happens to."""

    def test_it_holds_ten_of_each(self) -> None:
        assert len(POSITIVES) == 10
        assert len(NEGATIVES) == 10

    def test_every_positive_names_a_code_the_corpus_actually_holds(self) -> None:
        codes = {row["code"] for row in CORPUS}
        missing = [p["query"] for p in POSITIVES if p["expects"] not in codes]
        assert not missing

    def test_the_positives_cover_all_three_query_kinds(self) -> None:
        """Paraphrase, typo and multilingual fail differently.

        A set of paraphrases alone would prove the model handles synonyms and say nothing
        about a misspelled invoice line or an Arabic *Bayan* description, which are the
        two forms real input actually arrives in.
        """
        assert {p["kind"] for p in POSITIVES} == {"paraphrase", "typo", "multilingual"}

    def test_no_negative_is_answerable_from_the_corpus(self) -> None:
        """Hard negatives are absent goods, not adjacent subheadings.

        8482.10 ball bearings and 8482.20 roller bearings are semantically almost
        identical, and no distance threshold tells them apart. Labelling one a negative
        would assert that a ceiling can do something a ceiling cannot, and the suite would
        then be enforcing a false claim.
        """
        codes = {row["code"] for row in CORPUS}
        assert all(
            n.get("confusable_with") in codes or n.get("confusable_with") is None for n in NEGATIVES
        )


class TestTheCeilingIsCalibratedForRecall:
    @pytest.mark.parametrize("case", _cases(POSITIVES))
    def test_the_correct_code_sits_inside_the_ceiling(
        self, case: dict[str, Any], embedder: FastEmbedEmbedder, corpus_vectors: list[list[float]]
    ) -> None:
        """Every labelled positive's answer is reachable — no silent misses.

        This is the assertion week 8's ceiling would have passed and week 8's *previous*
        ceiling (0.55) would have failed, which is why the property is worth pinning
        query by query rather than in aggregate: an aggregate hides which one broke.
        """
        ranked = _ranked(embedder, corpus_vectors, case["query"])
        assert _distance_to(ranked, case["expects"]) <= CALIBRATED_CEILING

    def test_the_ceiling_matches_the_backend_constant(self) -> None:
        assert FastEmbedEmbedder().vector_ceiling == CALIBRATED_CEILING

    def test_the_ceiling_is_not_loose_enough_to_be_meaningless(
        self, embedder: FastEmbedEmbedder, corpus_vectors: list[list[float]]
    ) -> None:
        """It still has to exclude something.

        The three rubbish queries — live cattle, cut roses, a marine insurance service —
        must fall outside. A ceiling raised until everything passes trades a silent miss
        for a silent false positive, and vector search always returns *something*.
        """
        excluded = [
            n["query"]
            for n in NEGATIVES
            if _ranked(embedder, corpus_vectors, n["query"])[0][1] > CALIBRATED_CEILING
        ]
        assert len(excluded) >= 3

    def test_the_ceiling_is_no_looser_than_the_measurement_requires(
        self, embedder: FastEmbedEmbedder, corpus_vectors: list[list[float]]
    ) -> None:
        """Headroom over the worst positive, and not much more.

        Every point of slack admits more noise into the candidate list. The ceiling is set
        just above the measured worst case rather than at a comfortable round number,
        because the comfortable round number is what 0.75 was.
        """
        worst = max(
            _distance_to(_ranked(embedder, corpus_vectors, p["query"]), p["expects"])
            for p in POSITIVES
        )
        assert worst <= WORST_TRUE_POSITIVE
        # 0.0452 of headroom at the time of measuring. The bound is loose enough to
        # survive a re-measure and tight enough to catch a ceiling raised to make
        # something pass.
        assert CALIBRATED_CEILING - worst < 0.10


class TestNoCeilingCanDeliverPrecision:
    """The week 9 finding, kept as an executable claim.

    If a future model *does* separate these two classes, these tests fail — and that
    failure is the signal to reconsider whether the confirmation rule is still needed,
    not something to suppress.
    """

    def test_the_two_distance_ranges_overlap(
        self, embedder: FastEmbedEmbedder, corpus_vectors: list[list[float]]
    ) -> None:
        worst_positive = max(
            _distance_to(_ranked(embedder, corpus_vectors, p["query"]), p["expects"])
            for p in POSITIVES
        )
        nearest_negative = min(
            _ranked(embedder, corpus_vectors, n["query"])[0][1] for n in NEGATIVES
        )
        assert nearest_negative < worst_positive, (
            "the positive and negative distance ranges now separate, which would mean a "
            "single ceiling could deliver precision on its own — revisit "
            "search.CONFIRMATION_LEXICAL_FLOOR before relaxing anything"
        )

    def test_the_measured_edges_are_where_the_calibration_said(
        self, embedder: FastEmbedEmbedder, corpus_vectors: list[list[float]]
    ) -> None:
        """Pins the numbers the ceiling was derived from.

        Without this, the derivation in `embeddings.py` is a comment describing a
        measurement nobody re-runs.
        """
        worst_positive = max(
            _distance_to(_ranked(embedder, corpus_vectors, p["query"]), p["expects"])
            for p in POSITIVES
        )
        nearest_negative = min(
            _ranked(embedder, corpus_vectors, n["query"])[0][1] for n in NEGATIVES
        )
        assert worst_positive == pytest.approx(WORST_TRUE_POSITIVE, abs=0.02)
        assert nearest_negative == pytest.approx(NEAREST_HARD_NEGATIVE, abs=0.02)

    def test_the_confirmation_floor_exists_because_of_this(self) -> None:
        """The rule that carries precision instead. Measured in the integration suite."""
        assert 0.0 < CONFIRMATION_LEXICAL_FLOOR < 1.0

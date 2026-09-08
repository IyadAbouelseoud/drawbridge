"""The claim that vector search is semantic, tested as a claim rather than asserted.

Week 7 shipped a hashing vectorizer that exercised the whole pgvector path and could not
match a paraphrase; it reported `is_semantic = False` so nothing would present it as
search it was not. Week 8 replaced it. These fixtures are what makes that replacement a
fact rather than a change of label.

The decisive test is `test_paraphrase_reaches_the_line_the_lexical_backend_misses`: the
same query, against the same corpus, through both backends. Lexical loses; semantic wins.
A test that only asserted the semantic backend works would pass just as happily if the two
backends were identical.

The model is fetched once from HuggingFace and cached. Where that fetch cannot happen the
semantic tests skip rather than fail — a machine with no network has not broken anything —
but they never silently pass.
"""

from __future__ import annotations

import math

import pytest

from services.classifier.src.embeddings import (
    DEFAULT_BACKEND,
    DEFAULT_FASTEMBED_MODEL,
    EMBEDDING_DIM,
    Embedder,
    EmbeddingError,
    FastEmbedEmbedder,
    HashingEmbedder,
    build,
    to_pgvector,
)

# A miniature corpus spanning both jurisdictions and both scripts. Real published text,
# because a synthetic corpus of maximally-distinct sentences makes any embedder look good.
CORPUS: list[tuple[str, str]] = [
    (
        "8471300100",
        "Portable automatic data processing machines weighing not more than 10 kg, "
        "consisting of at least a central processing unit, a keyboard and a display",
    ),
    (
        "7207200000",
        "Semi-finished products of iron or non-alloy steel containing by weight "
        "0.25 percent or more of carbon",
    ),
    (
        "8483409000",
        "Gears and gearing, other than toothed wheels, chain sprockets and other "
        "transmission elements presented separately",
    ),
    (
        "0901210000",
        "Coffee, roasted, not decaffeinated",
    ),
]

# No token in common with the target description. That is the point: lexical similarity
# has nothing to work with, so any hit is semantic or accidental.
PARAPHRASE = "ruggedised field laptop computer"
TARGET = "8471300100"

# Arabic, as a ZATCA description would arrive. The retrieval target is an *English* line,
# which is the property the dual-jurisdiction corpus actually needs.
ARABIC_QUERY = "حاسب آلي محمول لمعالجة البيانات"


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _rank(embedder: Embedder, query: str) -> list[tuple[str, float]]:
    """Codes ordered by cosine similarity to the query, nearest first.

    Reproduces in Python what pgvector's distance operator does in the index. Testing the
    ranking here rather than through Postgres keeps the fixture about the embedder; the
    SQL path is already covered by the integration tests.
    """
    texts = [f"{code} {body}" for code, body in CORPUS]
    vectors = embedder.embed([*texts, query])
    query_vector = vectors[-1]
    scored = [
        (code, _cosine(vector, query_vector))
        for (code, _), vector in zip(CORPUS, vectors[:-1], strict=True)
    ]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


@pytest.fixture(scope="module")
def semantic() -> FastEmbedEmbedder:
    embedder = FastEmbedEmbedder()
    try:
        embedder.embed(["warm the model"])
    except EmbeddingError as exc:  # pragma: no cover - depends on network availability
        pytest.skip(f"fastembed model unavailable: {exc}")
    return embedder


class TestTheReplacementIsReal:
    def test_paraphrase_reaches_the_line_the_lexical_backend_misses(
        self, semantic: FastEmbedEmbedder
    ) -> None:
        """The week 7 limitation, and its removal, in one assertion.

        The paraphrase shares no word with "portable automatic data processing machines".
        The hashing backend therefore cannot rank it first — it has nothing but character
        trigrams. The semantic backend must.
        """
        lexical_first = _rank(HashingEmbedder(), PARAPHRASE)[0][0]
        semantic_ranking = _rank(semantic, PARAPHRASE)

        assert lexical_first != TARGET, (
            "the hashing backend ranked the paraphrase correctly, which means this "
            "fixture no longer demonstrates the difference it exists to demonstrate"
        )
        assert semantic_ranking[0][0] == TARGET

    def test_the_semantic_win_is_a_margin_not_a_coin_flip(
        self, semantic: FastEmbedEmbedder
    ) -> None:
        """First place by a clear gap.

        A correct ranking that is correct by 0.001 would flip on a model revision, and a
        test that tolerates that is a test that will pass while retrieval degrades.
        """
        ranking = _rank(semantic, PARAPHRASE)
        assert ranking[0][1] - ranking[1][1] > 0.05

    def test_an_arabic_query_retrieves_the_english_line(self, semantic: FastEmbedEmbedder) -> None:
        """Cross-lingual retrieval — the property that justifies the multilingual model.

        A Bayan describes goods in Arabic; the USITC schedule is in English. Without this,
        the two halves of the corpus are two corpora and a KSA claim cannot reach a US
        classification at all.
        """
        assert _rank(semantic, ARABIC_QUERY)[0][0] == TARGET

    def test_it_reports_itself_semantic_and_the_hashing_backend_does_not(
        self, semantic: FastEmbedEmbedder
    ) -> None:
        assert semantic.is_semantic is True
        assert HashingEmbedder().is_semantic is False

    def test_the_default_backend_is_the_semantic_one(self) -> None:
        """A deployment that says nothing gets semantic search.

        The default is the setting most likely to go unexamined, so it is the one that
        must not be the lexical placeholder.
        """
        assert DEFAULT_BACKEND == "fastembed"
        assert isinstance(build(DEFAULT_BACKEND), FastEmbedEmbedder)


class TestTheDistanceCeilingBelongsToTheModel:
    """The week 8 regression, kept.

    `VECTOR_CEILING` was 0.55, measured against the trigram backend. Swapping the model
    left it in place, and every semantic hit fell outside it — search reported
    `method=lexical` and returned nothing, which is indistinguishable from a corpus that
    was never embedded. Nothing raised; the feature simply did not work.
    """

    def test_the_ceiling_is_a_property_of_the_backend(self) -> None:
        assert HashingEmbedder().vector_ceiling != FastEmbedEmbedder().vector_ceiling

    def test_a_good_match_sits_inside_the_semantic_ceiling(
        self, semantic: FastEmbedEmbedder
    ) -> None:
        """The assertion that would have failed before the fix.

        This model's distances are compressed: a correct match sits near 0.6, not near
        0.2. A ceiling from another space does not degrade this gracefully.
        """
        best_distance = 1.0 - _rank(semantic, PARAPHRASE)[0][1]
        assert best_distance <= semantic.vector_ceiling

    def test_an_unrelated_query_sits_outside_it(self, semantic: FastEmbedEmbedder) -> None:
        """The ceiling still has to reject something.

        Raising it until everything passes would trade a silent miss for a silent false
        positive, which is worse: vector search always returns *something*.
        """
        ranking = _rank(semantic, "a claim for maritime salvage of a sunken vessel")
        assert 1.0 - ranking[0][1] > semantic.vector_ceiling

    def test_the_hashing_ceiling_would_have_suppressed_the_semantic_hit(
        self, semantic: FastEmbedEmbedder
    ) -> None:
        """Names the bug directly, so a future backend swap cannot repeat it quietly."""
        best_distance = 1.0 - _rank(semantic, PARAPHRASE)[0][1]
        assert best_distance > HashingEmbedder().vector_ceiling


class TestTheVectorsFitTheColumn:
    def test_width_matches_the_migrated_column(self, semantic: FastEmbedEmbedder) -> None:
        """384, as migration a7c31f9d4e60 set it.

        A mismatch here is not a degraded search: the write fails, or worse, half the
        corpus lands in a different space from the other half.
        """
        assert EMBEDDING_DIM == 384
        assert len(semantic.embed(["portable machines"])[0]) == EMBEDDING_DIM

    def test_vectors_are_unit_length(self, semantic: FastEmbedEmbedder) -> None:
        vector = semantic.embed(["gears and gearing"])[0]
        assert math.isclose(math.sqrt(sum(c * c for c in vector)), 1.0, abs_tol=1e-6)

    def test_a_batch_returns_one_vector_per_text_in_order(
        self, semantic: FastEmbedEmbedder
    ) -> None:
        """Order is load-bearing: `embed_corpus.py` zips vectors back onto row ids.

        A reordered batch would write every embedding onto the wrong tariff line — a
        failure that produces no error and a corpus that is confidently wrong.
        """
        texts = [body for _, body in CORPUS]
        batched = semantic.embed(texts)
        assert len(batched) == len(texts)
        for text, vector in zip(texts, batched, strict=True):
            assert _cosine(vector, semantic.embed([text])[0]) > 0.999

    def test_pgvector_literal_round_trips_the_width(self, semantic: FastEmbedEmbedder) -> None:
        literal = to_pgvector(semantic.embed(["roasted coffee"])[0])
        assert literal.startswith("[")
        assert literal.endswith("]")
        assert len(literal[1:-1].split(",")) == EMBEDDING_DIM


class TestItRefusesRatherThanGuesses:
    def test_empty_text_raises(self, semantic: FastEmbedEmbedder) -> None:
        """Not a zero vector.

        A zero vector is a legal input to cosine distance and sits at a constant distance
        from everything, which reads downstream as "no match found" rather than "nothing
        was asked".
        """
        with pytest.raises(EmbeddingError):
            semantic.embed(["   "])

    def test_a_width_mismatch_is_caught_before_it_is_written(
        self, semantic: FastEmbedEmbedder
    ) -> None:
        """Configured width vs actual model width.

        The check exists because the failure it prevents is silent: a corpus half-written
        at the wrong width has no symptom until a search returns nonsense.
        """
        wrong = FastEmbedEmbedder(dimension=semantic.dimension * 4)
        with pytest.raises(EmbeddingError, match=str(semantic.dimension)):
            wrong.embed(["portable machines"])

    def test_an_unknown_backend_name_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(KeyError):
            build("fastembeb")

    def test_model_id_records_which_model_wrote_the_corpus(self) -> None:
        """Mixing models in one corpus is a data error, and this is what detects it."""
        assert FastEmbedEmbedder().model_id == f"fastembed:{DEFAULT_FASTEMBED_MODEL}"
        assert HashingEmbedder().model_id != FastEmbedEmbedder().model_id

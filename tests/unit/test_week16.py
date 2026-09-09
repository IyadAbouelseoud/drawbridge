"""Week 16 — the second stage, and the backup that had never been restored.

Two findings, one shape. Week 15 fixed the text the corpus was embedded from and the
number did not move, which left the standing claim — *a 384-dimension multilingual encoder
is too thin for 29,000 near-identical legal phrases* — untested rather than disproved.
Week 16 measured it properly and found the sharper version: the answer is usually **in**
the fifty nearest candidates and ranked wrong. Retrieval@50 is 8 of 10 where retrieval@10
is 6. That is a ranking problem, and a cross-encoder is the instrument for it.

The backup half is the same story told about a file. `retention.py` had been taking
provably immutable dumps since week 15, and nothing had ever read one back. The first run
of `restore` failed in thirty seconds on a masked password — a defect that could only
appear when something actually tried to connect.

So the tests here assert against *use* wherever a unit test can: the arithmetic of the
blend, the candidates the reranker is not allowed to touch, the guard that stops a drill
overwriting production, and the digest check that makes a restored file evidence rather
than bytes.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pytest

from scripts.retention import RESTORE_CHECK_TABLES, RetentionError, _fetch, restore
from services.classifier.src.embeddings import (
    DEFAULT_RERANK_MODEL,
    FastEmbedReranker,
    Reranker,
    build_reranker,
    confidence,
)
from services.classifier.src.search import (
    CODE_SCORE,
    RERANK_DEPTH,
    RERANK_WEIGHT,
    TariffHit,
    _rerank,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class StubReranker(Reranker):
    """A reranker whose scores are dictated by the test.

    A stub rather than the real cross-encoder, and not to be fast: the real model is a
    gigabyte fetched from HuggingFace, so a test that loaded it would be a test of network
    access that fails in CI for a reason unrelated to anything it asserts. What is being
    tested here is the blending, the exclusions and the bookkeeping — all of which are this
    module's own logic and none of which depend on the scores being good.
    """

    model_id = "stub-ce:v1"

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        self.seen.append((query, tuple(documents)))
        return [self.scores.get(document, 0.0) for document in documents]


def hit(code: str, score: float, *, code_matched: bool = False) -> TariffHit:
    return TariffHit(
        code=code,
        description_en=f"description for {code}",
        description_ar=None,
        jurisdiction="us",
        source="usitc",
        revision="2026-HTSA",
        duty_rate_general="Free",
        score=score,
        lexical_score=None,
        vector_distance=1.0 - score,
        code_matched=code_matched,
    )


class TestTheLogisticDoesNotOverflow:
    """`confidence` exists because a logit and a cosine similarity are not commensurable:
    one is unbounded in both directions and the other is bounded in [0, 1]. Weighting them
    together without squashing does not produce a weighted average, it produces whichever
    number happened to be larger."""

    def test_zero_is_a_coin_flip(self) -> None:
        assert confidence(0.0) == pytest.approx(0.5)

    def test_it_is_monotonic(self) -> None:
        values = [confidence(x) for x in (-8.0, -2.0, -0.5, 0.0, 0.5, 2.0, 8.0)]
        assert values == sorted(values)

    @pytest.mark.parametrize("logit", [-800.0, -710.0, -100.0, 100.0, 710.0, 800.0])
    def test_it_survives_the_logits_a_shortlist_actually_produces(self, logit: float) -> None:
        """The naive form overflows on a large negative logit, and a large negative logit
        is not an edge case here — in a shortlist of fifty candidates for one tariff line,
        forty-nine of them are things the model is confident are wrong."""
        value = confidence(logit)
        assert 0.0 <= value <= 1.0
        assert not math.isnan(value)

    def test_it_stays_inside_the_unit_interval(self) -> None:
        assert confidence(-800.0) == 0.0
        assert confidence(800.0) == 1.0


class TestTheBlendIsActuallyABlend:
    """Pure reranker order scores 3 of 10 at rank 1 and blending scores 4, because the
    cross-encoder demotes two answers retrieval already had right. The weight is the
    finding; these tests pin the arithmetic that carries it."""

    def test_it_blends_both_stages_at_the_stated_weight(self) -> None:
        hits = [hit("8471300100", 0.40)]
        bodies = {"8471300100": "portable automatic data processing machines"}
        stub = StubReranker({bodies["8471300100"]: 2.0})

        (result,) = _rerank(hits, query="laptop", bodies=bodies, reranker=stub, weight=0.7)

        expected = 0.7 * confidence(2.0) + 0.3 * 0.40
        assert result.score == pytest.approx(expected)
        assert result.rerank_score == pytest.approx(confidence(2.0))

    def test_it_records_which_model_scored_the_hit(self) -> None:
        """The same reason `embedding_model_id` is on the corpus. A score is meaningless
        outside the model that produced it, and a classification has to stay explicable
        after the default has moved on."""
        bodies = {"8471300100": "body"}
        stub = StubReranker({"body": 1.0})

        (result,) = _rerank(
            [hit("8471300100", 0.4)], query="q", bodies=bodies, reranker=stub, weight=0.7
        )
        assert result.reranker_id == "stub-ce:v1"
        assert result.as_dict()["reranker_id"] == "stub-ce:v1"

    def test_it_can_promote_a_candidate_retrieval_ranked_last(self) -> None:
        """The whole point of the second stage. Measured on the real corpus, the correct
        subheading for "ruggedised field laptop computer" sits at position 28 of 50."""
        hits = [hit("9999999999", 0.60), hit("8471300100", 0.35)]
        bodies = {"9999999999": "unrelated", "8471300100": "portable machines"}
        stub = StubReranker({"unrelated": -4.0, "portable machines": 4.0})

        result = sorted(
            _rerank(hits, query="laptop", bodies=bodies, reranker=stub, weight=RERANK_WEIGHT),
            key=lambda h: -h.score,
        )
        assert result[0].code == "8471300100"


class TestWhatTheRerankerIsNotAllowedToTouch:
    def test_a_code_lookup_is_never_reranked(self) -> None:
        """`CODE_SCORE` is the top of the range and the blend is bounded below it, so
        reranking a code hit would rank the line the caller asked for underneath a
        paraphrase of it. The caller named the digits; a model does not get an opinion."""
        hits = [hit("8471300100", CODE_SCORE, code_matched=True)]
        bodies = {"8471300100": "portable machines"}
        stub = StubReranker({"portable machines": -6.0})

        (result,) = _rerank(hits, query="8471.30", bodies=bodies, reranker=stub, weight=0.7)

        assert result.score == CODE_SCORE
        assert result.rerank_score is None
        assert stub.seen == []

    def test_a_candidate_with_no_body_is_passed_through(self) -> None:
        """An empty document scores arbitrarily, and an arbitrary score at 70% weight is
        worse than no score at all."""
        hits = [hit("8471300100", 0.4), hit("8482101000", 0.3)]
        bodies = {"8471300100": "portable machines"}
        stub = StubReranker({"portable machines": 1.0})

        results = {
            h.code: h for h in _rerank(hits, query="q", bodies=bodies, reranker=stub, weight=0.7)
        }

        assert results["8482101000"].score == pytest.approx(0.3)
        assert results["8482101000"].rerank_score is None
        assert stub.seen[0][1] == ("portable machines",)

    def test_reranking_does_not_confirm_anything(self) -> None:
        """The vector path finds and ranks and never confirms, and a cross-encoder is
        still a model saying something looked close. `needs_analyst_confirmation` is the
        lexical path's decision alone, and adding a second opinion from a second model
        does not turn two model opinions into published schedule text."""
        hits = [hit("8471300100", 0.4)]
        bodies = {"8471300100": "portable machines"}
        stub = StubReranker({"portable machines": 8.0})

        (result,) = _rerank(hits, query="q", bodies=bodies, reranker=stub, weight=0.7)

        assert result.rerank_score is not None
        assert result.rerank_score > 0.99
        assert result.needs_analyst_confirmation is True

    def test_nothing_scorable_means_nothing_is_called(self) -> None:
        hits = [hit("8471300100", CODE_SCORE, code_matched=True)]
        stub = StubReranker({})
        assert _rerank(hits, query="q", bodies={}, reranker=stub, weight=0.7) == hits
        assert stub.seen == []


class TestTheConstantsCarryTheirMeasurement:
    def test_the_depth_is_where_recall_stops_improving(self) -> None:
        """Measured over the published HTSA: 6/10 at depth 10 and 25, 8/10 from 50 onward,
        and still 8/10 at 500. Below 50 the pool is missing answers the reranker could
        rescue; above it, nothing more is ever found."""
        assert RERANK_DEPTH == 50

    def test_the_weight_sits_inside_the_measured_plateau(self) -> None:
        """0.50 through 0.85 all score 4/10 at rank one and 8/10 in the top ten. The
        plateau being broad is worth more than its centre: a constant that only works at
        one setting has been fitted to ten queries."""
        assert 0.50 <= RERANK_WEIGHT <= 0.85

    def test_the_default_reranker_is_multilingual(self) -> None:
        """Half this corpus is Arabic. Week 15 measured an English cross-encoder over it
        and watched the Arabic smartphone query fall from rank 1 to rank 12 — an English
        reranker does not score Arabic badly, it has no basis for scoring it at all."""
        assert "multilingual" in DEFAULT_RERANK_MODEL
        assert build_reranker().model_id == f"fastembed-ce:{DEFAULT_RERANK_MODEL}"

    def test_an_unknown_reranker_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(KeyError, match="unknown reranker backend"):
            build_reranker("nope")

    def test_constructing_one_downloads_nothing(self) -> None:
        """Lazy for the same reason the embedder is: a gigabyte of weights should be
        fetched when someone asks for a score, not when a config module is imported."""
        assert FastEmbedReranker()._encoder is None


class TestTheRestoreDrillRefusesToPretend:
    def test_it_will_not_restore_over_the_database_it_dumped(self) -> None:
        """The natural way to test a restore is to point it at the database you already
        have. Doing that once replaces production with a copy of itself from last night."""
        with pytest.raises(RetentionError, match="not a drill"):
            restore(
                bucket="drawbridge-backups",
                dsn="postgresql+psycopg://u:p@localhost:5432/drawbridge",
                scratch="drawbridge",
            )

    def test_the_checked_tables_are_the_ones_the_obligation_is_about(self) -> None:
        """Not the four largest. The ledger is the audit record, `claims` and `entry_lines`
        are what a refund was computed from, and `tariff_lines` is the schedule those
        figures were justified against. A dump missing one of these passes a row count over
        the whole database and satisfies nothing an auditor asks for."""
        assert "audit_ledger" in RESTORE_CHECK_TABLES
        assert set(RESTORE_CHECK_TABLES) == {
            "audit_ledger",
            "claims",
            "entry_lines",
            "tariff_lines",
        }

    def test_a_tampered_object_is_refused_before_it_is_restored(self, tmp_path: Path) -> None:
        """The digest recorded at write time is the load-bearing check. An ETag is computed
        by the same party that stored the object, so comparing an object to its own ETag
        proves the transfer worked — not that the bytes are the ones `pg_dump` produced."""

        class Body:
            def iter_chunks(self, _size: int) -> list[bytes]:
                return [b"not the dump that was written"]

        class Client:
            def get_object(self, **_kwargs: Any) -> dict[str, Any]:
                return {"Body": Body(), "Metadata": {"sha256": "0" * 64}}

        with pytest.raises(RetentionError, match="does not match the digest"):
            _fetch(
                Client(),
                bucket="b",
                key="postgres/2026/01/01/x.dump",
                version_id=None,
                path=tmp_path / "x.dump",
            )

    def test_an_object_with_no_recorded_digest_is_still_restorable(self, tmp_path: Path) -> None:
        """Backups written before the metadata existed have nothing to compare against.
        Refusing them would make the guard the reason an old backup cannot be restored,
        which is the opposite of what it is for."""

        class Body:
            def iter_chunks(self, _size: int) -> list[bytes]:
                return [b"a dump"]

        class Client:
            def get_object(self, **_kwargs: Any) -> dict[str, Any]:
                return {"Body": Body(), "Metadata": {}}

        digest = _fetch(Client(), bucket="b", key="k", version_id=None, path=tmp_path / "x.dump")
        assert len(digest) == 64

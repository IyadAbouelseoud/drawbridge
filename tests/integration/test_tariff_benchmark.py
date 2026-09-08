"""Precision of `classify_with_embedding`, measured where precision is actually decided.

The vector ceiling cannot separate the benchmark's positives from its negatives — see
`tests/golden/test_tariff_benchmark.py`. What can is agreement between the lexical and
vector paths, provided the lexical side is strong enough to mean something. This file
loads the labelled corpus into a real Postgres, runs the same `search_tariff` call the
MCP tool makes, and measures precision over the hits the system marks as needing no
analyst.

That set is the one where precision matters. Everything else the tool returns is a
suggestion carrying `needs_analyst_confirmation`, and a wrong suggestion costs a review.
A wrong *confirmed* classification costs a duty rate on a filing.

The corpus is inserted under its own revision and removed with the transaction, so the
benchmark never becomes part of the tenant's schedule.
"""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from services.classifier.src.embeddings import EmbeddingError, FastEmbedEmbedder, to_pgvector
from services.classifier.src.search import CONFIRMATION_LEXICAL_FLOOR, search_tariff

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

BENCHMARK = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "tariff_benchmark.json"
DATA: dict[str, Any] = json.loads(BENCHMARK.read_text(encoding="utf-8"))

REVISION = "benchmark-v1"
"""Its own revision so `search_tariff(revision=...)` sees the benchmark and nothing else.

Sharing a revision with the real schedule would make the measurement depend on whatever
happened to be ingested, which is the opposite of a fixture."""

_INSERT = text("""
    INSERT INTO tariff_lines (jurisdiction, source, revision, effective_from,
                              code, heading, hs6, description_en, embedding)
    VALUES ('us', 'usitc_hts', :revision, DATE '2026-01-01',
            CAST(:code AS text), left(CAST(:code AS text), 4), left(CAST(:code AS text), 6),
            :description, CAST(:embedding AS vector))
""")


@pytest.fixture(scope="module")
def embedder() -> FastEmbedEmbedder:
    embedder = FastEmbedEmbedder()
    try:
        embedder.embed(["warm the model"])
    except EmbeddingError as exc:  # pragma: no cover - depends on network availability
        pytest.skip(f"fastembed model unavailable: {exc}")
    return embedder


@pytest.fixture
def loaded(session: Session, embedder: FastEmbedEmbedder) -> Iterator[Session]:
    rows = DATA["corpus"]
    vectors = embedder.embed([f"{r['code']} {r['description_en']}" for r in rows])
    for row, vector in zip(rows, vectors, strict=True):
        session.execute(
            _INSERT,
            {
                "revision": REVISION,
                "code": row["code"],
                "description": row["description_en"],
                "embedding": to_pgvector(vector),
            },
        )
    session.flush()
    yield session


def _search(session: Session, embedder: FastEmbedEmbedder, query: str) -> list[Any]:
    """The call `mcp_hts.classify_with_embedding` makes, with the backend's own ceiling."""
    hits, _ = search_tariff(
        session,
        query=query,
        jurisdiction="us",
        embedding=embedder.embed([query])[0],
        revision=REVISION,
        limit=5,
        vector_ceiling=embedder.vector_ceiling,
    )
    return hits


def _confirmed(hits: list[Any]) -> list[Any]:
    return [hit for hit in hits if not hit.needs_analyst_confirmation]


class TestPrecisionOverConfirmedAnswers:
    def test_no_confirmed_answer_is_wrong(
        self, loaded: Session, embedder: FastEmbedEmbedder
    ) -> None:
        """100% precision on the twenty labelled queries.

        Precision, not accuracy: the denominator is what the system was willing to stand
        behind, and a query it declined to answer confidently costs a review rather than a
        misclassification. A rule that answered nothing would also score 1.0 here, which
        is why the recall test below is not optional.
        """
        wrong: list[str] = []
        for case in DATA["positives"]:
            for hit in _confirmed(_search(loaded, embedder, case["query"])):
                if hit.code != case["expects"]:
                    wrong.append(f"{case['query']!r} -> {hit.code} (want {case['expects']})")
        for case in DATA["negatives"]:
            for hit in _confirmed(_search(loaded, embedder, case["query"])):
                wrong.append(f"{case['query']!r} -> {hit.code} (corpus cannot answer it)")
        assert not wrong

    def test_it_confirms_enough_to_be_worth_having(
        self, loaded: Session, embedder: FastEmbedEmbedder
    ) -> None:
        """The other half of precision. Refusing everything is not a classifier.

        Five of the ten positives clear the bar unaided on this corpus. The rest are
        returned as candidates an analyst confirms — which is the honest outcome for a
        paraphrase against twenty-four lines of tariff text, not a failure.
        """
        confirmed = [
            case["query"]
            for case in DATA["positives"]
            if _confirmed(_search(loaded, embedder, case["query"]))
        ]
        assert len(confirmed) >= 5

    def test_the_correct_code_is_retrieved_for_every_positive(
        self, loaded: Session, embedder: FastEmbedEmbedder
    ) -> None:
        """Recall, through the real SQL rather than the in-process approximation.

        The golden test measures distances in Python; this one goes through pgvector's
        operator and the ceiling filter. They should agree, and if they ever do not, the
        SQL path is the one that ships.
        """
        missing = [
            case["query"]
            for case in DATA["positives"]
            if case["expects"] not in {hit.code for hit in _search(loaded, embedder, case["query"])}
        ]
        assert not missing


class TestTheConfirmationRuleIsWhatDoesIt:
    """The rule, isolated from the retrieval it sits on top of.

    Without these, `test_no_confirmed_answer_is_wrong` could pass because the corpus
    happens to be small, and nobody would know which mechanism earned it.
    """

    def test_weak_agreement_between_both_paths_is_not_confirmation(
        self, loaded: Session, embedder: FastEmbedEmbedder
    ) -> None:
        """The defect week 9 found, named.

        "wooden lead pencils" reaches wooden office furniture on both paths — a trigram
        coincidence on the word wooden at 0.157, and a mediocre vector distance. Under the
        old rule (`matched_by == "both"`) that came back as an answer needing no analyst.
        """
        hits = _search(loaded, embedder, "wooden lead pencils and coloured crayons")
        agreed = [h for h in hits if h.matched_by == "both"]
        assert agreed, "the fixture no longer exercises the case it exists for"
        assert all(h.lexical_score is not None for h in agreed)
        assert all(h.lexical_score < CONFIRMATION_LEXICAL_FLOOR for h in agreed)
        assert all(h.needs_analyst_confirmation for h in agreed)

    def test_a_vector_only_hit_is_never_confirmed(
        self, loaded: Session, embedder: FastEmbedEmbedder
    ) -> None:
        """Arabic queries reach the English lines with no lexical overlap at all.

        Cross-lingual retrieval works and is still a suggestion: the schedule did not say
        so, a multilingual encoder did.
        """
        hits = _search(loaded, embedder, "هاتف ذكي يعمل على الشبكات الخلوية")
        assert hits
        assert hits[0].code == "8517130000"
        assert hits[0].matched_by == "vector"
        assert hits[0].needs_analyst_confirmation

    def test_the_floor_in_force_is_recorded_on_the_hit(
        self, loaded: Session, embedder: FastEmbedEmbedder
    ) -> None:
        """A stored classification has to stay explicable after the constant moves."""
        hits = _search(loaded, embedder, "roasted cofee beans, not decafinated")
        assert hits
        assert hits[0].confirmation_lexical_floor == CONFIRMATION_LEXICAL_FLOOR
        assert hits[0].as_dict()["confirmation_lexical_floor"] == CONFIRMATION_LEXICAL_FLOOR

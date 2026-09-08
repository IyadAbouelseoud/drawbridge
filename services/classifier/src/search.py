"""Tariff classification search.

Hybrid by default: lexical trigram matching and vector similarity answer different
questions and fail differently.

- **Lexical** finds a line when the analyst already speaks tariff language — "portable
  automatic data processing machines" is close to verbatim, and trigram similarity nails
  it. It fails on paraphrase.
- **Vector** finds a line from a commercial description — "ruggedised field laptop" has
  no lexical overlap with the heading text at all. It fails by returning something
  plausible for anything, including nonsense.

Running both and reporting which contributed is the point. A hit both paths agree on is
worth more than either alone, and a hit only the vector path produced is exactly the case
an analyst should look at rather than accept.

No embedding model is called here. Embeddings arrive as vectors from the caller, so this
module stays a pure function of its input and a classification can be re-run years later
without depending on a model endpoint that may no longer exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

# Below this trigram similarity a lexical hit is noise rather than a match.
LEXICAL_FLOOR = 0.15

# Cosine distance above this means the vector path found nothing genuinely close. Vector
# search always returns *something*, so without a ceiling a nonsense query yields
# confident rubbish.
#
# This is only the fallback. The real value belongs to the backend that wrote the corpus
# (`Embedder.vector_ceiling`), because a distance threshold is meaningful only inside one
# embedding space — week 8 changed the model and this constant, unchanged, suppressed
# every semantic hit. Callers should pass `vector_ceiling` explicitly; this default exists
# for the lexical backend and for callers that have no embedder to ask.
VECTOR_CEILING = 0.55

# Lexical similarity a hit must reach before agreement between the two paths counts as
# corroboration. Distinct from LEXICAL_FLOOR, which decides whether a lexical hit is
# returned at all; this decides whether one is strong enough to stand behind an answer.
#
# Week 9 added it because the benchmark found the previous rule wrong. `matched_by ==
# "both"` was treated as confirmation regardless of how weak the lexical side was, and
# two of the twenty labelled queries produced exactly that: "wooden lead pencils" reached
# wooden office furniture at 0.157, and a desktop-computer query reached the portable ADP
# subheading at 0.160. Both came back as corroborated answers needing no analyst, and both
# were wrong. Every correct corroborated answer in the set scored 0.216 or better.
#
# 0.20 sits in that gap. It is a narrow gap measured on twenty queries, so it is a floor
# that will move — but the shape of the rule is the finding, not the constant: weak
# agreement between two weak signals is not evidence, and reading it as evidence is how a
# misclassification reaches a filing unexamined.
CONFIRMATION_LEXICAL_FLOOR = 0.20

# Weight given to each path when both contribute. Lexical is trusted slightly more
# because an exact phrase match against published tariff text is stronger evidence than
# embedding proximity.
LEXICAL_WEIGHT = 0.55
VECTOR_WEIGHT = 0.45


@dataclass(frozen=True, slots=True)
class TariffHit:
    """One candidate classification, with the evidence behind it."""

    code: str
    description_en: str
    description_ar: str | None
    jurisdiction: str
    source: str
    revision: str
    duty_rate_general: str | None
    score: float
    lexical_score: float | None
    vector_distance: float | None

    confirmation_lexical_floor: float = CONFIRMATION_LEXICAL_FLOOR
    """The floor this hit was judged against, carried on the hit rather than looked up.

    A stored classification has to stay explicable: an auditor asking in 2030 why a line
    was taken without review needs the threshold that was in force, not the one the
    constant holds by then."""

    @property
    def matched_by(self) -> str:
        if self.lexical_score is not None and self.vector_distance is not None:
            return "both"
        return "lexical" if self.lexical_score is not None else "vector"

    @property
    def needs_analyst_confirmation(self) -> bool:
        """Whether this hit should be confirmed rather than taken.

        The lexical path decides this, alone. A strong trigram match is the published
        schedule text saying so; the vector path only ever says a model found something
        near, which is not the same claim however near it got. So the vector path finds
        and ranks, and never confirms.

        The week 9 benchmark is why there is a *strength* condition and not just a path
        condition. The previous rule confirmed anything both paths reached, and "wooden
        lead pencils" reached wooden office furniture on a trigram coincidence over the
        word wooden — 0.157, comfortably over the retrieval floor — with a mediocre vector
        distance agreeing. Two weak signals agreeing is one piece of noise counted twice.
        Every correct corroborated answer in that set scored 0.216 or better.

        Classification drives the duty rate, so an unconfirmed suggestion reaching a
        filing is a misclassification waiting to be assessed.
        """
        if self.lexical_score is None:
            return True
        return self.lexical_score < self.confirmation_lexical_floor

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "description_en": self.description_en,
            "description_ar": self.description_ar,
            "jurisdiction": self.jurisdiction,
            "source": self.source,
            "revision": self.revision,
            "duty_rate_general": self.duty_rate_general,
            "score": round(self.score, 4),
            "matched_by": self.matched_by,
            "lexical_score": round(self.lexical_score, 4) if self.lexical_score else None,
            "vector_distance": (
                round(self.vector_distance, 4) if self.vector_distance is not None else None
            ),
            "needs_analyst_confirmation": self.needs_analyst_confirmation,
            "confirmation_lexical_floor": self.confirmation_lexical_floor,
        }


_LEXICAL_SQL = text("""
    SELECT code, description_en, description_ar, jurisdiction, source, revision,
           duty_rate_general,
           similarity(description_en, :q) AS lex
    FROM tariff_lines
    WHERE jurisdiction = :jurisdiction
      AND (CAST(:revision AS text) IS NULL OR revision = CAST(:revision AS text))
      AND similarity(description_en, :q) > :floor
    ORDER BY lex DESC
    LIMIT :limit
""")

_VECTOR_SQL = text("""
    SELECT code, description_en, description_ar, jurisdiction, source, revision,
           duty_rate_general,
           embedding <=> CAST(:embedding AS vector) AS dist
    FROM tariff_lines
    WHERE jurisdiction = :jurisdiction
      AND embedding IS NOT NULL
      AND (CAST(:revision AS text) IS NULL OR revision = CAST(:revision AS text))
    ORDER BY CAST(:embedding AS vector) <=> embedding
    LIMIT :limit
""")

_RULING_SQL = text("""
    SELECT ruling_number, ruling_date, classified_code, subject, url, superseded_by,
           similarity(subject || ' ' || body, :q) AS lex
    FROM tariff_rulings
    WHERE jurisdiction = :jurisdiction
      AND (CAST(:code AS text) IS NULL OR classified_code = CAST(:code AS text)
           OR hs6 = left(CAST(:code AS text), 6))
      AND similarity(subject || ' ' || body, :q) > :floor
    ORDER BY lex DESC
    LIMIT :limit
""")


def search_tariff(
    session: Session,
    *,
    query: str,
    jurisdiction: str,
    embedding: Sequence[float] | None = None,
    revision: str | None = None,
    limit: int = 10,
    vector_ceiling: float = VECTOR_CEILING,
    confirmation_lexical_floor: float = CONFIRMATION_LEXICAL_FLOOR,
) -> tuple[list[TariffHit], str]:
    """Classify a goods description. Returns hits and which method answered.

    Passing no embedding is a supported mode, not a degraded one: lexical-only search is
    correct when the description is already tariff language, and it keeps the service
    usable before the embedding pass has run over a freshly ingested schedule.

    `vector_ceiling` should come from the backend that embedded the corpus. Leaving it at
    the default while querying a corpus embedded by a different model is the failure week
    8 hit: every vector hit falls outside a threshold calibrated for another space, and
    the search silently degrades to lexical without reporting that it did.
    """
    lexical: dict[str, dict[str, Any]] = {}
    for row in session.execute(
        _LEXICAL_SQL,
        {
            "q": query,
            "jurisdiction": jurisdiction,
            "revision": revision,
            "floor": LEXICAL_FLOOR,
            "limit": limit * 2,
        },
    ).mappings():
        lexical[row["code"]] = dict(row)

    vector: dict[str, dict[str, Any]] = {}
    if embedding is not None:
        for row in session.execute(
            _VECTOR_SQL,
            {
                # pgvector accepts its literal text form; casting in SQL keeps the
                # driver from having to know about the type.
                "embedding": "[" + ",".join(str(float(x)) for x in embedding) + "]",
                "jurisdiction": jurisdiction,
                "revision": revision,
                "limit": limit * 2,
            },
        ).mappings():
            if row["dist"] <= vector_ceiling:
                vector[row["code"]] = dict(row)

    method = (
        "hybrid"
        if lexical and vector
        else "lexical"
        if lexical
        else "vector"
        if vector
        else "lexical"
    )
    hits = _merge(lexical, vector, confirmation_lexical_floor)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit], method


def _merge(
    lexical: dict[str, dict[str, Any]],
    vector: dict[str, dict[str, Any]],
    confirmation_lexical_floor: float = CONFIRMATION_LEXICAL_FLOOR,
) -> list[TariffHit]:
    """Combine the two paths into one ranked list.

    Cosine distance is inverted to a similarity so the two scores point the same way
    before they are weighted; a hit found by only one path keeps that path's score
    rather than being penalised for the other's silence.
    """
    hits: list[TariffHit] = []
    for code in set(lexical) | set(vector):
        lex_row = lexical.get(code)
        vec_row = vector.get(code)
        row = lex_row or vec_row
        assert row is not None

        lex_score = float(lex_row["lex"]) if lex_row else None
        distance = float(vec_row["dist"]) if vec_row else None
        vec_score = (1.0 - distance) if distance is not None else None

        if lex_score is not None and vec_score is not None:
            score = LEXICAL_WEIGHT * lex_score + VECTOR_WEIGHT * vec_score
        else:
            score = lex_score if lex_score is not None else (vec_score or 0.0)

        hits.append(
            TariffHit(
                code=code,
                description_en=row["description_en"],
                description_ar=row["description_ar"],
                jurisdiction=row["jurisdiction"],
                source=row["source"],
                revision=row["revision"],
                duty_rate_general=row["duty_rate_general"],
                score=score,
                lexical_score=lex_score,
                vector_distance=distance,
                confirmation_lexical_floor=confirmation_lexical_floor,
            )
        )
    return hits


def search_rulings(
    session: Session,
    *,
    query: str,
    jurisdiction: str = "us",
    code: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find rulings supporting a classification.

    Superseded rulings are returned but flagged rather than filtered out. A claim filed
    while a ruling was good law relied on it, so hiding the supersession would make the
    historical record harder to reconstruct, not easier.
    """
    rows = session.execute(
        _RULING_SQL,
        {
            "q": query,
            "jurisdiction": jurisdiction,
            "code": code,
            "floor": LEXICAL_FLOOR,
            "limit": limit,
        },
    ).mappings()

    return [
        {
            "ruling_number": r["ruling_number"],
            "ruling_date": r["ruling_date"].isoformat() if r["ruling_date"] else None,
            "classified_code": r["classified_code"],
            "subject": r["subject"],
            "url": r["url"],
            "superseded_by": r["superseded_by"],
            "is_current": r["superseded_by"] is None,
            "score": round(float(r["lex"]), 4),
        }
        for r in rows
    ]

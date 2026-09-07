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
# search always returns *something*, so without a floor a nonsense query yields confident
# rubbish.
VECTOR_CEILING = 0.55

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

    @property
    def matched_by(self) -> str:
        if self.lexical_score is not None and self.vector_distance is not None:
            return "both"
        return "lexical" if self.lexical_score is not None else "vector"

    @property
    def needs_analyst_confirmation(self) -> bool:
        """Whether this hit should be confirmed rather than taken.

        A vector-only hit is a suggestion: the model found something semantically near,
        which is not the same as the schedule saying so. Classification drives the duty
        rate, so an unconfirmed suggestion reaching a filing is a misclassification
        waiting to be assessed.
        """
        return self.matched_by == "vector"

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
) -> tuple[list[TariffHit], str]:
    """Classify a goods description. Returns hits and which method answered.

    Passing no embedding is a supported mode, not a degraded one: lexical-only search is
    correct when the description is already tariff language, and it keeps the service
    usable before the embedding pass has run over a freshly ingested schedule.
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
            if row["dist"] <= VECTOR_CEILING:
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
    hits = _merge(lexical, vector)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit], method


def _merge(
    lexical: dict[str, dict[str, Any]], vector: dict[str, dict[str, Any]]
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

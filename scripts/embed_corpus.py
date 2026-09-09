"""Populate the pgvector columns over the tariff corpus.

    python scripts/embed_corpus.py                       # semantic, the default
    python scripts/embed_corpus.py --table rulings
    python scripts/embed_corpus.py --backend hashing     # lexical, no model download
    python scripts/embed_corpus.py --backend ollama --model nomic-embed-text

**Resumable by construction.** Only rows with a NULL embedding are selected, so an
interrupted run continues where it stopped and a second run over a finished corpus is a
no-op. That matters at 19,000 lines against a local model: the job takes long enough that
it will be interrupted, and a non-resumable pass would mean starting over.

**Batched.** A real backend amortises one round trip across the batch. `--batch-size`
trades throughput against how much work an interruption discards.

**Never prints a vector.** The output is counts and rates. A 384-float array in a
terminal is unreadable and, at corpus scale, ruinous to anything capturing the output.

**The text embedded is the description, and nothing else.** Until week 15 it was
`f"{code} {body}"` — the ten-digit tariff code joined to the front of every document,
on the reasoning that a query like "8471.30 portable machines" should reach the line by
either half. It does not work that way. No analyst query carries the code, so every one
of the 28,899 document vectors began with a token the query side never contains, and
28,899 near-identical digit strings pull the whole corpus toward one direction in a space
that is supposed to separate them. The stored vector for a line reproduced at cosine
1.000000 against `code + " " + search_text` and 0.950 against the text alone: the corpus
and the queries were in measurably different distributions, for seven weeks, and it
presented the whole time as "the model is too thin at volume".

A query that does carry a code is now served by `search_tariff`'s code path, which
matches the digits exactly instead of hoping a subword tokeniser does. That is both
better retrieval and an answer somebody can defend in an audit.

Every row written records the backend **and the text convention** in
`embedding_model_id`, which is what `embeddings.py` has claimed since week 8 and what no
column existed to hold.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, TextIO

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from services.classifier.src.embeddings import (
    BACKENDS,
    DEFAULT_BACKEND,
    Embedder,
    EmbeddingError,
    build,
    to_pgvector,
)

DEFAULT_DSN = "postgresql+psycopg://drawbridge:drawbridge@localhost:5432/drawbridge"

# One SELECT per table. `FOR UPDATE SKIP LOCKED` so two workers can embed the same corpus
# concurrently without either blocking on the other's batch or double-writing a row.
_PENDING_LINES = text("""
    SELECT tariff_line_id AS id, code,
           -- search_text first: the same ancestor chain, leaf-first and truncated, which
           -- is what survives mean pooling. Null for ZATCA and for anything ingested
           -- before f2b90d47ac13, so the description remains the fallback.
           coalesce(nullif(search_text, ''), nullif(description_en, ''), description_ar, '') AS body
    FROM tariff_lines
    WHERE (embedding IS NULL
           OR (CAST(:reembed AS boolean)
               AND embedding_model_id IS DISTINCT FROM CAST(:corpus_id AS text)))
      AND (CAST(:jurisdiction AS text) IS NULL OR jurisdiction = CAST(:jurisdiction AS text))
      AND (CAST(:revision AS text) IS NULL OR revision = CAST(:revision AS text))
    ORDER BY tariff_line_id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
""")

_PENDING_RULINGS = text("""
    SELECT ruling_id AS id, classified_code AS code,
           subject || ' ' || left(body, 4000) AS body
    FROM tariff_rulings
    WHERE (embedding IS NULL
           OR (CAST(:reembed AS boolean)
               AND embedding_model_id IS DISTINCT FROM CAST(:corpus_id AS text)))
      AND (CAST(:jurisdiction AS text) IS NULL OR jurisdiction = CAST(:jurisdiction AS text))
      AND (CAST(:revision AS text) IS NULL OR TRUE)
    ORDER BY ruling_id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
""")

_UPDATE_LINE = text("""
    UPDATE tariff_lines
       SET embedding = CAST(:embedding AS vector), embedding_model_id = :model_id
     WHERE tariff_line_id = :id
""")

_UPDATE_RULING = text("""
    UPDATE tariff_rulings
       SET embedding = CAST(:embedding AS vector), embedding_model_id = :model_id
     WHERE ruling_id = :id
""")

_COVERAGE = text("""
    SELECT count(*) AS total, count(embedding) AS embedded,
           count(*) FILTER (
               WHERE embedding IS NOT NULL
                 AND embedding_model_id IS DISTINCT FROM CAST(:corpus_id AS text)
           ) AS stale
    FROM tariff_lines
    WHERE (CAST(:jurisdiction AS text) IS NULL OR jurisdiction = CAST(:jurisdiction AS text))
""")

_COVERAGE_RULINGS = text("""
    SELECT count(*) AS total, count(embedding) AS embedded,
           count(*) FILTER (
               WHERE embedding IS NOT NULL
                 AND embedding_model_id IS DISTINCT FROM CAST(:corpus_id AS text)
           ) AS stale
    FROM tariff_rulings
    WHERE (CAST(:jurisdiction AS text) IS NULL OR jurisdiction = CAST(:jurisdiction AS text))
""")

TABLES = {
    "lines": (_PENDING_LINES, _UPDATE_LINE, _COVERAGE),
    "rulings": (_PENDING_RULINGS, _UPDATE_RULING, _COVERAGE_RULINGS),
}


#: The text convention this script embeds, recorded alongside the model. Two corpora
#: embedded by the same model from different text are as incomparable as two models, and
#: that is the mistake that actually happened — so the stamp has to name both halves or
#: it would not have caught it.
TEXT_CONVENTION = "desc"


def corpus_id(embedder: Embedder) -> str:
    """What goes in `embedding_model_id`: the backend and the text it was given.

    Truncated to the column width rather than allowed to fail the write, because a long
    Ollama model name should degrade to a shorter stamp and not to an unembedded corpus.
    """
    return f"{embedder.model_id}/{TEXT_CONVENTION}"[:128]


def _utf8(stream: TextIO) -> None:
    """Force UTF-8 on a console stream where it supports it.

    ZATCA descriptions are Arabic and a Windows console defaults to cp1252. Only
    `TextIOWrapper` carries `reconfigure`; a redirected stream may not, and that is not a
    reason to fail the run.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def embed_batch(
    session: Session,
    embedder: Embedder,
    *,
    table: str,
    jurisdiction: str | None,
    revision: str | None,
    batch_size: int,
    reembed: bool = False,
) -> tuple[int, int]:
    """Embed one batch. Returns (written, skipped).

    A row whose text the backend refuses is skipped and left NULL rather than being
    written with a placeholder. Vector search already skips unembedded rows, so a skipped
    row degrades to lexical-only for that line — which is a worse answer, not a wrong one.
    A placeholder vector would be a wrong one.
    """
    select_sql, update_sql, _ = TABLES[table]
    rows = (
        session.execute(
            select_sql,
            {
                "jurisdiction": jurisdiction,
                "revision": revision,
                "limit": batch_size,
                "reembed": reembed,
                "corpus_id": corpus_id(embedder),
            },
        )
        .mappings()
        .all()
    )
    if not rows:
        return 0, 0

    # The description alone. See the module docstring: joining the code prefixed every
    # document with a token no query carries.
    texts = [str(row["body"]).strip() for row in rows]
    try:
        vectors = embedder.embed(texts)
    except EmbeddingError:
        # One bad text must not discard the batch, so fall back to per-row on failure.
        vectors = []
        for one in texts:
            try:
                vectors.append(embedder.embed([one])[0])
            except EmbeddingError:
                vectors.append([])

    written = skipped = 0
    for row, vector in zip(rows, vectors, strict=True):
        if not vector:
            skipped += 1
            continue
        session.execute(
            update_sql,
            {
                "id": row["id"],
                "embedding": to_pgvector(vector),
                "model_id": corpus_id(embedder),
            },
        )
        written += 1

    session.commit()
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    _utf8(sys.stdout)
    _utf8(sys.stderr)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default=DEFAULT_BACKEND, choices=tuple(sorted(BACKENDS)))
    parser.add_argument("--model", default=None, help="fastembed or ollama")
    parser.add_argument("--base-url", default=None, help="ollama only")
    parser.add_argument("--cache-dir", default=None, help="fastembed model cache")
    parser.add_argument("--table", default="lines", choices=tuple(TABLES))
    parser.add_argument("--jurisdiction", default=None, choices=("us", "ksa"))
    parser.add_argument("--revision", default=None)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Stop after this many rows. 0 embeds everything pending.",
    )
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="also rewrite rows whose embedding_model_id is not the current backend and "
        "text convention. Overwrites in place rather than nulling first, so the corpus "
        "converges instead of going dark and an interrupted run leaves a mixture the "
        "stamp makes visible.",
    )
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)

    kwargs: dict[str, Any] = {}
    if args.backend in {"ollama", "fastembed"} and args.model:
        kwargs["model"] = args.model
    if args.backend == "ollama" and args.base_url:
        kwargs["base_url"] = args.base_url
    if args.backend == "fastembed" and args.cache_dir:
        kwargs["cache_dir"] = args.cache_dir

    embedder = build(args.backend, **kwargs)
    if not embedder.is_semantic:
        print(
            f"note: {embedder.model_id} is a lexical backend. It cannot match a "
            "paraphrase, so vector hits from this corpus are not semantic search.",
            file=sys.stderr,
        )

    engine = create_engine(args.dsn or os.environ.get("DRAWBRIDGE_DATABASE_URL", DEFAULT_DSN))
    started = time.monotonic()
    total_written = total_skipped = 0

    with Session(engine) as session:
        while True:
            limit = args.batch_size
            if args.max_rows:
                remaining = args.max_rows - total_written - total_skipped
                if remaining <= 0:
                    break
                limit = min(limit, remaining)

            written, skipped = embed_batch(
                session,
                embedder,
                table=args.table,
                jurisdiction=args.jurisdiction,
                revision=args.revision,
                batch_size=limit,
                reembed=args.reembed,
            )
            if written == 0 and skipped == 0:
                break
            total_written += written
            total_skipped += skipped
            print(
                f"  {total_written} embedded, {total_skipped} skipped",
                file=sys.stderr,
            )

        _, _, coverage_sql = TABLES[args.table]
        coverage = (
            session.execute(
                coverage_sql,
                {"jurisdiction": args.jurisdiction, "corpus_id": corpus_id(embedder)},
            )
            .mappings()
            .one()
        )

    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {
                "backend": embedder.model_id,
                "corpus_id": corpus_id(embedder),
                "semantic": embedder.is_semantic,
                "table": args.table,
                "embedded": total_written,
                "skipped": total_skipped,
                "seconds": round(elapsed, 2),
                "rows_per_second": round(total_written / elapsed, 1) if elapsed else None,
                "coverage": {
                    "total": coverage["total"],
                    "embedded": coverage["embedded"],
                    # Rows holding a vector some other backend or text convention wrote.
                    # Non-zero means the corpus is not searchable as one thing: a query
                    # vector is comparable to one convention and meaningless against the
                    # other. `--reembed` is what clears it.
                    "stale": coverage["stale"],
                    "pct": (
                        round(100 * coverage["embedded"] / coverage["total"], 1)
                        if coverage["total"]
                        else 0.0
                    ),
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

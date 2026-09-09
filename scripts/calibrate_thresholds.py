"""Measure `vector_ceiling` and `CONFIRMATION_LEXICAL_FLOOR` against the real schedule.

    python scripts/calibrate_thresholds.py
    python scripts/calibrate_thresholds.py --rerank
    python scripts/calibrate_thresholds.py --revision 2026-HTSA --json

Both thresholds were set against a twenty-four line fixture. This runs the same labelled
query set against the full published schedule and reports what the numbers look like when
there are twenty-nine thousand candidates instead of twenty-four.

**The negatives do not survive the change of corpus, and that is the finding.**

`tests/fixtures/tariff_benchmark.json` defines a hard negative as *a good the corpus does
not carry, phrased like one it does* — live cattle, cut roses, raw sugar, printed books,
wooden pencils. Every one of those is a real heading in the published HTSA. Against the
full schedule they are not negatives at all; they are positives whose expected code the
fixture never recorded, because at the time there was nothing for them to match.

So a threshold "recalibrated" by counting how many negatives cross it would be measuring
the fixture's obsolescence and calling it precision. Two things are measured instead:

1. **Positives, at volume.** Does the right code still come back when 29,000 lines are
   competing for it, and at what distance? This is the question volume actually changes,
   and it is the one that decides whether the ceiling is still in the right place.
2. **The one negative that is still a negative.** "Marine cargo insurance brokerage
   arranged for a shipper" is not a good. No tariff schedule answers it at any volume, so
   the distance the corpus returns for it is a floor on what noise looks like.

`--rerank` runs the same set through the week 16 two-stage pipeline: fifty candidates from
pgvector, re-scored by a cross-encoder, blended. It is the flag that answers whether the
second stage earns its two seconds a query, and it prints the one-stage rank beside the
two-stage one so the comparison is on the page rather than in a commit message.

Read-only. Nothing here writes to the corpus or edits a threshold; it prints what the
evidence supports and leaves the decision, and the commit that records it, to a person.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from services.classifier.src.embeddings import EmbeddingError, build, build_reranker
from services.classifier.src.search import (
    CONFIRMATION_LEXICAL_FLOOR,
    RERANK_DEPTH,
    search_tariff,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.classifier.src.embeddings import Reranker

BENCHMARK = (
    pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "tariff_benchmark.json"
)
DEFAULT_DSN = "postgresql+psycopg://drawbridge:drawbridge@localhost:5432/drawbridge"

#: The only benchmark negative that stays a negative against the full schedule: it is not
#: a good, so no tariff line answers it however many lines there are.
STILL_A_NEGATIVE = "marine cargo insurance brokerage arranged for a shipper"


def _dsn() -> str:
    return os.environ.get("DRAWBRIDGE_DATABASE_URL", DEFAULT_DSN)


def _corpus_size(session: Session, revision: str) -> tuple[int, int]:
    row = (
        session.execute(
            text(
                "SELECT count(*) AS total, count(embedding) AS embedded "
                "FROM tariff_lines WHERE jurisdiction = 'us' AND revision = :rev"
            ),
            {"rev": revision},
        )
        .mappings()
        .one()
    )
    return int(row["total"]), int(row["embedded"])


def _rank_of(hits: Sequence[Any], expected_hs6: str) -> int | None:
    """1-based rank of the first hit in the expected six-digit subheading.

    Six digits, not ten, and the reason is what a goods description can actually decide.
    The published schedule splits 0901.21 eight ways on organic certification, Arabica
    versus Robusta, and container size — none of which appears in "roasted cofee beans,
    not decafinated". Scoring that query against a ten-digit code would be scoring the
    model on facts the query does not contain, and would report a correct classification
    as a miss. Six is also the internationally harmonised level, which is why
    `tariff_lines.hs6` exists as the cross-jurisdiction join key.
    """
    for index, hit in enumerate(hits, start=1):
        if hit.code[:6] == expected_hs6:
            return index
    return None


def measure(
    session: Session,
    embedder: Any,
    revision: str,
    reranker: Reranker | None = None,
) -> dict[str, Any]:
    data = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    total, embedded = _corpus_size(session, revision)

    positives: list[dict[str, Any]] = []
    for case in data["positives"]:
        vector = embedder.embed([case["query"]])[0]
        # Deliberately wide open. The point is to observe the distances the corpus
        # actually returns, and a ceiling applied here would hide the evidence needed
        # to choose one.
        common = {
            "query": case["query"],
            "jurisdiction": "us",
            "embedding": vector,
            "revision": revision,
            "limit": 10,
            "vector_ceiling": 2.0,
        }
        expected_hs6 = case.get("expects_hs6") or case["expects"][:6]

        # The one-stage result is measured whether or not a reranker was asked for, so
        # `--rerank` reports a delta rather than a number in isolation. A second stage
        # that has to be compared against a figure from a previous commit is a second
        # stage nobody can check.
        base_hits, method = search_tariff(session, **common)
        baseline_rank = _rank_of(base_hits, expected_hs6)

        hits = base_hits
        elapsed_ms: int | None = None
        if reranker is not None:
            started = time.perf_counter()
            hits, method = search_tariff(session, reranker=reranker, **common)
            elapsed_ms = int((time.perf_counter() - started) * 1000)

        top = hits[0] if hits else None
        positives.append(
            {
                "query": case["query"],
                "kind": case.get("kind", ""),
                "expects_hs6": expected_hs6,
                "rank": _rank_of(hits, expected_hs6),
                "baseline_rank": baseline_rank,
                "method": method,
                "top_code": top.code if top else None,
                "top_distance": top.vector_distance if top else None,
                "top_lexical": top.lexical_score if top else None,
                "top_rerank": top.rerank_score if top else None,
                "rerank_ms": elapsed_ms,
                "confirmed": bool(top and not top.needs_analyst_confirmation),
            }
        )

    noise: list[dict[str, Any]] = []
    for case in data["negatives"]:
        if case["query"] != STILL_A_NEGATIVE:
            continue
        vector = embedder.embed([case["query"]])[0]
        hits, _ = search_tariff(
            session,
            query=case["query"],
            jurisdiction="us",
            embedding=vector,
            revision=revision,
            limit=10,
            vector_ceiling=2.0,
            reranker=reranker,
        )
        top = hits[0] if hits else None
        noise.append(
            {
                "query": case["query"],
                "nearest_code": top.code if top else None,
                "nearest_distance": top.vector_distance if top else None,
                "nearest_lexical": top.lexical_score if top else None,
                "nearest_rerank": top.rerank_score if top else None,
                "would_confirm": bool(top and not top.needs_analyst_confirmation),
            }
        )

    return {
        "revision": revision,
        "corpus": {"lines": total, "embedded": embedded},
        "model": embedder.model_id,
        "reranker": reranker.model_id if reranker is not None else None,
        "rerank_depth": RERANK_DEPTH if reranker is not None else None,
        "positives": positives,
        "surviving_negatives": noise,
        "voided_negatives": [
            case["query"] for case in data["negatives"] if case["query"] != STILL_A_NEGATIVE
        ],
    }


def summarise(result: dict[str, Any]) -> str:
    lines: list[str] = []
    corpus = result["corpus"]
    lines.append(f"corpus      {corpus['lines']} line(s), {corpus['embedded']} embedded")
    lines.append(f"model       {result['model']}")
    if result.get("reranker"):
        lines.append(f"reranker    {result['reranker']} @ depth {result['rerank_depth']}")
    lines.append("")

    found = [p for p in result["positives"] if p["rank"] is not None]
    top1 = [p for p in found if p["rank"] == 1]
    distances = [p["top_distance"] for p in result["positives"] if p["top_distance"] is not None]

    lines.append("POSITIVES — does the right subheading still come back at volume?")
    lines.append(f"  {len(found)}/{len(result['positives'])} retrieved in the top 10")
    lines.append(f"  {len(top1)}/{len(result['positives'])} at rank 1")
    if result.get("reranker"):
        base_found = [p for p in result["positives"] if p.get("baseline_rank") is not None]
        base_top1 = [p for p in base_found if p["baseline_rank"] == 1]
        lines.append(
            f"  one stage was {len(base_found)}/{len(result['positives'])} in the top 10 "
            f"and {len(base_top1)}/{len(result['positives'])} at rank 1"
        )
        timings = [p["rerank_ms"] for p in result["positives"] if p.get("rerank_ms")]
        if timings:
            lines.append(
                f"  reranked query latency: median {int(statistics.median(timings))} ms, "
                f"max {max(timings)} ms"
            )
    if distances:
        lines.append(
            f"  top-hit distance: min {min(distances):.3f} "
            f"median {statistics.median(distances):.3f} max {max(distances):.3f}"
        )
    lines.append("")
    for p in result["positives"]:
        rank = p["rank"] if p["rank"] is not None else "-"
        dist = f"{p['top_distance']:.3f}" if p["top_distance"] is not None else "  -  "
        lex = f"{p['top_lexical']:.3f}" if p["top_lexical"] is not None else "  -  "
        flag = "confirm" if p["confirmed"] else "review "
        moved = ""
        if result.get("reranker"):
            was = p["baseline_rank"] if p["baseline_rank"] is not None else "-"
            moved = f"  was={was!s:<4}"
        lines.append(
            f"  rank={rank!s:>3}{moved}  d={dist}  lex={lex}  {flag}  "
            f"{p['kind']:<12} {p['query'][:44]}"
        )

    lines.append("")
    lines.append("NEGATIVES — only the non-good survives the change of corpus")
    for n in result["surviving_negatives"]:
        dist = f"{n['nearest_distance']:.3f}" if n["nearest_distance"] is not None else "  -  "
        lines.append(
            f"  nearest d={dist} code={n['nearest_code']} "
            f"would_confirm={n['would_confirm']}  {n['query'][:44]}"
        )
    lines.append("")
    lines.append(
        f"  {len(result['voided_negatives'])} of the fixture's negatives are void here: "
        "they name goods"
    )
    lines.append("  the 24-line corpus did not carry and the published schedule does. Against the")
    lines.append("  full HTSA they are positives with no recorded answer, not negatives. Counting")
    lines.append("  them would measure the fixture going stale and report it as precision.")

    lines.append("")
    lines.append("READING IT")
    if distances:
        worst = max(distances)
        lines.append(
            f"  A ceiling below {worst:.3f} would start discarding correct answers this "
            "corpus returns."
        )
    for n in result["surviving_negatives"]:
        if n["nearest_distance"] is not None:
            lines.append(
                f"  Noise for a non-good sits at {n['nearest_distance']:.3f}; a ceiling at "
                "or above that admits it."
            )
    lines.append(
        f"  CONFIRMATION_LEXICAL_FLOOR is {CONFIRMATION_LEXICAL_FLOOR}; the `confirm`/`review` "
        "column above"
    )
    lines.append("  is what it currently decides. Nothing here changes either constant.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure thresholds against the real corpus.")
    parser.add_argument("--revision", default="2026-HTSA", help="schedule revision to search")
    parser.add_argument("--dsn", default=None, help="database URL")
    parser.add_argument("--backend", default="fastembed", help="embedding backend")
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="run the two-stage pipeline and report it against one stage",
    )
    parser.add_argument("--rerank-backend", default="fastembed", help="reranker backend")
    parser.add_argument("--json", action="store_true", help="emit the raw measurement")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")

    try:
        embedder = build(args.backend)
        reranker = build_reranker(args.rerank_backend) if args.rerank else None
    except EmbeddingError as exc:
        print(f"cannot calibrate: {exc}", file=sys.stderr)
        return 1

    engine = create_engine(args.dsn or _dsn())
    with Session(engine) as session:
        total, embedded = _corpus_size(session, args.revision)
        if total == 0:
            print(
                f"no lines at revision {args.revision!r}; load the schedule first "
                "(scripts/ingest_tariff.py)",
                file=sys.stderr,
            )
            return 1
        if embedded < total:
            print(
                f"warning: {total - embedded} of {total} lines have no embedding; "
                "vector distances below are measured over a partial corpus",
                file=sys.stderr,
            )
        result = measure(session, embedder, args.revision, reranker)

    print(json.dumps(result, indent=2, default=str) if args.json else summarise(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

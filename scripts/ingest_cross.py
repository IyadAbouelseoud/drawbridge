"""Fetch a sample of CBP CROSS rulings and load them.

    python scripts/ingest_cross.py fetch --limit 100
    python scripts/ingest_cross.py load data/cross_rulings_2026-09-08.json
    python scripts/ingest_cross.py fetch --limit 100 --load

CROSS is the body of CBP classification rulings. `tariff_rulings` has been empty since
week 3 and `find_rulings` has therefore had nothing to cite, which is a real hole: a claim
citing a ruling that addresses the article survives a desk audit, and one citing a
plausible-sounding heading does not.

**Why there is a fetcher at all.** CBP publishes no bulk export. Weeks 9 through 13 each
recorded that and each deferred it. The search interface at rulings.cbp.gov is a
single-page application backed by a public JSON API — `/api/search` and `/api/ruling/{n}` —
and that API is what this uses. Driving a browser to scrape the rendered page would produce
the same rows through a headless Chromium, less reliably; and the ruling bodies come back
as plain text, so there is no markup to parse. Thirty bodies sampled across six chapters
contained not one HTML tag. `_looks_like_markup` exists for the day that changes and has
never fired.

**Fetch and load are separate subcommands, and that is deliberate.** The invariant in
`scripts/ingest_tariff.py` is that a corpus is loaded from a downloaded file, never from a
live endpoint, because a classification that reached a filing has to be reproducible years
later and an endpoint is reproducible only while the publisher keeps it alive. `fetch`
writes a snapshot; `load` reads one. `--load` runs both in sequence and still writes the
snapshot first, so the reproducible artifact exists either way.

**This is a sample, not the corpus.** CROSS holds on the order of two hundred thousand
rulings. What lands here is a few dozen to a few hundred, drawn by search terms chosen for
chapter spread, and the snapshot records which term found each one so the selection is
inspectable rather than asserted. A retrieval score computed over this sample says nothing
about a retrieval score over CROSS.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.ingest_tariff import _dsn, _utf8
from services.ingest.src.cross import SOURCE, parse_rows
from services.ingest.src.tariff import load_rulings

BASE_URL = "https://rulings.cbp.gov"

#: A courteous, identifiable agent. A scraper that presents itself as a browser is
#: pretending to be a person, and the publisher's ability to tell automated traffic apart
#: is what lets them rate-limit it instead of blocking it.
USER_AGENT = "drawbridge-tariff-ingest/1.0 (customs duty recovery; contact via repository)"

#: Search terms, chosen for spread across the schedule rather than for volume. Each names
#: goods in a different chapter, because a sample drawn from one term is a sample of one
#: heading's rulings and would make `find_rulings` look far better on that heading than it
#: is anywhere else.
DEFAULT_TERMS: tuple[str, ...] = (
    "portable automatic data processing machine",  # 8471
    "footwear textile upper",  # 6404
    "roasted coffee",  # 0901
    "woven polyester fabric",  # 5407
    "aluminum extrusion profile",  # 7604
    "printed circuit assembly",  # 8534
    "stainless steel fastener",  # 7318
    "plastic storage container",  # 3924
    "ceramic tableware",  # 6912
    "wooden furniture parts",  # 9403
    "rubber conveyor belt",  # 4010
    "electric motor",  # 8501
)

#: Between requests. CBP publishes no rate limit, so this is chosen to be obviously
#: unobjectionable rather than to be fast: a hundred rulings takes under two minutes and
#: nobody has to think about whether it was rude.
DEFAULT_DELAY = 0.4


class _TextOnly(HTMLParser):
    """Strip tags, keeping text. Used only when a body looks like markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _looks_like_markup(body: str) -> bool:
    lowered = body[:4000].lower()
    return any(tag in lowered for tag in ("<p>", "<br", "<div", "<table", "<html"))


def clean_body(raw: str) -> str:
    """Ruling text as prose.

    CROSS serves these as fixed-width plain text with carriage returns and heavy leading
    indentation — a 1989 ruling is literally the typewritten page. Collapsing the
    indentation matters for retrieval: `embed_corpus` embeds `subject || left(body, 4000)`,
    and forty columns of spaces at the start of every line is forty columns of the budget
    spent on nothing.

    Blank lines are preserved as paragraph breaks. The structure of a ruling — FACTS,
    ISSUE, LAW AND ANALYSIS, HOLDING — is carried entirely by them, and an auditor reading
    a cited ruling needs to find the holding.
    """
    text = raw
    if _looks_like_markup(text):  # pragma: no cover - not observed in the sampled corpus
        parser = _TextOnly()
        parser.feed(text)
        text = parser.text()
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    lines = [line.strip() for line in text.split("\n")]

    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


class Cross:
    """The two CROSS endpoints, with retries and a delay between calls."""

    def __init__(self, *, delay: float = DEFAULT_DELAY, timeout: float = 30.0) -> None:
        self.delay = delay
        self._client = httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One request, retried on the failures that are worth retrying.

        429 and 5xx are transient and back off. A 404 is not: `/api/ruling/{n}` returns one
        for a number the search returned but the detail store does not have, which happens,
        and retrying it three times would turn a known gap into a slow one.
        """
        delay = self.delay
        for attempt in range(4):
            time.sleep(delay)
            response = self._client.get(path, params=params)
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            if response.status_code < httpx.codes.BAD_REQUEST:
                return response.json()
            if response.status_code < httpx.codes.INTERNAL_SERVER_ERROR and (
                response.status_code != httpx.codes.TOO_MANY_REQUESTS
            ):
                msg = f"CROSS refused GET {path} with HTTP {response.status_code}"
                raise RuntimeError(msg)
            delay = min(delay * 4 or 1.0, 30.0)
            print(
                f"    HTTP {response.status_code} on {path}; retry {attempt + 1} in {delay:.0f}s",
                file=sys.stderr,
            )
        msg = f"CROSS did not answer GET {path} after 4 attempts"
        raise RuntimeError(msg)

    def search(self, term: str, *, page_size: int) -> list[dict[str, Any]]:
        payload = self._get(
            "/api/search",
            {
                "term": term,
                "collection": "ALL",
                "pageSize": page_size,
                "page": 1,
                # Newest first. A ruling that has been superseded is still worth holding
                # (see `services/ingest/src/cross.py`), but a sample skewed to 1989 would
                # cite the schedule as it stood before three renumberings.
                "sortBy": "DATEDESC",
            },
        )
        if not isinstance(payload, dict):
            return []
        rulings = payload.get("rulings")
        return [r for r in rulings if isinstance(r, dict)] if isinstance(rulings, list) else []

    def ruling(self, number: str) -> dict[str, Any] | None:
        payload = self._get(f"/api/ruling/{number}")
        return payload if isinstance(payload, dict) else None


def fetch(*, terms: tuple[str, ...], limit: int, per_term: int, delay: float) -> dict[str, Any]:
    """Search every term, then read rulings round-robin across the terms.

    Round-robin rather than term-by-term, and the difference is the whole point of having
    twelve terms. Draining the first term before starting the second means a `--limit` of
    100 against 25 hits per term produces a sample from four chapters and a docstring
    claiming twelve — which is how a corpus quietly becomes a corpus about laptops.

    Searched first, in full, and only then read. The search endpoint is one call per term;
    the detail endpoint is one call per ruling, and the delay dominates. Interleaving after
    the searches means the budget is spent on a spread rather than on whichever term
    happened to be first.
    """
    client = Cross(delay=delay)
    seen: dict[str, dict[str, Any]] = {}
    found_by: dict[str, str] = {}
    per_term_count: dict[str, int] = dict.fromkeys(terms, 0)
    try:
        queues: dict[str, list[str]] = {}
        for term in terms:
            numbers: list[str] = []
            for hit in client.search(term, page_size=per_term):
                number = str(hit.get("rulingNumber") or "").strip().upper()
                if number:
                    numbers.append(number)
            queues[term] = numbers
            print(f"  search  {term:<44} {len(numbers):>3} hit(s)")

        while len(seen) < limit and any(queues.values()):
            for term in terms:
                if len(seen) >= limit:
                    break
                queue = queues.get(term) or []
                while queue:
                    number = queue.pop(0)
                    if number in seen:
                        continue
                    detail = client.ruling(number)
                    if detail is None:
                        continue
                    body = clean_body(str(detail.get("text") or ""))
                    if not body:
                        # A ruling with no text is a citation to nothing. `find_rulings`
                        # returns the body to an analyst; an empty one is worse than
                        # absent, because it reads as a ruling that endorses the claim.
                        continue
                    detail["text"] = body
                    detail.setdefault("url", f"{BASE_URL}/ruling/{number}")
                    seen[number] = detail
                    found_by[number] = term
                    per_term_count[term] += 1
                    break
    finally:
        client.close()

    for term in terms:
        print(f"  drawn   {term:<44} {per_term_count[term]:>3}")

    return {
        "_about": (
            "CBP CROSS sample fetched by scripts/ingest_cross.py. Not the corpus: CROSS "
            "holds ~200k rulings and this is a term-drawn sample. `found_by` records which "
            "search term surfaced each ruling, so the selection is inspectable."
        ),
        "source": SOURCE,
        "endpoint": f"{BASE_URL}/api/ruling/{{rulingNumber}}",
        "fetched_at": datetime.now(UTC).isoformat(),
        "terms": list(terms),
        "found_by": found_by,
        "rulings": list(seen.values()),
    }


def write_snapshot(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def load(path: Path, dsn: str, *, dry_run: bool) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rulings") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        print(f"{path} holds no rulings array", file=sys.stderr)
        return 1

    records = list(parse_rows(rows))
    unusable = len(rows) - len(records)
    print(f"{path}")
    print(f"  parsed  {len(records)} ruling(s); {unusable} unusable (no code, or no date)")
    for record in records[:3]:
        print(f"    {record.ruling_number:<10} {record.classified_code:<12} {record.subject[:60]}")

    if dry_run:
        return 0

    engine = create_engine(dsn)
    with Session(engine) as session:
        report = load_rulings(session, records, source=SOURCE)
        session.commit()
    print(f"  loaded  inserted={report.inserted} updated={report.updated}")
    print("  embed with: python scripts/embed_corpus.py --table rulings")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8(sys.stdout)
    _utf8(sys.stderr)

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    fetcher = sub.add_parser("fetch", help="download a sample and write a snapshot")
    fetcher.add_argument("--limit", type=int, default=100, help="rulings to collect")
    fetcher.add_argument("--per-term", type=int, default=25, help="search hits per term")
    fetcher.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    fetcher.add_argument("--out", type=Path, default=None)
    fetcher.add_argument("--terms", nargs="*", default=None)
    fetcher.add_argument("--load", action="store_true", help="also load the snapshot")
    fetcher.add_argument("--dsn", default=None)

    loader = sub.add_parser("load", help="load a snapshot written by `fetch`")
    loader.add_argument("path", type=Path)
    loader.add_argument("--dsn", default=None)
    loader.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "load":
        return load(args.path, args.dsn or _dsn(), dry_run=args.dry_run)

    terms = tuple(args.terms) if args.terms else DEFAULT_TERMS
    print(f"fetching up to {args.limit} rulings from {BASE_URL}")
    payload = fetch(terms=terms, limit=args.limit, per_term=args.per_term, delay=args.delay)
    stamp = datetime.now(UTC).date().isoformat()
    out = args.out or Path("data") / f"cross_rulings_{stamp}.json"
    write_snapshot(payload, out)
    print(f"  wrote {len(payload['rulings'])} ruling(s) to {out}")

    if args.load:
        return load(out, args.dsn or _dsn(), dry_run=False)
    print(f"  load with: python scripts/ingest_cross.py load {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

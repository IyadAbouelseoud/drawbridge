"""Load a tariff corpus into Postgres.

    python scripts/ingest_tariff.py usitc  data/hts_2026_rev3.json --revision 2026-HTSA-rev3
    python scripts/ingest_tariff.py zatca  data/zatca_tariff_2025.csv --revision 2025-ICT
    python scripts/ingest_tariff.py cross  data/cross_rulings.json

Ingest is from a downloaded file, never from a live endpoint. A classification that reached
a filing must be reproducible years later, and a corpus assembled by a network call at load
time is reproducible only for as long as the publisher keeps the URL alive.

`--dry-run` parses and reports without writing, which is how a new export's column spellings
get checked before a partial load has to be unwound.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.ingest.src import cross, usitc, zatca
from services.ingest.src.tariff import (
    IngestReport,
    RulingRecord,
    TariffRecord,
    load_rulings,
    load_tariff_lines,
)

DEFAULT_DSN = "postgresql+psycopg://drawbridge:drawbridge@localhost:5432/drawbridge"


def _dsn() -> str:
    return os.environ.get("DRAWBRIDGE_DATABASE_URL", DEFAULT_DSN)


def _dry_run_lines(records: list[TariffRecord], source: str) -> IngestReport:
    """Report what a load would do, and surface a sample so the parse is inspectable."""
    report = IngestReport(source=source)
    report.inserted = len(records)
    for record in records[:3]:
        print(f"  {record.code}  {record.description_en[:88]}", file=sys.stderr)
        if record.description_ar:
            print(f"           ar: {record.description_ar[:70]}", file=sys.stderr)
    return report


def _utf8(stream: TextIO) -> None:
    """Force UTF-8 on a console stream, where the stream supports it.

    Only `TextIOWrapper` carries `reconfigure`; a redirected or wrapped stream may not,
    and a missing method is not a reason to fail a load.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    # ZATCA descriptions are Arabic; a Windows console defaults to cp1252.
    _utf8(sys.stdout)
    _utf8(sys.stderr)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", choices=("usitc", "zatca", "cross"))
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--revision",
        help="Schedule edition, e.g. 2026-HTSA-rev3. Required for a schedule; "
        "meaningless for rulings, which carry their own dates.",
    )
    parser.add_argument(
        "--effective-from",
        type=date.fromisoformat,
        help="Date this revision took effect. Defaults to the revision's publication "
        "year on 1 January, which is wrong often enough that it should be passed.",
    )
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 2

    if args.corpus in {"usitc", "zatca"} and not args.revision:
        print(f"--revision is required for the {args.corpus} schedule", file=sys.stderr)
        return 2

    effective_from = args.effective_from or date(date.today().year, 1, 1)

    records: list[TariffRecord] = []
    rulings: list[RulingRecord] = []

    if args.corpus == "usitc":
        records = list(
            usitc.parse_file(args.path, revision=args.revision, effective_from=effective_from)
        )
    elif args.corpus == "zatca":
        records = list(
            zatca.parse_file(args.path, revision=args.revision, effective_from=effective_from)
        )
    else:
        rulings = list(cross.parse_file(args.path))

    source = (
        usitc.SOURCE
        if args.corpus == "usitc"
        else (zatca.SOURCE if args.corpus == "zatca" else cross.SOURCE)
    )

    if args.dry_run:
        if records:
            report = _dry_run_lines(records, source)
        else:
            report = IngestReport(source=source)
            report.inserted = len(rulings)
            for ruling in rulings[:3]:
                print(
                    f"  {ruling.ruling_number}  {ruling.classified_code}  {ruling.subject[:70]}",
                    file=sys.stderr,
                )
        print(json.dumps({"dry_run": True, **report.as_dict()}, indent=2))
        return 0

    engine = create_engine(args.dsn or _dsn())
    with Session(engine) as session:
        report = (
            load_tariff_lines(session, records, source=source)
            if records
            else load_rulings(session, rulings, source=source)
        )
        session.commit()

    print(json.dumps(report.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

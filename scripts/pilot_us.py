"""Seed the US pilot: one importer, five-year-old entries, refund still recoverable.

    python scripts/pilot_us.py --write-payload pilot/

**Why the imports are old and the exports are not.** A backward-looking claim reaches
back for duty already paid, and the US statute bounds that reach twice. §1313(j) gives
five years from *import* to export the goods, and §1313(r) gives three years from *export*
to file. Seeding both dates five years back would produce a corpus whose filing window
closed in 2023 — a pilot that proves the deadline engine works and recovers nothing.

So the entries are genuinely five years old and the exports sit between them and today,
which is the shape of the real thing: goods brought in against the 2021 Section 301 rates,
sold on into Europe two and a half years later, and the duty still claimable this year.
One line is deliberately outside the window so the pilot exercises a rejection as well as
an allowance.

The corpus is fiction and every figure says so — see `scripts/pilot_common.py`. Nothing
built from it may be filed.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from drawbridge_schemas.provenance import DocumentKind, Language
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode
from scripts.pilot_common import (
    PilotCorpus,
    PilotError,
    PilotTenant,
    document_ref,
    fixture_provenance,
    owner_session,
    pilot_uuid,
    report,
    seed,
    trigger_payload,
)

TENANT = PilotTenant(
    slug="us-northbridge",
    name="Northbridge Trading LLC (pilot)",
    jurisdiction="us",
)

ENTRY_FIGURES = (
    "quantity",
    "entered_value",
    "duty_paid",
    "mpf_paid",
    "hmf_paid",
    "section_301_duty",
)
EXPORT_FIGURES = ("quantity", "declared_value")


def _entry(
    *,
    declaration: str,
    line_number: int,
    import_date: date,
    hts: str,
    description: str,
    quantity: Decimal,
    entered_value: Decimal,
    duty_paid: Decimal,
    section_301: Decimal,
    mpf_paid: Decimal,
) -> EntryLine:
    ref = document_ref(
        DocumentKind.CBP_7501, f"us/{declaration}-{line_number}", language=Language.ENGLISH
    )
    return EntryLine(
        line_id=pilot_uuid("entry", declaration, str(line_number)),
        tenant_id=TENANT.tenant_id,
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        declaration_number=declaration,
        line_number=line_number,
        import_date=import_date,
        # Filed six days after arrival, which is inside the ten working days CBP allows
        # and is what the entry summary in a real 7501 packet looks like.
        declaration_date=date.fromordinal(import_date.toordinal() + 6),
        port_of_entry="2704",
        country_of_origin="CN",
        hts=HTSCode(code=hts),
        description=description,
        quantity=quantity,
        unit_of_measure="NO",
        entered_value=entered_value,
        duty_paid=duty_paid,
        section_301_duty=section_301,
        mpf_paid=mpf_paid,
        hmf_paid=Decimal("0.00"),
        provenance=fixture_provenance(ref, ENTRY_FIGURES),
    )


def _export(
    *,
    reference: str,
    line_number: int,
    export_date: date,
    hts: str,
    description: str,
    quantity: Decimal,
    declared_value: Decimal,
    destination: str = "DE",
) -> ExportLine:
    ref = document_ref(
        DocumentKind.CBP_7501, f"us/{reference}-{line_number}", language=Language.ENGLISH
    )
    return ExportLine(
        line_id=pilot_uuid("export", reference, str(line_number)),
        tenant_id=TENANT.tenant_id,
        jurisdiction=Jurisdiction.US,
        reference=reference,
        line_number=line_number,
        export_date=export_date,
        destination_country=destination,
        hts=HTSCode(code=hts),
        description=description,
        quantity=quantity,
        unit_of_measure="NO",
        declared_value=declared_value,
        provenance=fixture_provenance(ref, EXPORT_FIGURES),
    )


def build_corpus() -> PilotCorpus:
    """Three entries from 2021, three exports, one of them out of time."""
    imports = (
        _entry(
            declaration="AAA-2104417-1",
            line_number=1,
            import_date=date(2021, 6, 14),
            hts="8471300100",
            description="Portable automatic data processing machines, 10.1in display",
            quantity=Decimal("4000"),
            entered_value=Decimal("1000000.00"),
            duty_paid=Decimal("0.00"),
            section_301=Decimal("250000.00"),
            mpf_paid=Decimal("3456.00"),
        ),
        _entry(
            declaration="AAA-2104417-1",
            line_number=2,
            import_date=date(2021, 6, 14),
            hts="8443310000",
            description="Multifunction print/copy/scan units",
            quantity=Decimal("1200"),
            entered_value=Decimal("300000.00"),
            duty_paid=Decimal("0.00"),
            section_301=Decimal("75000.00"),
            mpf_paid=Decimal("1036.80"),
        ),
        _entry(
            declaration="AAA-2107733-9",
            line_number=1,
            import_date=date(2021, 9, 2),
            hts="8528521000",
            description="Monitors capable of directly connecting to an ADP machine",
            quantity=Decimal("2500"),
            entered_value=Decimal("437500.00"),
            duty_paid=Decimal("21875.00"),
            section_301=Decimal("109375.00"),
            mpf_paid=Decimal("1512.00"),
        ),
    )

    exports = (
        # Inside both windows: 2.6 years after import, filable until 2027-01.
        _export(
            reference="MAEU241033871",
            line_number=1,
            export_date=date(2024, 1, 18),
            hts="8471300150",
            description="Portable ADP machines, re-exported unused",
            quantity=Decimal("1600"),
            declared_value=Decimal("392000.00"),
        ),
        _export(
            reference="MAEU241033871",
            line_number=2,
            export_date=date(2024, 1, 18),
            hts="8443310000",
            description="Multifunction print units, re-exported unused",
            quantity=Decimal("450"),
            declared_value=Decimal("110250.00"),
            destination="NL",
        ),
        # Out of time on purpose: exported 2022-03, so §1313(r) closed the filing window
        # in March 2025. A pilot that only ever sees allowable lines does not exercise
        # the half of the deadline engine that says no.
        _export(
            reference="MAEU220417265",
            line_number=1,
            export_date=date(2022, 3, 30),
            hts="8528521000",
            description="Monitors, re-exported unused",
            quantity=Decimal("900"),
            declared_value=Decimal("157500.00"),
            destination="GB",
        ),
    )

    # Duty at risk over the two lines still inside the filing window, allocated pro rata
    # on quantity. Stated so week 13 has a number to reproduce rather than a number to
    # accept; the matcher computes the real allocation and the two should agree.
    computer_share = Decimal("250000.00") * Decimal("1600") / Decimal("4000")
    printer_share = Decimal("75000.00") * Decimal("450") / Decimal("1200")

    return PilotCorpus(
        tenant=TENANT,
        jurisdiction=Jurisdiction.US,
        imports=imports,
        exports=exports,
        expected_base=(computer_share + printer_share).quantize(Decimal("0.01")),
        caveats=(
            "every figure carries a pilot-fixture box and traces to no document; "
            "nothing built from this corpus may be filed",
            "MAEU220417265 is deliberately outside the 19 U.S.C. §1313(r) filing window "
            "and should be rejected, not matched",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the US pilot tenant.")
    parser.add_argument(
        "--write-payload",
        type=Path,
        default=None,
        help="directory to write the n8n trigger body into",
    )
    parser.add_argument("--dry-run", action="store_true", help="build and print, write nothing")
    args = parser.parse_args(argv)

    corpus = build_corpus()

    if args.dry_run:
        print(
            report(
                corpus,
                {
                    "tenant": corpus.tenant.name,
                    "tenant_id": str(corpus.tenant.tenant_id),
                    "jurisdiction": corpus.jurisdiction.value,
                    "imports": len(corpus.imports),
                    "exports": len(corpus.exports),
                },
            )
        )
        return 0

    try:
        with owner_session() as session:
            written = seed(corpus, session)
    except PilotError as exc:
        print(f"refusing: {exc}")
        return 1

    print(report(corpus, written))

    if args.write_payload:
        args.write_payload.mkdir(parents=True, exist_ok=True)
        path = args.write_payload / f"{TENANT.slug}-trigger.json"
        path.write_text(json.dumps(trigger_payload(corpus), indent=2), encoding="utf-8")
        print(f"payload     {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

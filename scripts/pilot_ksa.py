"""Seed the KSA pilot: one re-exporter, and a conflict worth stating before week 13.

    python scripts/pilot_ksa.py --write-payload pilot/
    python scripts/pilot_ksa.py --time-barred    # the five-year corpus, for the rejection

**The brief asked for five-year-old data and GCC law does not allow it.** Common Customs
Law Art. 174 bars any claim for duties paid more than three years ago, and the profile in
`drawbridge_schemas.jurisdiction` encodes it as an absolute bar with no discretion. A KSA
corpus dated 2021 does not produce a small refund or a contested one; it produces zero,
because every line is dead before the matcher sees it. That is not a defect in the seed
data — it is the statute — and quietly aging the corpus forward without saying so would
hide the one fact that most shapes what a Saudi pilot can be.

So the default corpus is dated to the oldest duty payments still inside Art. 174, which is
where a real backward-looking KSA engagement has to start. The literal five-year corpus is
kept behind `--time-barred`, because week 13 should also be able to show a customer *why*
their older entries are gone, and a rejection nobody can reproduce is an assertion.

Three gates stack in this lane and the corpus exercises all of them: one Gregorian year
from duty payment to re-export (Art. 16 §3(a)), six Gregorian months from re-export to
filing (§3(b)), and the USD 5,000 minimum per re-export (§2). One line sits just under the
minimum on purpose.

The corpus is fiction and every figure says so — see `scripts/pilot_common.py`.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from drawbridge_schemas.provenance import DocumentKind, Language
from drawbridge_schemas.tenant import TenantProfile
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode, ValuationBasis
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

_SLUG = "ksa-alrajhi-logistics"

TENANT = PilotTenant(
    slug=_SLUG,
    name="Al-Rajhi Regional Logistics Co. (pilot)",
    jurisdiction="ksa",
    # The IBAN is the field that matters here and it is a real mod-97 check digit over a
    # fictional account: ZATCA settles an approved refund to this number, so a corpus
    # carrying one that fails validation would exercise the validator instead of the
    # payment instruction. 24 characters, as every Saudi IBAN is.
    profile=TenantProfile(
        tenant_id=pilot_uuid("tenant", _SLUG),
        legal_name="Al-Rajhi Regional Logistics Co.",
        cr_number="4030298871",
        vat_number="310298871400003",
        iban="SA0380000000608010167519",
        address_line1="Al-Rawdah District, King Abdulaziz Road",
        city="Jeddah",
        postal_code="23434",
        country="SA",
        contact_email="customs@alrajhi-logistics.example",
        contact_phone="+966-12-555-0177",
    ),
)

ENTRY_FIGURES = ("quantity", "entered_value", "duty_paid", "vat_paid")
EXPORT_FIGURES = ("quantity", "declared_value")

# 3.75 SAR/USD is the peg, used here only to place the USD 5,000 threshold cases either
# side of the line. The claim path itself never hardcodes it — week 4 removed that and
# anchors the rate to the duty-payment date (Valuation Art. 1(I)(6)).
_PEG = Decimal("3.75")


def _entry(
    *,
    declaration: str,
    line_number: int,
    duty_payment: date,
    hts: str,
    description: str,
    quantity: Decimal,
    entered_value: Decimal,
    duty_paid: Decimal,
) -> EntryLine:
    ref = document_ref(
        DocumentKind.ZATCA_BAYAN, f"ksa/{declaration}-{line_number}", language=Language.MIXED
    )
    # Goods clear, then duty is paid against guarantee up to thirty days later. The gap is
    # the point: the GCC clock runs from payment, not from arrival, and a corpus where the
    # two dates coincide would never catch a matcher that used the wrong one.
    arrival = date.fromordinal(duty_payment.toordinal() - 21)
    return EntryLine(
        line_id=pilot_uuid("entry", declaration, str(line_number)),
        tenant_id=TENANT.tenant_id,
        jurisdiction=Jurisdiction.KSA,
        currency=Currency.SAR,
        declaration_number=declaration,
        line_number=line_number,
        import_date=arrival,
        declaration_date=arrival,
        duty_payment_date=duty_payment,
        port_of_entry="Jeddah Islamic Port",
        country_of_origin="CN",
        hts=HTSCode(code=hts),
        description=description,
        quantity=quantity,
        unit_of_measure="NO",
        entered_value=entered_value,
        duty_paid=duty_paid,
        # 15% import VAT, recovered through the VAT return as input tax and never part of
        # a drawback base (COMPLIANCE-GCC.md §2.4). Carried so reconciliation can show it
        # was excluded deliberately rather than forgotten.
        vat_paid=((entered_value + duty_paid) * Decimal("0.15")).quantize(Decimal("0.01")),
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
    linked_declaration: str,
    destination: str = "AE",
) -> ExportLine:
    ref = document_ref(
        DocumentKind.ZATCA_REEXPORT_DECLARATION,
        f"ksa/{reference}-{line_number}",
        language=Language.MIXED,
    )
    return ExportLine(
        line_id=pilot_uuid("export", reference, str(line_number)),
        tenant_id=TENANT.tenant_id,
        jurisdiction=Jurisdiction.KSA,
        reference=reference,
        line_number=line_number,
        export_date=export_date,
        destination_country=destination,
        hts=HTSCode(code=hts),
        description=description,
        quantity=quantity,
        unit_of_measure="NO",
        declared_value=declared_value,
        valuation_basis=ValuationBasis.ART_28_EXPORT,
        # Art. 15(c): the import declaration number on the re-export declaration is what
        # makes an Art. 97 claim possible at all. A line without it is not a weak claim,
        # it is not a claim.
        linked_import_declaration=linked_declaration,
        unused_and_unaltered=True,
        provenance=fixture_provenance(ref, EXPORT_FIGURES),
    )


# Offsets from the filing date, in days. Every KSA date is derived rather than written
# down, and that is a correction rather than a preference.
#
# Week 12 wrote literal dates and never ran the corpus through the rules engine. By the
# time week 13 did, the clean re-export was 461 days behind the filing date and Art. 16
# §3(b) had closed on it — so the corpus asserted a recoverable SAR 54,687.50 that no
# filing date could actually produce. The GCC lane cannot hold fixed dates: six months
# from re-export to filing means any literal corpus expires within two quarters of being
# written, and expires silently.
#
# The three gates, and where each offset sits inside it:
#   Art. 174        duty paid within 3 years of filing      210 days  (of 1095)
#   Art. 16 §3(a)   re-exported within 1 year of payment     90 days  (of 365)
#   Art. 16 §3(b)   filed within 6 months of re-export      120 days  (of ~182)
_DUTY_PAID_AGO = 210
_REEXPORT_AGO = 120

# The second declaration exists to fail §3(a) and only §3(a): paid 500 days before filing,
# re-exported on the same day as the others, which is 380 days after payment — outside the
# one-year window while still inside the six-month filing window. A line that failed two
# gates would not show which one did the work.
_LATE_DUTY_PAID_AGO = 500


def build_corpus(*, time_barred: bool = False, as_of: date | None = None) -> PilotCorpus:
    """Two declarations, three re-exports, one below the Art. 16 §2 minimum.

    Dates are relative to `as_of`, the date the claim would be filed on. See the offsets
    above for why: a GCC corpus with literal dates goes stale in a quarter.

    `time_barred` moves the duty payments back beyond four years, outside Art. 174, and
    should produce a claim of zero with the article named in the rejection.
    """
    today = as_of or date.today()

    def ago(days: int) -> date:
        return date.fromordinal(today.toordinal() - days)

    shift = 4 * 365 if time_barred else 0

    def back(value: date) -> date:
        return date.fromordinal(value.toordinal() - shift)

    imports = (
        _entry(
            declaration="21001-2024-0447213",
            line_number=1,
            duty_payment=back(ago(_DUTY_PAID_AGO)),
            hts="847130000",
            description="حاسبات آلية محمولة / Portable computers",
            quantity=Decimal("3000"),
            entered_value=Decimal("2343750.00"),
            duty_paid=Decimal("117187.50"),
        ),
        _entry(
            declaration="21001-2024-0447213",
            line_number=2,
            duty_payment=back(ago(_DUTY_PAID_AGO)),
            hts="844331000",
            description="طابعات / Printers",
            quantity=Decimal("850"),
            entered_value=Decimal("212500.00"),
            duty_paid=Decimal("10625.00"),
        ),
        _entry(
            declaration="21001-2025-0113988",
            line_number=1,
            duty_payment=back(ago(_LATE_DUTY_PAID_AGO)),
            hts="852852000",
            description="شاشات عرض / Display monitors",
            quantity=Decimal("1250"),
            entered_value=Decimal("312500.00"),
            duty_paid=Decimal("15625.00"),
        ),
    )

    exports = (
        # 90 days after payment: inside the one-year re-export window, and filed 120
        # days later, inside the six months that then start running. The only line in the
        # corpus that clears all three gates.
        _export(
            reference="RX-2025-0091447",
            line_number=1,
            export_date=back(ago(_REEXPORT_AGO)),
            hts="847130000",
            description="حاسبات آلية محمولة معاد تصديرها دون استعمال",
            quantity=Decimal("1400"),
            declared_value=Decimal("1093750.00"),
            linked_declaration="21001-2024-0447213",
        ),
        # Under the gate: SAR 18,000 is USD 4,800 at the peg, short of the Art. 16 §2
        # minimum of USD 5,000. Week 9's triage calls this a near miss and routes it to an
        # analyst rather than dropping it, which is the behaviour the pilot should show a
        # customer before it shows them a refund.
        _export(
            reference="RX-2025-0091448",
            line_number=1,
            export_date=back(ago(_REEXPORT_AGO)),
            hts="844331000",
            description="طابعات معاد تصديرها دون استعمال",
            quantity=Decimal("60"),
            declared_value=(Decimal("4800.00") * _PEG).quantize(Decimal("0.01")),
            linked_declaration="21001-2024-0447213",
            destination="BH",
        ),
        # Outside Art. 16 §3(a): re-exported 380 days after the duty was paid. Filed
        # inside the six-month window, so §3(a) is the only gate this line fails.
        _export(
            reference="RX-2026-0044120",
            line_number=1,
            export_date=back(ago(_REEXPORT_AGO)),
            hts="852852000",
            description="شاشات عرض معاد تصديرها دون استعمال",
            quantity=Decimal("500"),
            declared_value=Decimal("125000.00"),
            linked_declaration="21001-2025-0113988",
        ),
    )

    # Only the first re-export clears all three gates. Duty allocated on quantity, at the
    # Art. 16 §6 rate of duties actually paid — no haircut, unlike the US lane.
    recoverable = (Decimal("117187.50") * Decimal("1400") / Decimal("3000")).quantize(
        Decimal("0.01")
    )

    caveats = [
        "every figure carries a pilot-fixture box and traces to no document; "
        "nothing built from this corpus may be filed",
        "RX-2025-0091448 is under the Art. 16 §2 USD 5,000 minimum and should reach an "
        "analyst as a near miss, not be silently dropped",
        "RX-2026-0044120 is outside the Art. 16 §3(a) one-year window and should be rejected",
    ]
    if time_barred:
        caveats.insert(
            0,
            "TIME-BARRED CORPUS: duty paid more than three years ago (Common Customs Law "
            "Art. 174). Expect a refund of zero — this corpus exists to demonstrate that, "
            "not to file",
        )

    return PilotCorpus(
        tenant=TENANT,
        jurisdiction=Jurisdiction.KSA,
        imports=imports,
        exports=exports,
        expected_base=Decimal("0.00") if time_barred else recoverable,
        caveats=tuple(caveats),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the KSA pilot tenant.")
    parser.add_argument(
        "--time-barred",
        action="store_true",
        help="date the corpus five years back, outside Art. 174 — expect zero",
    )
    parser.add_argument(
        "--write-payload",
        type=Path,
        default=None,
        help="directory to write the n8n trigger body into",
    )
    parser.add_argument("--dry-run", action="store_true", help="build and print, write nothing")
    args = parser.parse_args(argv)

    corpus = build_corpus(time_barred=args.time_barred)

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
        suffix = "-time-barred" if args.time_barred else ""
        path = args.write_payload / f"{TENANT.slug}{suffix}-trigger.json"
        path.write_text(json.dumps(trigger_payload(corpus), indent=2), encoding="utf-8")
        print(f"payload     {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

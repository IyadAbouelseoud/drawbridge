"""Fasah sandbox probe — does a partial re-export link to a full-quantity import *Bayan*?

This is the week 6 answer to `docs/COMPLIANCE-GCC.md` §8.2. The law permits part shipments
(Rules of Implementation Art. 16 §4, Common Customs Law Art. 44(b)); whether the *platform*
accepts a re-export declaration whose quantity is below its linked import line is a
platform behaviour that no public ZATCA or Tabadul material states.

**This script does not transmit.** The Fasah sandbox is gated behind credentials issued to
a registered customs broker, which this environment does not hold, and firing a speculative
declaration at a customs platform on an assumption is not a thing to do quietly. What it
does instead:

    python scripts/fasah_sandbox_probe.py            # the four payloads, as JSON
    python scripts/fasah_sandbox_probe.py --curl     # the exact cURL commands to run
    python scripts/fasah_sandbox_probe.py --record   # write the results template

Run the cURL commands against the sandbox with real credentials, paste each response into
`scripts/fasah_probe_results.json`, and §8.2 is then updated from that file.

The four probes are designed so each one isolates exactly one unknown, and so a failure at
probe B is distinguishable from a failure that would also have hit probe A:

    A  full-quantity re-export, separate Bayan   -> does the link mechanism work at all
    B  40% partial against a fresh Bayan         -> is a below-quantity link accepted
    C  the residual 60% against the same Bayan   -> does residual quantity survive
    D  a fourth draw exceeding the residual      -> what the platform says on over-draw

Probe D is expected to fail. Its *error code* is the finding — a platform that silently
accepts an over-draw is a materially different compliance risk from one that rejects it,
and the engine's Art. 44(b) no-loss-to-treasury guard is calibrated differently in each
case.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

# Fasah / ZATCA sandbox. Documented as the integration host for Tabadul's single window;
# the path segments below follow the declaration-service shape published in the Fasah
# developer material. Both are overridable, because a sandbox that moves should not require
# a code change to probe.
DEFAULT_BASE_URL = "https://sandbox.fasah.sa"
DECLARATION_PATH = "/api/v1/customs/declarations"
DECLARATION_STATUS_PATH = "/api/v1/customs/declarations/{declaration_id}"

RESULTS_PATH = Path(__file__).parent / "fasah_probe_results.json"


class DeclarationKind(StrEnum):
    """Bayan type. Only the re-export form carries a drawback linkage."""

    IMPORT = "IMPORT"
    RE_EXPORT = "RE_EXPORT"


class ProbeExpectation(StrEnum):
    """What the probe is testing for, so a result is legible without the prose above."""

    BASELINE_ACCEPT = "baseline_accept"
    """Establishes the link mechanism works. A failure here invalidates B, C and D."""

    UNDER_TEST = "under_test"
    """The actual open question. Either outcome is a finding."""

    EXPECTED_REJECT = "expected_reject"
    """Acceptance here is the alarming result, not the rejection."""


@dataclass(frozen=True, slots=True)
class BayanLine:
    """One line of an import declaration, as the probe needs to reference it.

    Quantities are `Decimal` and serialised as strings. A customs quantity that round-trips
    through a float is a quantity that can be off by a unit at the fourth decimal place,
    and the whole point of the probe is to learn what the platform does at an exact
    quantity boundary.
    """

    declaration_number: str
    line_number: int
    hs_code: str
    description_en: str
    description_ar: str
    quantity: Decimal
    unit_of_measure: str
    declared_value: Decimal
    currency: str = "SAR"
    country_of_origin: str = "CN"
    duty_paid: Decimal = Decimal("0.00")
    duty_payment_date: date = date(2025, 3, 10)


@dataclass(frozen=True, slots=True)
class Probe:
    """One request in the sequence, with the question it isolates."""

    ref: str
    title: str
    question: str
    expectation: ProbeExpectation
    source_line: BayanLine
    reexport_quantity: Decimal
    reexport_value: Decimal
    consignment_id: str
    is_partial_shipment: bool
    notes: str = ""
    depends_on: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- fixtures

# Two distinct import declarations. Probe A consumes one entirely; B, C and D draw against
# the other in sequence. Sharing a single Bayan between the baseline and the partial series
# would make an A-failure and a B-failure indistinguishable.
BAYAN_BASELINE = BayanLine(
    declaration_number="20250310000000001",
    line_number=1,
    hs_code="847130",
    description_en="Portable automatic data processing machines, weight <= 10 kg",
    description_ar="آلات معالجة بيانات أوتوماتيكية محمولة لا يتجاوز وزنها ١٠ كجم",
    quantity=Decimal("100.0000"),
    unit_of_measure="PCE",
    declared_value=Decimal("375000.00"),
    duty_paid=Decimal("18750.00"),
)

BAYAN_PARTIAL = BayanLine(
    declaration_number="20250310000000002",
    line_number=1,
    hs_code="847130",
    description_en="Portable automatic data processing machines, weight <= 10 kg",
    description_ar="آلات معالجة بيانات أوتوماتيكية محمولة لا يتجاوز وزنها ١٠ كجم",
    quantity=Decimal("100.0000"),
    unit_of_measure="PCE",
    declared_value=Decimal("375000.00"),
    duty_paid=Decimal("18750.00"),
)

# Unit value carried straight through from the import line, so every probe's declared
# re-export value is exactly proportional to its quantity. A platform rejection must then
# be attributable to the quantity and nothing else.
_UNIT_VALUE = BAYAN_PARTIAL.declared_value / BAYAN_PARTIAL.quantity

PROBES: tuple[Probe, ...] = (
    Probe(
        ref="A",
        title="Full-quantity re-export against its own Bayan",
        question="Does the Art. 15(c) declaration link work at all on this sandbox?",
        expectation=ProbeExpectation.BASELINE_ACCEPT,
        source_line=BAYAN_BASELINE,
        reexport_quantity=Decimal("100.0000"),
        reexport_value=Decimal("375000.00"),
        consignment_id="CONS-BASELINE-001",
        is_partial_shipment=False,
        notes=(
            "Baseline. If this is rejected the problem is the link mechanism or the "
            "credentials, not partial quantity, and B-D tell us nothing."
        ),
    ),
    Probe(
        ref="B",
        title="Partial re-export (40%) against a full-quantity Bayan",
        question=(
            "Does Fasah accept a re-export declaration whose quantity is below the "
            "linked import line? This is the §8.2 question."
        ),
        expectation=ProbeExpectation.UNDER_TEST,
        source_line=BAYAN_PARTIAL,
        reexport_quantity=Decimal("40.0000"),
        reexport_value=(_UNIT_VALUE * Decimal("40")).quantize(Decimal("0.01")),
        consignment_id="CONS-PARTIAL-002",
        is_partial_shipment=True,
        notes=(
            "40 units at 3750.00 SAR = 150000.00 SAR = 40000.00 USD at the 3.75 peg, "
            "comfortably clear of the Art. 16 §2 USD 5,000 minimum. The threshold must "
            "not be what fails here, or the result is unattributable."
        ),
        depends_on=("A",),
    ),
    Probe(
        ref="C",
        title="Residual re-export (60%) against the same Bayan",
        question=(
            "After a partial draw, does the residual quantity remain available for a "
            "second re-export against the same import line?"
        ),
        expectation=ProbeExpectation.UNDER_TEST,
        source_line=BAYAN_PARTIAL,
        reexport_quantity=Decimal("60.0000"),
        reexport_value=(_UNIT_VALUE * Decimal("60")).quantize(Decimal("0.01")),
        consignment_id="CONS-PARTIAL-002",
        is_partial_shipment=True,
        notes=(
            "Same consignment_id as B — this is the Art. 16 §4 'part shipments of the "
            "same consignment' case, not two unrelated consignments."
        ),
        depends_on=("A", "B"),
    ),
    Probe(
        ref="D",
        title="Over-draw beyond the residual",
        question=(
            "What does the platform return when the cumulative re-exported quantity "
            "would exceed the import line? Silent acceptance is the dangerous answer."
        ),
        expectation=ProbeExpectation.EXPECTED_REJECT,
        source_line=BAYAN_PARTIAL,
        reexport_quantity=Decimal("1.0000"),
        reexport_value=_UNIT_VALUE.quantize(Decimal("0.01")),
        consignment_id="CONS-PARTIAL-002",
        is_partial_shipment=True,
        notes=(
            "B + C already exhaust the 100 units. One further unit is the smallest "
            "possible over-draw, so a rejection here is unambiguously about the "
            "cumulative ceiling and not about a large or malformed quantity. Record the "
            "error code verbatim — Art. 44(b)'s 'no loss to the treasury' guard is "
            "calibrated differently depending on whether the platform enforces this."
        ),
        depends_on=("A", "B", "C"),
    ),
)


# ---------------------------------------------------------------------- payload build


def _money(value: Decimal) -> str:
    """Two-decimal string. Never a float — see `BayanLine`."""
    return str(value.quantize(Decimal("0.01")))


def _quantity(value: Decimal) -> str:
    """Four-decimal string, matching the Numeric(18,4) quantity column."""
    return str(value.quantize(Decimal("0.0001")))


def build_payload(probe: Probe, *, reexport_date: date) -> dict[str, Any]:
    """The re-export *Bayan* body for one probe.

    Field naming follows the Fasah declaration service shape. Where the platform's exact
    key is not published, the GCC-law name is used and flagged in `_probe` below so a
    rejection on an unknown field is distinguishable from a rejection on the quantity —
    which is the whole point of the exercise.
    """
    line = probe.source_line
    return {
        "declarationType": DeclarationKind.RE_EXPORT,
        "declarationDate": reexport_date.isoformat(),
        "customsOffice": "SAJED",  # Jeddah Islamic Port. Any office with a drawback desk.
        "declarantIdentity": {
            "type": "CR",
            "number": "REPLACE_WITH_SANDBOX_CR_NUMBER",
            "name": "REPLACE_WITH_SANDBOX_ENTITY_NAME",
        },
        # Rules of Implementation Art. 15(c): the import declaration number must be
        # affixed to the re-export declaration. This object is the whole experiment.
        "linkedImportDeclaration": {
            "declarationNumber": line.declaration_number,
            "lineNumber": line.line_number,
            "importedQuantity": _quantity(line.quantity),
            "dutyPaid": _money(line.duty_paid),
            "dutyPaymentDate": line.duty_payment_date.isoformat(),
        },
        "consignment": {
            "consignmentId": probe.consignment_id,
            "isPartShipment": probe.is_partial_shipment,
            # Art. 16 §4 requires part shipments to be "definitely proven" to belong to
            # one consignment. Whether the platform wants evidence at submission or on
            # audit is itself part of what the probe learns.
            "partShipmentEvidence": (
                "Commercial invoice and packing list referencing the single inbound "
                "consignment; available on request."
                if probe.is_partial_shipment
                else None
            ),
        },
        "lines": [
            {
                "lineNumber": 1,
                "hsCode": line.hs_code,
                "goodsDescriptionEn": line.description_en,
                "goodsDescriptionAr": line.description_ar,
                "quantity": _quantity(probe.reexport_quantity),
                "unitOfMeasure": line.unit_of_measure,
                # Common Customs Law Art. 28: declared value plus costs to the customs
                # office. Not CIF, not bare FOB — COMPLIANCE-GCC.md §8.1.
                "declaredValue": _money(probe.reexport_value),
                "valuationBasis": "ART_28_EXPORT",
                "currency": line.currency,
                "countryOfOrigin": line.country_of_origin,
                "countryOfDestination": "AE",
                # Art. 16 §5.
                "condition": "UNUSED_UNALTERED",
            }
        ],
        "drawbackRequest": {
            "requested": True,
            "basis": "GCC_COMMON_CUSTOMS_LAW_ART_97",
            # Deliberately not asserting a Resolution 28624 article — COMPLIANCE-GCC.md
            # §8.4.1. If the platform requires one, the rejection tells us which.
            "proceduralCitation": None,
        },
        "_probe": {
            "ref": probe.ref,
            "expectation": probe.expectation,
            "question": probe.question,
            "unverifiedFieldNames": [
                "consignment.partShipmentEvidence",
                "drawbackRequest.proceduralCitation",
            ],
            "note": (
                "Field names marked unverified are not published in the Fasah developer "
                "material. A 400 naming one of them is a schema finding, not an answer "
                "to the partial-quantity question — resubmit without it before "
                "concluding anything."
            ),
        },
    }


def build_curl(probe: Probe, *, base_url: str, reexport_date: date) -> str:
    """A single-line cURL for one probe.

    `--fail-with-body` rather than `--fail`: probe D is *expected* to fail, and its
    response body is the finding. Discarding it would throw away the result.
    """
    payload = build_payload(probe, reexport_date=reexport_date)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return " ".join(
        [
            "curl --fail-with-body -sS -X POST",
            shlex.quote(f"{base_url}{DECLARATION_PATH}"),
            "-H 'Content-Type: application/json'",
            "-H 'Accept: application/json'",
            '-H "Authorization: Bearer $FASAH_SANDBOX_TOKEN"',
            '-H "X-Client-Id: $FASAH_CLIENT_ID"',
            "-d",
            shlex.quote(body),
        ]
    )


def build_status_curl(*, base_url: str) -> str:
    """Follow-up read. The submission response may not carry the linkage verdict.

    Some declaration services accept synchronously and validate asynchronously, in which
    case a 201 on probe D would mean nothing on its own.
    """
    path = DECLARATION_STATUS_PATH.format(declaration_id="$DECLARATION_ID")
    return " ".join(
        [
            "curl -sS",
            shlex.quote(f"{base_url}{path}"),
            "-H 'Accept: application/json'",
            '-H "Authorization: Bearer $FASAH_SANDBOX_TOKEN"',
        ]
    )


# --------------------------------------------------------------------------- results


def results_template() -> dict[str, Any]:
    """The shape §8.2 will be updated from.

    Fixed keys, one entry per probe, `null` where the answer is not yet known. A template
    rather than free-form notes because the four questions must each be answered
    explicitly — an omission should be visible as a `null`, not as an absence.
    """
    return {
        "schema": "drawbridge/fasah-probe-result/1",
        "recorded_at": None,
        "recorded_by": None,
        "sandbox_base_url": DEFAULT_BASE_URL,
        "credentials_source": None,
        "probes": {
            probe.ref: {
                "title": probe.title,
                "question": probe.question,
                "expectation": probe.expectation,
                "submitted": False,
                "http_status": None,
                "declaration_id": None,
                "accepted": None,
                "error_code": None,
                "response_body": None,
                "observation": None,
            }
            for probe in PROBES
        },
        "conclusion": {
            "partial_link_accepted": None,
            "residual_remains_available": None,
            "prior_art_44b_ruling_required": None,
            "over_draw_behaviour": None,
            "compliance_gcc_md_section_8_2_updated": False,
        },
    }


def _serialise(value: Any) -> Any:
    """JSON encoder for the Decimal and date values in the dataclasses."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unserialisable: {type(value).__name__}")


# ------------------------------------------------------------------------------ main


def _utf8(stream: TextIO) -> None:
    """Force UTF-8 on a console stream, where the stream supports it.

    Only `TextIOWrapper` carries `reconfigure`; a redirected or wrapped stream may not,
    and a missing method is not a reason to fail a load.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    # The payloads carry Arabic goods descriptions, and a Windows console defaults to
    # cp1252, which cannot encode them. Reconfiguring the stream beats stripping the
    # Arabic: a Bayan without its Arabic description is not the payload we are testing.
    _utf8(sys.stdout)

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--reexport-date",
        type=date.fromisoformat,
        default=date(2025, 6, 15),
        help="Re-export declaration date. Must sit inside one Gregorian year of the "
        "import line's duty-payment date (Art. 16 §3(a)).",
    )
    parser.add_argument("--curl", action="store_true", help="Emit cURL commands to run manually")
    parser.add_argument(
        "--record",
        action="store_true",
        help=f"Write the results template to {RESULTS_PATH.name} if absent",
    )
    args = parser.parse_args(argv)

    if args.record:
        if RESULTS_PATH.exists():
            print(f"{RESULTS_PATH} already exists — not overwriting recorded results")
            return 1
        RESULTS_PATH.write_text(
            json.dumps(results_template(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {RESULTS_PATH}")
        return 0

    if args.curl:
        print("# Fasah partial-consignment probe — run these in order.")
        print("# Requires: FASAH_SANDBOX_TOKEN, FASAH_CLIENT_ID, and a registered broker CR.")
        print("# Probe D is expected to FAIL; capture its body, that is the finding.")
        for probe in PROBES:
            depends = f" (after {', '.join(probe.depends_on)})" if probe.depends_on else ""
            print(f"\n# --- Probe {probe.ref}: {probe.title}{depends}")
            print(f"# Q: {probe.question}")
            if probe.notes:
                print(f"# {probe.notes}")
            print(build_curl(probe, base_url=args.base_url, reexport_date=args.reexport_date))
        print("\n# --- Follow-up: read back the declaration status (async validation)")
        print(build_status_curl(base_url=args.base_url))
        print(f"\n# Then: python {Path(__file__).name} --record  and fill in the results.")
        return 0

    document = {
        "base_url": args.base_url,
        "path": DECLARATION_PATH,
        "reexport_date": args.reexport_date,
        "transmits": False,
        "reason": (
            "Fasah sandbox access requires credentials issued to a registered customs "
            "broker. Payloads are constructed for manual submission; see --curl."
        ),
        "probes": [
            {**asdict(probe), "payload": build_payload(probe, reexport_date=args.reexport_date)}
            for probe in PROBES
        ],
    }
    json.dump(document, sys.stdout, indent=2, ensure_ascii=False, default=_serialise)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

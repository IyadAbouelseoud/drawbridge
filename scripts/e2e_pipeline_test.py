"""End-to-end exercise of the closed loop, both jurisdictions, against a running stack.

Not a pytest fixture. This drives the deployed API the way n8n drives it — one HTTP call
per workflow node, in the same order, with the same payload shapes — so that what it
proves is that the *deployment* works, not that the functions compose. The unit and golden
suites already establish the second, and they would go on passing if the router were never
wired into the app or a migration were never applied.

    uv run python scripts/e2e_pipeline_test.py
    uv run python scripts/e2e_pipeline_test.py --api http://localhost:8000 --keep

Two cases, chosen because they exercise opposite halves of the design:

**Case A — US unused-merchandise drawback, clean.** Confidence clears the floor, the
schedule corroborates the declared codes, CP-SAT reaches optimality. Triage finds nothing,
so the claim goes `quantified -> approved -> packaged` with no human in it and a CBP 7551
comes out the far end. This is the lane week 9 opened; before it, this claim would have sat
in an analyst's queue waiting to be waved through.

**Case B — GCC re-export, one line near the Article 16 §2 threshold.** Two re-exports
against one Bayan: one clears USD 5,000, one falls about 7% short. The short line is
rejected — "shall not be less than" has no soft edge — and the rejection raises a
`threshold_near_miss` review row rather than disappearing. The run suspends, the agent
drafts pre-analysis, an analyst resolves through the real `mcp-claims` tool, and the claim
finishes as a ZATCA refund payload.

Case B is expected to end **untransmittable**, and that is the point of running it. The
ZATCA packet carries an open Resolution 28624 citation (`COMPLIANCE-GCC.md` §8.4), so the
packager marks it as requiring analyst review before it may be sent. A run that reported
Case B as ready to file would mean that guard had been lost.

The script cleans up after itself unless `--keep` is passed. It writes to the same database
the stack uses, so `--keep` leaves real rows behind for inspection.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import httpx

DEFAULT_API = "http://localhost:8000"
TIMEOUT = 120.0

TENANT = UUID("00000000-0000-0000-0000-00000000e2e9")

CLAIMANT_US = {
    "name": "Northbridge Trading LLC",
    "identifier": "47-1928374-00",
    "address_line1": "1400 Harbor Parkway",
    "city": "Newark",
    "country": "US",
    "postal_code": "07114",
    "contact_email": "trade@example.invalid",
}

CLAIMANT_KSA = {
    "name": "Al-Marfa Logistics Company",
    "identifier": "4030291847",
    "address_line1": "King Abdulaziz Road",
    "city": "Jeddah",
    "country": "SA",
    "postal_code": "23334",
    "contact_email": "customs@example.invalid",
}


# The printed face of each source document, at the field count the real form carries.
# Values agree with the structured lines below; a document contradicting the feed it is
# supposed to evidence would be a fixture that quietly excuses a real inconsistency.
CBP_7501_FACE: tuple[str, list[tuple[str, str]]] = (
    "DEPARTMENT OF HOMELAND SECURITY - CBP FORM 7501 - ENTRY SUMMARY",
    [
        ("1. Filer Code/Entry Number", "ABC-1234567-8"),
        ("2. Entry Type", "01 Consumption"),
        ("3. Summary Date", "2023-03-20"),
        ("4. Surety Number", "0000"),
        ("5. Bond Type", "8 Continuous"),
        ("6. Port Code", "2704"),
        ("7. Entry Date", "2023-03-14"),
        ("8. Importing Carrier", "MAERSK KENSINGTON"),
        ("9. Mode of Transport", "11 Vessel, Non-Container"),
        ("10. Country of Origin", "CN"),
        ("11. Import Date", "2023-03-14"),
        ("12. B/L or AWB Number", "MAEU987654321"),
        ("13. Manufacturer ID", "CNSHAELE1234SHA"),
        ("14. Exporting Country", "CN"),
        ("15. Export Date", "2023-02-04"),
        ("16. I.T. Number", "V300-1122334"),
        ("17. I.T. Date", "2023-03-14"),
        ("18. Missing Documents", "None"),
        ("19. Foreign Port of Lading", "57035 Shanghai"),
        ("20. U.S. Port of Unlading", "2704"),
        ("21. Location of Goods", "E611 Newark Container Terminal"),
        ("22. Consignee Number", "47-1928374-00"),
        ("23. Importer Number", "47-1928374-00"),
        ("24. Reference Number", "PO-2023-00418"),
        ("26. Consignee Name", "Northbridge Trading LLC"),
        ("27. Importer of Record", "Northbridge Trading LLC"),
        ("28. Description of Merchandise", "Portable automatic data processing machines"),
        ("29. HTSUS Number", "8471300100"),
        ("30. Gross Weight", "8420 KG"),
        ("31. Net Quantity in HTSUS Units", "1000 NO"),
        ("32. Entered Value", "250000.00 USD"),
        ("33. HTSUS Rate", "Free"),
        ("34. Ad Valorem Duty", "0.00"),
        ("35. Section 301 Provision", "9903.88.15"),
        ("36. Section 301 Duty", "62500.00"),
        ("37. Merchandise Processing Fee", "864.00"),
        ("38. Harbor Maintenance Fee", "0.00"),
        ("39. Total Other Fees", "0.00"),
        ("40. Total Entered Value", "250000.00"),
        ("41. Total Duty", "62500.00"),
        ("42. Declarant Name", "Northbridge Customs Services"),
        ("43. Declaration Date", "2023-03-20"),
    ],
)

ZATCA_BAYAN_FACE: tuple[str, list[tuple[str, str]]] = (
    "ZAKAT, TAX AND CUSTOMS AUTHORITY - CUSTOMS DECLARATION (BAYAN)",
    [
        ("Declaration Number", "20240115447821"),
        ("Declaration Type", "Import for Home Use"),
        ("Declaration Date", "2024-01-15"),
        ("Customs Office", "Jeddah Islamic Port"),
        ("Office Code", "SAJED"),
        ("Registration Number", "4030291847"),
        ("Importer Name", "Al-Marfa Logistics Company"),
        ("Importer Address", "King Abdulaziz Road, Jeddah 23334"),
        ("Exporter Name", "Shenzhen Yuanhai Electronics Co Ltd"),
        ("Country of Origin", "CN"),
        ("Country of Export", "CN"),
        ("Port of Loading", "Shenzhen"),
        ("Mode of Transport", "Sea"),
        ("Vessel Name", "COSCO SHIPPING ARIES"),
        ("Bill of Lading", "COSU6301884720"),
        ("Container Numbers", "CSNU7734182, CSNU7734199"),
        ("Number of Packages", "96 Pallets"),
        ("Gross Weight", "10120 KG"),
        ("Net Weight", "9640 KG"),
        ("HS Code", "84713000"),
        ("Goods Description", "Portable data processing machines"),
        ("Quantity", "1200 PCE"),
        ("Invoice Number", "SYE-2023-11842"),
        ("Invoice Value", "234375.00 USD"),
        ("Freight", "18750.00 SAR"),
        ("Insurance", "4687.50 SAR"),
        ("Customs Value (CIF)", "937500.00 SAR"),
        ("Duty Rate", "5%"),
        ("Customs Duty", "46875.00 SAR"),
        ("VAT Rate", "15%"),
        ("Value Added Tax", "147656.25 SAR"),
        ("Total Payable", "194531.25 SAR"),
        ("Duty Payment Date", "2024-02-08"),
        ("Payment Reference", "SADAD-887412093"),
        ("Broker Licence", "CB-2019-4471"),
        ("Broker Name", "Rawabi Clearance Est."),
        ("Release Date", "2024-02-09"),
        ("Inspection Result", "Released without examination"),
    ],
)


# The export side needs its own document. Until week 10 both the import and the export
# lines cited the 7501 / *Bayan*, which was tolerable while provenance addressed a record;
# it stops being tolerable once a figure has to cite the box it was read from, because a
# re-export value is not printed on the import declaration and no box on that page holds
# it. Two documents per case is also what a real filing carries.
US_EXPORT_FACE: tuple[str, list[tuple[str, str]]] = (
    "PROOF OF EXPORT - OCEAN BILL OF LADING AND SHIPPER DECLARATION",
    [
        ("Bill of Lading Number", "MAEU123456789"),
        ("Booking Number", "BKG-2023-884211"),
        ("Shipper", "Northbridge Trading LLC"),
        ("Shipper EIN", "47-1928374-00"),
        ("Consignee", "Rheinwerk Distribution GmbH"),
        ("Notify Party", "Rheinwerk Distribution GmbH"),
        ("Vessel", "MAERSK KENSINGTON"),
        ("Voyage", "351E"),
        ("Port of Loading", "2704 Newark, NJ"),
        ("Port of Discharge", "DEHAM Hamburg"),
        ("Place of Delivery", "Koeln, Germany"),
        ("Date of Export", "2024-01-09"),
        ("Onboard Date", "2024-01-09"),
        ("Container Number", "MSKU4471820"),
        ("Seal Number", "SL8842137"),
        ("Marks and Numbers", "NB/PO-2023-00418"),
        ("Number of Packages", "40 Pallets"),
        ("Description of Goods", "Portable ADP machines, re-exported unused"),
        ("Schedule B Number", "8471300150"),
        ("Quantity Exported", "400 NO"),
        ("Gross Weight", "3368 KG"),
        ("Declared Value", "100000.00 USD"),
        ("Freight Terms", "Prepaid"),
        ("Merchandise Condition", "Unused and unaltered"),
        ("AES ITN", "X20240109123456"),
    ],
)

KSA_REEXPORT_FACE: tuple[str, list[tuple[str, str]]] = (
    "ZAKAT, TAX AND CUSTOMS AUTHORITY - RE-EXPORT DECLARATION",
    [
        ("Re-Export Declaration Number", "RE-20240912-0031"),
        ("Second Declaration Number", "RE-20240915-0032"),
        ("Linked Import Declaration", "20240115447821"),
        ("Declaration Type", "Re-Export of Foreign Goods"),
        ("Customs Office", "Jeddah Islamic Port"),
        ("Exporter Name", "Al-Marfa Logistics Company"),
        ("Registration Number", "4030291847"),
        ("Consignee", "Gulf Systems Trading FZE"),
        ("Destination Country", "AE"),
        ("Mode of Transport", "Road"),
        ("HS Code", "84713000"),
        ("Goods Description", "Portable data processing machines"),
        ("Consignment A Reference", "CNS-2024-0115-A"),
        ("Consignment A Date", "2024-07-12"),
        ("Consignment A Quantity", "500 PCE"),
        ("Consignment A Value", "390625.00 SAR"),
        ("Consignment B Reference", "CNS-2024-0115-B"),
        ("Consignment B Date", "2024-07-15"),
        ("Consignment B Quantity", "22 PCE"),
        ("Consignment B Value", "17500.00 SAR"),
        ("Total Re-Exported Value", "408125.00 SAR"),
        ("Condition of Goods", "Unused and unaltered"),
        ("Inspection Result", "Released"),
        ("Release Date", "2024-07-16"),
    ],
)


# --------------------------------------------------------------------------- reporting


@dataclass
class Report:
    """What happened, in the order it happened.

    Accumulated rather than printed as it goes so a failure in step nine still shows the
    eight steps that worked — which is usually where the cause is.
    """

    name: str
    steps: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def ok(self, step: str, detail: str = "") -> None:
        self.steps.append((step, detail))

    def check(self, step: str, condition: bool, detail: str) -> bool:
        self.steps.append((step, detail))
        if not condition:
            self.failures.append(f"{step}: {detail}")
        return condition

    def fail(self, step: str, detail: str) -> None:
        self.steps.append((step, detail))
        self.failures.append(f"{step}: {detail}")

    def render(self) -> str:
        lines = [f"\n{'=' * 78}", f"{self.name}", "=" * 78]
        for step, detail in self.steps:
            mark = "  " if not any(step in f for f in self.failures) else "!!"
            lines.append(f" {mark} {step:<34} {detail}")
        lines.append(f" -> {'FAILED: ' + '; '.join(self.failures) if self.failures else 'PASSED'}")
        return "\n".join(lines)


class Api:
    """Thin client. Never raises on a non-2xx; the caller decides what a status means."""

    def __init__(self, base: str) -> None:
        self._client = httpx.Client(base_url=base.rstrip("/"), timeout=TIMEOUT)

    def post(self, path: str, body: Any = None, **params: Any) -> tuple[int, Any]:
        response = self._client.post(path, json=body, params=params or None)
        return response.status_code, _json_or_text(response)

    def get(self, path: str, **params: Any) -> tuple[int, Any]:
        response = self._client.get(path, params=params or None)
        return response.status_code, _json_or_text(response)

    def close(self) -> None:
        self._client.close()


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


# ------------------------------------------------------------------------- the payload


def _pdf(title: str, rows: list[tuple[str, str]]) -> bytes:
    """A one-page native-text PDF carrying a realistic field count.

    Real bytes rather than a stub, because `/extraction/run` decides between the native
    and OCR paths by measuring the text layer, and then scores its confidence from the
    character density. A five-line document scores around 0.87 — under the 0.95 floor —
    and routes the run to review, which is the gate working correctly on a page that has
    almost nothing on it.

    So the fixture is not thinned down. A CBP 7501 and a ZATCA *Bayan* both carry forty-odd
    populated fields, and a test document with five of them would be measuring the gate
    against a page no importer ever files.
    """
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), title, fontsize=13)
        for index, (label, value) in enumerate(rows):
            column, row = divmod(index, 22)
            page.insert_text(
                (72 + column * 250, 110 + row * 17),
                f"{label}: {value}",
                fontsize=8,
            )
        return bytes(document.tobytes())


# Bytes of every document this run rendered, so a figure's box can be measured off the
# same file that was uploaded rather than computed from the coordinates `_pdf` used. The
# two agree today; measuring keeps them agreeing when the layout changes.
_RENDERED: dict[str, bytes] = {}
_ROWS: dict[str, dict[str, str]] = {}


def _document(kind: str, filename: str, title: str, rows: list[tuple[str, str]]) -> dict[str, Any]:
    data = _pdf(title, rows)
    _RENDERED[filename] = data
    _ROWS[filename] = dict(rows)
    return {
        "kind": kind,
        "filename": filename,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "page_count": 1,
    }


def _boxes(
    filename: str, ref: dict[str, Any], mapping: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Measure the box each named figure occupies on the rendered document.

    `search_for` rather than arithmetic over the layout constants: the coordinates that go
    into a provenance record have to be where the text *is*, and a box derived from where
    we intended to put it is an assertion about our own code rather than about the file an
    auditor would be handed.

    Raises when a label is not on the page. A missing box must stop the run — falling back
    to an approximate rectangle would produce exactly the fabricated provenance the whole
    mechanism exists to prevent.
    """
    import pymupdf

    boxes: dict[str, dict[str, Any]] = {}
    with pymupdf.open(stream=_RENDERED[filename], filetype="pdf") as document:
        page = document[0]
        for figure, label in mapping.items():
            value = _ROWS[filename][label]
            rects = page.search_for(f"{label}: {value}")
            if not rects:
                msg = f"{filename}: no box for {figure!r} - label {label!r} is not on the page"
                raise RuntimeError(msg)
            rect = rects[0]
            boxes[figure] = {
                "document_id": ref["document_id"],
                "document_sha256": ref["sha256"],
                "page": 1,
                "x0": round(rect.x0, 2),
                "y0": round(rect.y0, 2),
                "x1": round(rect.x1, 2),
                "y1": round(rect.y1, 2),
                "field_path": figure,
                "raw_text": value,
                "extractor": "native-labelled",
            }
    return boxes


def _ref(stored: dict[str, Any]) -> dict[str, Any]:
    """A `DocumentRef` from what `/documents/batch` returned.

    The endpoint answers in the shape a workflow wants — flat, with the object key — and
    the schema wants a nested reference. Converting here rather than widening the response
    keeps the API's contract about what it stored, not about what a consumer will do next.
    """
    return {
        "document_id": stored["document_id"],
        "kind": stored["kind"],
        "sha256": stored["sha256"],
        "object_key": stored["object_key"],
        "page_count": 1,
        "language": stored["language"],
    }


def _provenance(
    document_ref: dict[str, Any], field_path: str, figures: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Provenance whose every figure names the box it was read from.

    Week 9 sent `field_path` alone here and said so: the values arrived from a structured
    feed, and claiming a page rectangle they were never read from would have been a
    fabricated record. Week 10 removes the excuse rather than the honesty — the figures
    are located on the document that evidences them, and `_boxes` refuses to invent one
    for a label that is not there.
    """
    return {
        "spans": [{"document": document_ref, "page": 1, "field_path": field_path}],
        "confidence": {"score": 0.99, "method": "pdfplumber-native"},
        "figures": figures,
    }


US_IMPORT_FIGURES = {
    "quantity": "31. Net Quantity in HTSUS Units",
    "entered_value": "32. Entered Value",
    "section_301_duty": "36. Section 301 Duty",
    "mpf_paid": "37. Merchandise Processing Fee",
}
US_EXPORT_FIGURES = {
    "quantity": "Quantity Exported",
    "declared_value": "Declared Value",
}
KSA_IMPORT_FIGURES = {
    "quantity": "Quantity",
    "entered_value": "Customs Value (CIF)",
    "duty_paid": "Customs Duty",
    "vat_paid": "Value Added Tax",
}


def _us_payload(import_ref: dict[str, Any], export_ref: dict[str, Any]) -> dict[str, Any]:
    document_ref = import_ref
    import_boxes = _boxes("entry-summary.pdf", import_ref, US_IMPORT_FIGURES)
    export_boxes = _boxes("proof-of-export.pdf", export_ref, US_EXPORT_FIGURES)
    import_id, export_id = str(uuid4()), str(uuid4())
    return {
        "tenant_id": str(TENANT),
        "jurisdiction": "us",
        "as_of": "2024-06-01",
        "claimant": CLAIMANT_US,
        "imports": [
            {
                "line_id": import_id,
                "tenant_id": str(TENANT),
                "jurisdiction": "us",
                "currency": "USD",
                "declaration_number": "ABC-1234567-8",
                "line_number": 1,
                "import_date": "2023-03-14",
                "declaration_date": "2023-03-20",
                "port_of_entry": "2704",
                "country_of_origin": "CN",
                "hts": {"code": "8471300100"},
                "description": "Portable automatic data processing machines",
                "quantity": "1000",
                "unit_of_measure": "NO",
                "entered_value": "250000.00",
                "duty_paid": "0.00",
                "section_301_duty": "62500.00",
                "mpf_paid": "864.00",
                "provenance": _provenance(document_ref, "lines[0]", import_boxes),
            }
        ],
        "exports": [
            {
                "line_id": export_id,
                "tenant_id": str(TENANT),
                "jurisdiction": "us",
                "reference": "MAEU123456789",
                "line_number": 1,
                "export_date": "2024-01-09",
                "destination_country": "DE",
                "hts": {"code": "8471300150"},
                "description": "Portable ADP machines, re-exported unused",
                "quantity": "400",
                "unit_of_measure": "NO",
                "declared_value": "100000.00",
                "provenance": _provenance(export_ref, "lines[0]", export_boxes),
            }
        ],
    }


def _ksa_payload(import_ref: dict[str, Any], export_ref: dict[str, Any]) -> dict[str, Any]:
    """Two re-exports against one Bayan: one clears Art. 16 §2, one falls short by ~7%.

    SAR 17,500 converts to USD 4,666.67 at the peg — inside the 10% band that makes a
    rejection a *near miss* rather than a plain failure, which is what puts a row on the
    review queue instead of dropping the line silently.
    """
    document_ref = import_ref
    import_boxes = _boxes("bayan.pdf", import_ref, KSA_IMPORT_FIGURES)
    consignment_a = _boxes(
        "reexport.pdf",
        export_ref,
        {"quantity": "Consignment A Quantity", "declared_value": "Consignment A Value"},
    )
    consignment_b = _boxes(
        "reexport.pdf",
        export_ref,
        {"quantity": "Consignment B Quantity", "declared_value": "Consignment B Value"},
    )
    import_id = str(uuid4())
    return {
        "tenant_id": str(TENANT),
        "jurisdiction": "ksa",
        "as_of": "2024-10-01",
        "claimant": CLAIMANT_KSA,
        "imports": [
            {
                "line_id": import_id,
                "tenant_id": str(TENANT),
                "jurisdiction": "ksa",
                "currency": "SAR",
                "declaration_number": "20240115447821",
                "line_number": 1,
                "import_date": "2024-01-15",
                "declaration_date": "2024-01-15",
                "duty_payment_date": "2024-02-08",
                "port_of_entry": "Jeddah Islamic Port",
                "country_of_origin": "CN",
                "hts": {"code": "84713000"},
                "description": "Portable data processing machines",
                "quantity": "1200",
                "unit_of_measure": "PCE",
                "entered_value": "937500.00",
                "duty_paid": "46875.00",
                "vat_paid": "147656.25",
                "provenance": _provenance(document_ref, "lines[0]", import_boxes),
            }
        ],
        "exports": [
            {
                "line_id": str(uuid4()),
                "tenant_id": str(TENANT),
                "jurisdiction": "ksa",
                "reference": "RE-20240912-0031",
                "line_number": 1,
                "export_date": "2024-07-12",
                "destination_country": "AE",
                "hts": {"code": "84713000"},
                "description": "Portable data processing machines, re-exported unused",
                "quantity": "500",
                "unit_of_measure": "PCE",
                "declared_value": "390625.00",
                "linked_import_declaration": "20240115447821",
                "consignment_id": "CNS-2024-0115-A",
                "unused_and_unaltered": True,
                "provenance": _provenance(export_ref, "lines[0]", consignment_a),
            },
            {
                "line_id": str(uuid4()),
                "tenant_id": str(TENANT),
                "jurisdiction": "ksa",
                "reference": "RE-20240915-0032",
                "line_number": 2,
                "export_date": "2024-07-15",
                "destination_country": "AE",
                "hts": {"code": "84713000"},
                "description": "Portable data processing machines, re-exported unused",
                "quantity": "22",
                "unit_of_measure": "PCE",
                "declared_value": "17500.00",
                "linked_import_declaration": "20240115447821",
                "consignment_id": "CNS-2024-0115-B",
                "unused_and_unaltered": True,
                "provenance": _provenance(export_ref, "lines[1]", consignment_b),
            },
        ],
    }


# ----------------------------------------------------------------------------- the runs


def _ensure_tenant() -> None:
    """Create the test tenant directly.

    There is no tenant endpoint: onboarding is an operator action, not a pipeline one, and
    an API that could mint tenants from a webhook payload would be a multi-tenancy hole
    rather than a convenience.
    """
    from sqlalchemy.dialects.postgresql import insert

    from services.api.src.models import Tenant
    from services.api.src.sync_db import sync_session

    with sync_session() as session:
        session.execute(
            insert(Tenant)
            .values(tenant_id=TENANT, name="Drawbridge E2E", default_jurisdiction="us")
            .on_conflict_do_nothing(index_elements=[Tenant.tenant_id])
        )


def _cleanup(claim_ids: list[str]) -> None:
    from sqlalchemy import delete

    from services.api.src.models import (
        Claim,
        ClaimTransition,
        Document,
        EntryLine,
        ExportLine,
        RefundLine,
        ReviewQueue,
    )
    from services.api.src.sync_db import sync_session

    ids = [UUID(c) for c in claim_ids]
    with sync_session() as session:
        session.execute(delete(ReviewQueue).where(ReviewQueue.tenant_id == TENANT))
        session.execute(delete(RefundLine).where(RefundLine.claim_id.in_(ids)))
        session.execute(delete(ClaimTransition).where(ClaimTransition.claim_id.in_(ids)))
        session.execute(delete(Claim).where(Claim.tenant_id == TENANT))
        session.execute(delete(EntryLine).where(EntryLine.tenant_id == TENANT))
        session.execute(delete(ExportLine).where(ExportLine.tenant_id == TENANT))
        session.execute(delete(Document).where(Document.tenant_id == TENANT))


def _store(
    api: Api, report: Report, documents: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Put the documents in MinIO and get back the refs the lines will cite.

    Storage runs before the claim payload is built, not alongside it, because the document
    id is content-derived and only the store knows it. Building the lines first would mean
    citing a document id invented locally — a provenance record pointing at nothing, which
    is worse than no provenance because it survives inspection.
    """
    status, stored = api.post(
        "/documents/batch", {"tenant_id": str(TENANT), "documents": documents}
    )
    if status != 201:
        report.fail("store documents", f"HTTP {status} {stored}")
        return None
    refs: list[dict[str, Any]] = stored["stored"]
    report.ok("store documents", f"{len(refs)} object(s) in MinIO")
    return refs


def _intake(
    api: Api, report: Report, payload: dict[str, Any], stored: list[dict[str, Any]]
) -> tuple[Any, Any] | None:
    """Extract, classify, match, triage — the stages between intake and the branch."""
    status, extraction = api.post(
        "/extraction/run",
        {
            "tenant_id": payload["tenant_id"],
            "documents": [d["document_id"] for d in stored],
        },
    )
    if status != 200:
        report.fail("extract", f"HTTP {status} {extraction}")
        return None
    report.check(
        "extract",
        extraction["confidence_ok"],
        f"{len(extraction['documents'])} doc(s), below floor: "
        f"{extraction['confidence_below_floor']}",
    )

    status, classified = api.post(
        "/classification/run",
        {
            "jurisdiction": payload["jurisdiction"],
            "lines": [
                {
                    "line_id": line["line_id"],
                    "description": line["description"],
                    "declared_code": line["hts"]["code"],
                }
                for line in payload["imports"]
            ],
        },
    )
    if status != 200:
        report.fail("classify", f"HTTP {status} {classified}")
        return None
    report.ok(
        "classify",
        f"method={classified['lines'][0]['method']} "
        f"corroborated={classified['corroborated']}/{len(classified['lines'])} "
        f"unsupported={len(classified['unsupported'])}",
    )

    status, match = api.post(
        "/matching/run",
        {
            "jurisdiction": payload["jurisdiction"],
            "imports": payload["imports"],
            "exports": payload["exports"],
            "as_of": payload["as_of"],
        },
    )
    if status != 200:
        report.fail("match", f"HTTP {status} {match}")
        return None
    report.check(
        "match",
        bool(match["matches"]),
        f"status={match['status']} matches={len(match['matches'])} "
        f"rejections={len(match['rejections'])} refund={match['total_refund']} "
        f"{match['currency']}",
    )

    status, triage = api.post(
        "/triage/evaluate",
        {
            "match_result": match,
            "confidences": extraction["confidences"],
            "as_of": payload["as_of"],
        },
    )
    if status != 200:
        report.fail("triage", f"HTTP {status} {triage}")
        return None
    reasons = sorted({item["reason"] for item in triage["items"]})
    report.ok(
        "triage",
        f"requires_review={triage['requires_review']} "
        f"severity={triage['highest_severity']} reasons={reasons or '-'}",
    )
    return match, triage


def _persist(
    api: Api, report: Report, payload: dict[str, Any], match: Any, triage: Any
) -> str | None:
    status, claim = api.post(
        "/claims/persist",
        {
            "tenant_id": payload["tenant_id"],
            "jurisdiction": payload["jurisdiction"],
            "imports": payload["imports"],
            "exports": payload["exports"],
            "matches": match["matches"],
            "total_refund": match["total_refund"],
            "requires_review": triage["requires_review"],
        },
    )
    if status != 201:
        report.fail("persist claim", f"HTTP {status} {claim}")
        return None
    report.ok(
        "persist claim",
        f"{claim['claim_id'][:8]} state={claim['state']} "
        f"deadline={claim['filing_deadline']} refund={claim['total_refund']}",
    )
    return str(claim["claim_id"])


def _package(
    api: Api, report: Report, claim_id: str, claimant: dict[str, Any], expect: str
) -> None:
    status, packet = api.post(
        "/packaging/build",
        {"claim_id": claim_id, "claimant": claimant, "include_artifacts": True},
    )
    if status != 200:
        report.fail("build packet", f"HTTP {status} {packet}")
        return
    filenames = [a["filename"] for a in packet["artifacts"]]
    report.check(
        "build packet",
        any(expect in name for name in filenames),
        f"{filenames} transmittable={packet['transmittable']} "
        f"open_citations={len(packet['open_citations'])}",
    )

    status, moved = api.post(
        "/claims/transition",
        {
            "claim_id": claim_id,
            # 'pipeline', matching the workflow's Mark Packaged node. The script stands
            # in for n8n and should leave the same audit trail n8n would; an 'e2e' actor
            # in the history would make the trail describe the test rather than the run.
            "to_state": "packaged",
            "actor": "pipeline",
            "reason": "packet rendered by the end-to-end script",
        },
    )
    report.check("mark packaged", status == 200, f"HTTP {status} -> {moved}")


def case_a(api: Api) -> tuple[Report, str | None]:
    report = Report("Case A — US unused-merchandise drawback, no manual touch")
    stored = _store(
        api,
        report,
        [
            _document("cbp_7501", "entry-summary.pdf", *CBP_7501_FACE),
            _document("proof_of_export", "proof-of-export.pdf", *US_EXPORT_FACE),
        ],
    )
    if stored is None:
        return report, None

    payload = _us_payload(_ref(stored[0]), _ref(stored[1]))
    intake = _intake(api, report, payload, stored)
    if intake is None:
        return report, None
    match, triage = intake

    if not report.check(
        "no analyst needed",
        triage["requires_review"] is False,
        "triage raised nothing, so the automated lane applies",
    ):
        return report, None

    claim_id = _persist(api, report, payload, match, triage)
    if claim_id is None:
        return report, None

    status, moved = api.post(
        "/claims/transition",
        {
            "claim_id": claim_id,
            "to_state": "approved",
            "actor": "pipeline",
            "reason": "clean claim, triage raised nothing",
        },
    )
    if not report.check("auto-approve", status == 200, f"HTTP {status} -> {moved}"):
        return report, claim_id

    _package(api, report, claim_id, CLAIMANT_US, expect="7551")

    status, history = api.get(f"/claims/{claim_id}/history")
    actors = [t["actor"] for t in history["transitions"]] if status == 200 else []
    report.check(
        "no human in the trail",
        all(actor == "pipeline" for actor in actors),
        f"actors={actors}",
    )

    _trace_through_mcp(claim_id, "section_301_duty", report)
    return report, claim_id


def case_b(api: Api) -> tuple[Report, str | None]:
    report = Report("Case B — GCC re-export, one line near the Art. 16 §2 threshold")
    stored = _store(
        api,
        report,
        [
            _document("zatca_bayan", "bayan.pdf", *ZATCA_BAYAN_FACE),
            _document("zatca_reexport_declaration", "reexport.pdf", *KSA_REEXPORT_FACE),
        ],
    )
    if stored is None:
        return report, None

    payload = _ksa_payload(_ref(stored[0]), _ref(stored[1]))
    intake = _intake(api, report, payload, stored)
    if intake is None:
        return report, None
    match, triage = intake

    reasons = {item["reason"] for item in triage["items"]}
    if not report.check(
        "near miss raised a review",
        "threshold_near_miss" in reasons,
        f"reasons={sorted(reasons)}",
    ):
        return report, None

    claim_id = _persist(api, report, payload, match, triage)
    if claim_id is None:
        return report, None

    status, suspended = api.post(
        "/review/suspend",
        {
            "tenant_id": payload["tenant_id"],
            "claim_id": claim_id,
            "workflow_run_id": "e2e-run",
            "items": triage["items"],
        },
    )
    if not report.check("suspend for analyst", status == 201, f"HTTP {status}"):
        return report, claim_id
    review_ids = [item["review_id"] for item in suspended["items"]]

    # The agent is optional infrastructure: with no ANTHROPIC_API_KEY it reports
    # `unavailable` and the row keeps a NULL memo, which is exactly the state an analyst
    # worked in before the service existed. That is a reportable outcome, not a failure.
    status, drafted = api.post("/review/draft", None, tenant_id=payload["tenant_id"])
    report.ok(
        "draft analyst memos",
        f"HTTP {status} {drafted}" if status == 200 else f"HTTP {status} (agent unreachable)",
    )

    resolved = _resolve_through_mcp(review_ids, report)
    if not resolved:
        return report, claim_id

    status, moved = api.post(
        "/claims/transition",
        {
            "claim_id": claim_id,
            "to_state": "approved",
            "actor": "analyst:e2e",
            "reason": "near miss reviewed and the remaining line confirmed",
        },
    )
    if not report.check("approve after review", status == 200, f"HTTP {status} -> {moved}"):
        return report, claim_id

    _package(api, report, claim_id, CLAIMANT_KSA, expect="zatca")

    status, packet = api.post(
        "/packaging/build",
        {"claim_id": claim_id, "claimant": CLAIMANT_KSA, "include_artifacts": False},
    )
    report.check(
        "28624 still blocks transmission",
        status == 200 and packet["transmittable"] is False,
        "the ZATCA packet carries an open procedural citation and may not be sent "
        "(COMPLIANCE-GCC.md §8.4.1)",
    )

    # The GCC side of the trace. The figure an Art. 16 §2 decision turns on is the
    # re-export value, and it is the one an analyst overrode a threshold near-miss
    # against — so it is the one the ledger has to be able to point at.
    _trace_through_mcp(claim_id, "declared_value", report)
    return report, claim_id


def _trace_through_mcp(claim_id: str, field: str, report: Report) -> None:
    """Ask `mcp-ledger` where one figure on the finished claim came from.

    The last step of the loop and the one an auditor actually performs. Everything before
    it produces a refund figure; this asks the system to point at the rectangle on the
    document the figure was read from, and to do it from the append-only ledger rather
    than from the working row.

    In-process for the same reason as `_resolve_through_mcp`: the transport is FastMCP's
    and has nothing to do with whether the trace is correct.
    """
    from mcp_servers.mcp_ledger import server as ledger

    result = ledger.trace_figure(claim_id=claim_id, field=field)
    if not result.get("ok") or not result.get("found"):
        report.fail(f"trace {field}", str(result)[:200])
        return

    hit = result["ledger"][0]
    box = ", ".join(f"{value:.1f}" for value in hit["bbox"])
    report.check(
        f"trace {field}",
        result["consistent"] is True,
        f"{hit['document_sha256'][:12]}... p{hit['page']} [{box}] "
        f"raw={hit['raw_text']!r} ledger==live:{result['consistent']}",
    )

    chain = ledger.ledger_chain(tenant_id=str(TENANT))
    report.check(
        "ledger chain intact",
        chain.get("ok") is True and chain.get("broken_at") is None,
        f"{chain.get('entries')} entries, {chain.get('detail')}",
    )


def _resolve_through_mcp(review_ids: list[str], report: Report) -> bool:
    """Resolve every open exception through the real `mcp-claims` tool.

    In-process rather than over the MCP transport: what is being exercised is the analyst
    path — the reasoning requirement, the resume notification, the refusal to approve a
    claim with anything still open — and none of that lives in the transport. An HTTP MCP
    round trip here would test FastMCP's session handling and nothing about Drawbridge.
    """
    from mcp_servers.mcp_claims import server as claims

    for review_id in review_ids:
        result = claims.resolve_review_exception(
            review_id=review_id,
            resolution="approved",
            reasoning=(
                "Confirmed the declared value on the Bayan against the commercial "
                "invoice; the short line is correctly excluded and the remaining "
                "re-export clears the Article 16 minimum."
            ),
            analyst="e2e-operator",
        )
        if not result.get("ok"):
            report.fail("resolve via mcp-claims", str(result))
            return False
    report.ok("resolve via mcp-claims", f"{len(review_ids)} exception(s) approved")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base URL (default {DEFAULT_API})")
    parser.add_argument("--keep", action="store_true", help="leave the created rows in place")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()

    api = Api(args.api)
    status, ready = api.get("/ready")
    if status != 200:
        print(f"API at {args.api} is not ready: HTTP {status} {ready}", file=sys.stderr)
        return 2

    _ensure_tenant()

    reports: list[Report] = []
    claim_ids: list[str] = []
    try:
        for runner in (case_a, case_b):
            report, claim_id = runner(api)
            reports.append(report)
            if claim_id:
                claim_ids.append(claim_id)
    finally:
        api.close()
        if not args.keep and claim_ids:
            _cleanup(claim_ids)

    if args.json:
        print(
            json.dumps(
                [{"case": r.name, "passed": not r.failures, "steps": r.steps} for r in reports],
                indent=2,
            )
        )
    else:
        for report in reports:
            print(report.render())
        print(f"\n{'=' * 78}")
        print(f"ran {date.today()} against {args.api}; kept={args.keep}")

    return 0 if all(not r.failures for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())

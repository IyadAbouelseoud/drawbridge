"""CBP Forms 7551 and 7552.

- **7551 — Drawback Entry.** The claim itself: who is claiming, under which §1313
  provision, against which import entries, and for how much.
- **7552 — Delivery Certificate for Purposes of Drawback.** The transfer chain. Required
  where the claimant did not import the merchandise, or where a manufacturer certifies
  what was made and delivered. Emitted only when a `manufacturer` is present or a
  manufacturing theory is claimed — a 7552 attached to a straight §1313(j) claim by the
  importer of record is a document CBP did not ask for.

Field names follow the CBP field ordering as `cbp7551_*` / `cbp7552_*`. They are the
contract with any downstream filer software, so they are stable regardless of layout.

Drawbridge does not file. These are handed to a licensed filer, and every page says so.
"""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader

from drawbridge_schemas.jurisdiction import Jurisdiction
from services.packager.src import citations as cite
from services.packager.src.packet import (
    FilingPacket,
    PacketArtifact,
    PacketRequest,
    money,
    quantity,
)
from services.packager.src.pdf import Document, Field, Section

PDF_MEDIA_TYPE = "application/pdf"

PREPARER_NOTICE = (
    "Prepared by Drawbridge for filing by a licensed customs broker. Drawbridge is not a "
    "customs broker and does not transmit to CBP. Every figure below traces to a "
    "source-document span retained under 19 CFR 163; the supporting schedule accompanies "
    "this form."
)

CERTIFICATION_7551 = (
    "I declare that the merchandise described was imported and duty paid as stated, that "
    "the designated quantity has not been and will not be the subject of any other "
    "drawback claim, and that the exported or destroyed articles are as described. The "
    "records supporting this claim are retained and available for verification."
)

CERTIFICATION_7552 = (
    "I certify that the merchandise or articles described were delivered to the party "
    "named, that the quantities stated are correct, and that no other certificate has "
    "been issued covering the same merchandise or articles."
)

# §1313 subsections, as they print on the 7551 provision line.
PROVISIONS = {
    "direct_identity": "19 U.S.C. 1313(j)(1) - unused merchandise, direct identification",
    "hts_substitution": "19 U.S.C. 1313(j)(2) - unused merchandise, substitution",
    "manufacturing_direct_identity": "19 U.S.C. 1313(a) - manufacturing, direct identification",
    "manufacturing_substitution": "19 U.S.C. 1313(b) - manufacturing, substitution",
}

_MANUFACTURING = frozenset({"manufacturing_direct_identity", "manufacturing_substitution"})


def _provision_for(request: PacketRequest) -> str:
    """The §1313 subsection printed on the form.

    An explicit `drawback_provision` wins. Otherwise it is derived from the theories on
    the lines, and a claim mixing theories prints them all rather than picking one — a
    7551 asserting a single provision over mixed lines misdescribes half of them.
    """
    if request.drawback_provision:
        return request.drawback_provision
    theories = sorted({line.theory for line in request.lines})
    named = [PROVISIONS.get(theory, theory) for theory in theories]
    return "; ".join(named) if named else "not determined"


def _citations_for(request: PacketRequest) -> list[cite.Citation]:
    """The authorities a US packet rests on.

    All verified. The US side has no analogue of the ZATCA 28624 problem: the statute and
    the CFR are published and citable, so nothing here is ever an open citation.
    """
    theories = {line.theory for line in request.lines}
    found = [cite.US_1313R]
    if theories & {"direct_identity", "hts_substitution"}:
        found.insert(0, cite.US_1313J)
    if "manufacturing_direct_identity" in theories:
        found.insert(0, cite.US_1313A)
    if "manufacturing_substitution" in theories:
        found.insert(0, cite.US_1313B)
    if any(line.bom_path for line in request.lines):
        found.append(cite.US_190_SUBPART_B)
    return found


def _claimant_section(request: PacketRequest, prefix: str) -> Section:
    claimant = request.claimant
    return Section(
        title="Claimant",
        rows=[
            [
                Field(f"{prefix}_claimant_name", "Name", claimant.name, width=0.62),
                Field(f"{prefix}_claimant_id", "Identifier (IRS/EIN)", claimant.identifier, 0.38),
            ],
            [
                Field(f"{prefix}_claimant_address", "Address", claimant.address_block(), 0.62),
                Field(f"{prefix}_claimant_country", "Country", claimant.country, 0.18),
                Field(f"{prefix}_broker_id", "Filer / broker", claimant.broker_identifier, 0.20),
            ],
        ],
    )


def render_7551(request: PacketRequest) -> bytes:
    """CBP Form 7551 — Drawback Entry."""
    document = Document(
        "CBP Form 7551 - Drawback Entry",
        f"Claim {request.claim_id} | prepared {request.prepared_on.isoformat()} | "
        f"transcription for filing, not a CBP-issued form",
    )
    document.paragraph(PREPARER_NOTICE)

    document.section(_claimant_section(request, "cbp7551"))

    document.section(
        Section(
            title="Claim",
            rows=[
                [
                    Field("cbp7551_provision", "Drawback provision", _provision_for(request), 0.58),
                    Field("cbp7551_port_code", "Port code", request.port_code, 0.20),
                    Field(
                        "cbp7551_filing_deadline",
                        "Filing deadline",
                        request.filing_deadline.isoformat(),
                        0.22,
                    ),
                ],
                [
                    Field(
                        "cbp7551_period_start",
                        "Period from",
                        request.period_start.isoformat(),
                        0.25,
                    ),
                    Field("cbp7551_period_end", "Period to", request.period_end.isoformat(), 0.25),
                    Field("cbp7551_line_count", "Designations", str(len(request.lines)), 0.20),
                    Field("cbp7551_currency", "Currency", str(request.currency).upper(), 0.30),
                ],
            ],
        )
    )

    document.section(
        Section(
            title="Amounts",
            rows=[
                [
                    Field(
                        "cbp7551_total_duty",
                        "Duty designated",
                        money(request.total_duty_allocated),
                        0.34,
                    ),
                    Field(
                        "cbp7551_refund_rate",
                        "Refund rate",
                        "99% (19 U.S.C. 1313(l))",
                        0.32,
                    ),
                    Field(
                        "cbp7551_total_claimed",
                        "Total drawback claimed",
                        money(request.total_refund),
                        0.34,
                    ),
                ]
            ],
            note=(
                "The 1% retained by CBP is the statutory difference between duty "
                "designated and drawback claimed; it is not a fee and is not recoverable."
            ),
        )
    )

    document.section(
        Section(
            title="Certification",
            rows=[
                [
                    Field("cbp7551_signatory_name", "Signature of claimant", "", 0.45),
                    Field("cbp7551_signatory_title", "Title", "", 0.30),
                    Field("cbp7551_signature_date", "Date", "", 0.25),
                ]
            ],
            note=CERTIFICATION_7551,
        )
    )

    _designation_schedule(document, request)
    return document.render()


def _designation_schedule(document: Document, request: PacketRequest) -> None:
    """The line-item schedule backing the totals.

    Every designation, with both sides of the pairing on one row. A 7551 whose total has
    no itemisation behind it is a number CBP has to take on trust, and a claim taken on
    trust is a claim selected for review.
    """
    document.section(
        Section(
            title="Schedule A - import entries designated",
            rows=[],
        )
    )
    document.table(
        headers=["Entry no.", "Ln", "Import date", "HTS", "Description", "Qty", "Duty desig."],
        widths=[0.19, 0.04, 0.11, 0.12, 0.26, 0.14, 0.14],
        rows=[
            [
                line.import_declaration,
                str(line.import_line_number),
                line.import_date.isoformat(),
                line.import_hts,
                line.description,
                f"{quantity(line.quantity_designated)} {line.unit_of_measure}",
                money(line.duty_allocated),
            ]
            for line in request.lines
        ],
    )

    document.section(Section(title="Schedule B - exports or destructions claimed", rows=[]))
    document.table(
        headers=["Export ref.", "Ln", "Export date", "HTS", "Dest.", "Theory", "Drawback"],
        widths=[0.20, 0.04, 0.11, 0.12, 0.07, 0.28, 0.18],
        rows=[
            [
                line.export_reference,
                str(line.export_line_number),
                line.export_date.isoformat(),
                line.export_hts,
                line.destination_country or "DESTROYED",
                PROVISIONS.get(line.theory, line.theory),
                money(line.refund_amount),
            ]
            for line in request.lines
        ],
    )

    manufacturing = [line for line in request.lines if line.bom_path]
    if manufacturing:
        document.section(
            Section(
                title="Schedule C - bill of materials routes",
                rows=[],
                note=(
                    "Each route is read leaf-last: the imported component, then every "
                    "in-house stage above it. Consumption compounds each stage's yield, "
                    "which is why the designated quantity exceeds the quantity embodied "
                    "in the finished article."
                ),
            )
        )
        document.table(
            headers=[
                "Export ref.",
                "Component HTS",
                "Route (finished good to component)",
                "Qty used",
            ],
            widths=[0.20, 0.16, 0.46, 0.18],
            rows=[
                [
                    line.export_reference,
                    line.import_hts,
                    line.bom_path or "",
                    f"{quantity(line.quantity_designated)} {line.unit_of_measure}",
                ]
                for line in manufacturing
            ],
        )


def render_7552(request: PacketRequest) -> bytes:
    """CBP Form 7552 — Delivery Certificate for Purposes of Drawback."""
    transferor = request.manufacturer or request.claimant
    document = Document(
        "CBP Form 7552 - Delivery Certificate for Purposes of Drawback",
        f"Claim {request.claim_id} | prepared {request.prepared_on.isoformat()} | "
        f"transcription for filing, not a CBP-issued form",
    )
    document.paragraph(PREPARER_NOTICE)

    document.section(
        Section(
            title="Transferor / manufacturer",
            rows=[
                [
                    Field("cbp7552_transferor_name", "Name", transferor.name, 0.62),
                    Field("cbp7552_transferor_id", "Identifier", transferor.identifier, 0.38),
                ],
                [
                    Field(
                        "cbp7552_transferor_address",
                        "Address",
                        transferor.address_block(),
                        0.80,
                    ),
                    Field("cbp7552_transferor_country", "Country", transferor.country, 0.20),
                ],
            ],
        )
    )

    document.section(
        Section(
            title="Transferee / claimant",
            rows=[
                [
                    Field("cbp7552_transferee_name", "Name", request.claimant.name, 0.62),
                    Field("cbp7552_transferee_id", "Identifier", request.claimant.identifier, 0.38),
                ]
            ],
        )
    )

    is_manufacturing = any(line.theory in _MANUFACTURING for line in request.lines)
    document.section(
        Section(
            title="Basis",
            rows=[
                [
                    Field(
                        "cbp7552_basis",
                        "Certificate type",
                        (
                            "Certificate of manufacture and delivery"
                            if is_manufacturing
                            else "Delivery certificate"
                        ),
                        0.50,
                    ),
                    Field("cbp7552_provision", "Provision", _provision_for(request), 0.50),
                ],
                [
                    Field(
                        "cbp7552_total_quantity",
                        "Total quantity transferred",
                        quantity(request.total_quantity),
                        0.34,
                    ),
                    Field(
                        "cbp7552_total_duty",
                        "Duty attributable",
                        money(request.total_duty_allocated),
                        0.33,
                    ),
                    Field("cbp7552_line_count", "Lines certified", str(len(request.lines)), 0.33),
                ],
            ],
        )
    )

    document.section(
        Section(
            title="Certification",
            rows=[
                [
                    Field("cbp7552_signatory_name", "Signature of transferor", "", 0.45),
                    Field("cbp7552_signatory_title", "Title", "", 0.30),
                    Field("cbp7552_signature_date", "Date", "", 0.25),
                ]
            ],
            note=CERTIFICATION_7552,
        )
    )

    document.section(Section(title="Merchandise or articles delivered", rows=[]))
    document.table(
        headers=["Import entry", "Ln", "Import date", "HTS", "Description", "Qty", "Duty"],
        widths=[0.19, 0.04, 0.11, 0.12, 0.26, 0.14, 0.14],
        rows=[
            [
                line.import_declaration,
                str(line.import_line_number),
                line.import_date.isoformat(),
                line.import_hts,
                line.description,
                f"{quantity(line.quantity_designated)} {line.unit_of_measure}",
                money(line.duty_allocated),
            ]
            for line in request.lines
        ],
    )
    return document.render()


def needs_7552(request: PacketRequest) -> bool:
    """Whether a delivery certificate belongs in this packet.

    A 7552 is required when someone other than the claimant imported or manufactured the
    goods. Attaching one to a straight §1313(j) claim by the importer of record adds an
    unrequested document to the filing, which invites questions rather than answering them.
    """
    if request.manufacturer is not None:
        return True
    return any(line.theory in _MANUFACTURING for line in request.lines)


def build_us_packet(request: PacketRequest) -> FilingPacket:
    """Render the US filing packet: a 7551, and a 7552 where one is called for."""
    if request.jurisdiction is not Jurisdiction.US:
        msg = f"build_us_packet received a {request.jurisdiction} claim"
        raise ValueError(msg)

    packet = FilingPacket(
        claim_id=request.claim_id,
        jurisdiction=request.jurisdiction,
        citations=_citations_for(request),
    )

    if not request.lines:
        packet.warnings.append("no designated lines; the 7551 would claim nothing")

    packet.artifacts.append(
        PacketArtifact(
            filename=f"cbp7551-{request.claim_id}.pdf",
            media_type=PDF_MEDIA_TYPE,
            content=render_7551(request),
        )
    )

    if needs_7552(request):
        packet.artifacts.append(
            PacketArtifact(
                filename=f"cbp7552-{request.claim_id}.pdf",
                media_type=PDF_MEDIA_TYPE,
                content=render_7552(request),
            )
        )
    elif request.manufacturer is None and any(
        line.theory in _MANUFACTURING for line in request.lines
    ):  # pragma: no cover - unreachable, needs_7552 already covers it
        packet.warnings.append("manufacturing theory claimed with no transferor named")

    return packet


def field_values(pdf_bytes: bytes) -> dict[str, str]:
    """Read a rendered form's field values back.

    The packet's own read path, used by the golden fixtures and by anything downstream
    that needs the figures without re-deriving them from the claim.
    """
    reader = PdfReader(BytesIO(pdf_bytes))
    fields = reader.get_fields() or {}
    return {name: str(entry.get("/V", "")) for name, entry in fields.items()}

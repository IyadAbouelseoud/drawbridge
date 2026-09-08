"""Shared machinery for the two pilot seeders.

Week 13 files a backward-looking claim in each jurisdiction. This is the rehearsal
corpus it runs against while the real customer documents are still being collected — a
tenant, its historical import lines and its export lines, deterministic and re-runnable.

**Three things this is careful about.**

*It is fiction, and it says so in the data.* Every provenance box is stamped
`extractor="pilot-fixture"` and points at a document id that has no bytes behind it. A
figure on a Drawbridge claim is supposed to be traceable to a rectangle on a page
(`CLAUDE.md`), and these are traceable to nothing. The marker is what stops a packet built
from this corpus being mistaken for evidence, and `assert_not_evidence` is what a caller
uses to check before doing something irreversible with one.

*It is idempotent.* Every id is a UUID5 over the tenant slug and the line's natural key,
so running a seeder twice produces the same corpus rather than a second copy of it. A
pilot that doubles its own refund every time somebody re-runs the setup is a bad way to
discover that the matcher works.

*It connects as the owner.* Creating a tenant is an operator action — the same reasoning
as `scripts/e2e_pipeline_test.py` and `scripts/tenant_offboard.py`. A tenant cannot scope
a connection to itself before it exists, and an API that could mint tenants would be a
multi-tenancy hole rather than a convenience.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Language,
    Provenance,
    ProvenanceSpan,
    Span,
)
from drawbridge_schemas.tenant import TenantProfile
from drawbridge_schemas.trade import EntryLine, ExportLine
from services.api.src import profiles
from services.api.src.config import get_settings
from services.api.src.models import EntryLine as EntryLineRow
from services.api.src.models import ExportLine as ExportLineRow
from services.api.src.models import Tenant

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from drawbridge_schemas.jurisdiction import Jurisdiction

# The marker that makes a fixture-sourced figure recognisable everywhere downstream.
PILOT_EXTRACTOR = "pilot-fixture"

# Stable namespace, so a second run of a seeder rewrites the same rows.
PILOT_NAMESPACE = uuid5(NAMESPACE_URL, "https://drawbridge.local/pilot")


class PilotError(RuntimeError):
    """A seeding precondition that must not be worked around."""


def pilot_uuid(*parts: str) -> UUID:
    return uuid5(PILOT_NAMESPACE, "/".join(parts))


def fake_sha256(*parts: str) -> str:
    """A syntactically valid digest over the corpus key, not over any bytes.

    `ProvenanceSpan` requires 64 hex characters and it is right to: the hash is what lets
    an auditor check a box against a PDF they hold. Here there is no PDF, so this is a
    placeholder that satisfies the shape and matches nothing — which is exactly what
    `PILOT_EXTRACTOR` is there to declare.
    """
    return hashlib.sha256("/".join(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PilotTenant:
    slug: str
    name: str
    jurisdiction: str
    #: The filing identity. Fictional like everything else here, and structurally valid
    #: so that the packager exercises the real path: the EIN matches CBP's format and the
    #: IBAN passes mod-97, because a corpus whose identifiers fail validation would test
    #: the validator instead of the pipeline.
    profile: TenantProfile | None = None

    @property
    def tenant_id(self) -> UUID:
        return pilot_uuid("tenant", self.slug)


@dataclass(frozen=True, slots=True)
class PilotCorpus:
    """What one seeder produces: a tenant and the lines it is claiming over."""

    tenant: PilotTenant
    jurisdiction: Jurisdiction
    imports: tuple[EntryLine, ...]
    exports: tuple[ExportLine, ...]
    #: What week 13 should recover, computed from the corpus rather than asserted.
    expected_base: Decimal
    #: Anything the corpus is knowingly wrong about. Printed, never swallowed.
    caveats: tuple[str, ...] = ()


def document_ref(kind: DocumentKind, key: str, *, language: Language) -> DocumentRef:
    return DocumentRef(
        document_id=pilot_uuid("document", key),
        kind=kind,
        sha256=fake_sha256("document", key),
        object_key=f"pilot/{key}.pdf",
        page_count=1,
        language=language,
    )


def fixture_provenance(ref: DocumentRef, figures: Sequence[str]) -> Provenance:
    """Provenance whose every box is marked as invented.

    One distinct rectangle per figure rather than one shared box: a trace that returns
    the same coordinates for the duty and the VAT has located neither, and a pilot run
    built on that would exercise the traceability machinery without testing it.
    """
    return Provenance(
        spans=(Span(document=ref, page=1, field_path="lines[0]"),),
        confidence=Confidence(score=0.99, method=PILOT_EXTRACTOR),
        figures={
            name: ProvenanceSpan(
                document_id=ref.document_id,
                document_sha256=ref.sha256,
                page=1,
                x0=72.0,
                y0=110.0 + index * 15.0,
                x1=248.0,
                y1=123.0 + index * 15.0,
                field_path=name,
                raw_text=None,
                extractor=PILOT_EXTRACTOR,
            )
            for index, name in enumerate(figures)
        },
    )


def assert_not_evidence(corpus: PilotCorpus) -> None:
    """Refuse to treat a pilot corpus as a real extraction.

    Called by anything that would act on the corpus irreversibly — filing, or building a
    packet meant for a broker. The check is cheap and the failure it prevents is a
    fabricated figure reaching a customs authority under our name.
    """
    for line in corpus.imports:
        for name, span in line.provenance.figures.items():
            if span.extractor != PILOT_EXTRACTOR:
                msg = f"{line.declaration_number}:{name} is not marked as a pilot fixture"
                raise PilotError(msg)


@contextmanager
def owner_session() -> Iterator[Session]:
    """The owner connection. See the module docstring for why it is not the app role."""
    url = os.environ.get("DRAWBRIDGE_OWNER_DATABASE_URL") or get_settings().sync_database_url
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            engine.dispose()


def _ensure_tenant(session: Session, tenant: PilotTenant) -> Tenant:
    row = session.get(Tenant, tenant.tenant_id)
    if row is None:
        row = Tenant(
            tenant_id=tenant.tenant_id,
            name=tenant.name,
            default_jurisdiction=tenant.jurisdiction,
        )
        session.add(row)
        session.flush()
    elif row.offboarded_at is not None:
        # Reviving a tombstoned tenant would put live rows behind a signed archive that
        # says the relationship ended. Week 11 made that state unreachable through the
        # application and this keeps it unreachable through the seeder.
        msg = f"tenant {tenant.slug} is offboarded; it cannot be reseeded"
        raise PilotError(msg)

    if tenant.profile is not None:
        # Written every run rather than only on creation. The profile is what the packager
        # addresses a filing with, and a seeder that skipped it on an existing tenant
        # would leave an edited corpus filing under the previous corpus's identity.
        profiles.write(session, tenant.profile)
        session.flush()
    return row


def _entry_row(line: EntryLine) -> EntryLineRow:
    return EntryLineRow(
        line_id=line.line_id,
        tenant_id=line.tenant_id,
        jurisdiction=line.jurisdiction.value,
        currency=line.currency.value,
        declaration_number=line.declaration_number,
        line_number=line.line_number,
        import_date=line.import_date,
        declaration_date=line.declaration_date,
        duty_payment_date=line.duty_payment_date,
        port_of_entry=line.port_of_entry,
        country_of_origin=line.country_of_origin,
        hts_code=line.hts.code,
        description=line.description,
        quantity=line.quantity,
        unit_of_measure=line.unit_of_measure,
        entered_value=line.entered_value,
        duty_paid=line.duty_paid,
        mpf_paid=line.mpf_paid,
        hmf_paid=line.hmf_paid,
        section_301_duty=line.section_301_duty,
        other_duty=line.other_duty,
        vat_paid=line.vat_paid,
        excise_paid=line.excise_paid,
        quantity_designated=line.quantity_designated,
        provenance=line.provenance.model_dump(mode="json"),
    )


def _export_row(line: ExportLine) -> ExportLineRow:
    return ExportLineRow(
        line_id=line.line_id,
        tenant_id=line.tenant_id,
        jurisdiction=line.jurisdiction.value,
        reference=line.reference,
        line_number=line.line_number,
        export_date=line.export_date,
        destination_country=line.destination_country,
        is_destruction=line.is_destruction,
        hts_code=line.hts.code,
        description=line.description,
        quantity=line.quantity,
        unit_of_measure=line.unit_of_measure,
        declared_value=line.declared_value,
        quantity_claimed=line.quantity_claimed,
        linked_import_declaration=line.linked_import_declaration,
        consignment_id=line.consignment_id,
        is_partial_shipment=line.is_partial_shipment,
        unused_and_unaltered=line.unused_and_unaltered,
        provenance=line.provenance.model_dump(mode="json"),
    )


def seed(corpus: PilotCorpus, session: Session) -> dict[str, Any]:
    """Write the corpus. Safe to run twice; the second run is a no-op.

    Merged rather than inserted, because the ids are deterministic and a re-run is the
    expected way to pick up a corrected corpus. What it will not do is reseed a tenant
    whose lines have already been claimed: `quantity_designated` above zero means a claim
    exists over this data, and overwriting the line underneath it would leave a filed
    claim citing figures that no longer exist.
    """
    assert_not_evidence(corpus)
    _ensure_tenant(session, corpus.tenant)

    designated = session.execute(
        select(EntryLineRow.declaration_number)
        .where(EntryLineRow.tenant_id == corpus.tenant.tenant_id)
        .where(EntryLineRow.quantity_designated > 0)
    ).scalars()
    claimed = sorted(set(designated))
    if claimed:
        msg = (
            f"{len(claimed)} line(s) on {corpus.tenant.slug} are already designated to a "
            f"claim ({claimed[0]} ...); reseeding would rewrite figures a claim cites"
        )
        raise PilotError(msg)

    for entry in corpus.imports:
        session.merge(_entry_row(entry))
    for export in corpus.exports:
        session.merge(_export_row(export))
    session.flush()

    return {
        "tenant_id": str(corpus.tenant.tenant_id),
        "tenant": corpus.tenant.name,
        "jurisdiction": corpus.jurisdiction.value,
        "imports": len(corpus.imports),
        "exports": len(corpus.exports),
        "expected_base": str(corpus.expected_base),
    }


def trigger_payload(corpus: PilotCorpus) -> dict[str, Any]:
    """The body n8n's webhook takes, so week 13 is a POST rather than a rewrite.

    Lines are sent rather than referenced, matching `POST /claims/persist`: at trigger
    time the pipeline has not decided which of them it will use.
    """
    return {
        "tenant_id": str(corpus.tenant.tenant_id),
        "jurisdiction": corpus.jurisdiction.value,
        "imports": [line.model_dump(mode="json") for line in corpus.imports],
        "exports": [line.model_dump(mode="json") for line in corpus.exports],
    }


def report(corpus: PilotCorpus, written: dict[str, Any]) -> str:
    lines = [
        f"tenant      {written['tenant']}  ({written['tenant_id']})",
        f"lane        {written['jurisdiction']}",
        f"seeded      {written['imports']} import line(s), {written['exports']} export line(s)",
        f"duty at risk {corpus.expected_base}",
    ]
    lines += [f"CAVEAT      {caveat}" for caveat in corpus.caveats]
    return "\n".join(lines)

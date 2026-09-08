"""The audit ledger, against a real Postgres and through the MCP tools.

Two things are being checked and they are different in kind.

The first is a **trace**: a claim is persisted from lines whose figures carry measured
boxes, approved, and then one figure — the duty — is asked about by name. The answer has
to be a document hash, a page, and a rectangle. Anything vaguer is what the system had
before week 10, which was a page reference and a promise.

The second is **immutability**. Append-only is asserted everywhere in the docstrings; here
it is asserted against the database, because the property is enforced by triggers and a
trigger that was never installed looks exactly like one that works.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Language,
    Provenance,
    ProvenanceSpan,
    Span,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode, LineMatch, MatchTheory
from mcp_servers.mcp_claims import db as claims_db
from services.api.src.analyst import transition_claim
from services.api.src.ledger import LedgerError, record, verify_chain
from services.api.src.models import AuditLedger, Tenant
from services.api.src.persistence import persist_claim
from tests.integration.conftest import TEST_DSN

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

pytest_plugins = ("tests.integration.conftest",)

# The box the duty figure was read from, on page 2 of the Bayan. Arbitrary numbers, but
# fixed ones: the whole point of the trace is that it returns *these* and not a rectangle
# derived from something else.
DUTY_BOX = (312.5, 448.0, 396.25, 460.0)
BAYAN_SHA = "d" * 64


def _bayan_ref() -> DocumentRef:
    return DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.ZATCA_BAYAN,
        sha256=BAYAN_SHA,
        object_key="tenants/ledger/zatca_bayan/20240115447821.pdf",
        page_count=2,
        language=Language.MIXED,
    )


def _span(ref: DocumentRef, name: str, box: tuple[float, float, float, float]) -> ProvenanceSpan:
    x0, y0, x1, y1 = box
    return ProvenanceSpan(
        document_id=ref.document_id,
        document_sha256=ref.sha256,
        page=2,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        field_path=name,
        raw_text="46875.00",
        language=Language.MIXED,
        extractor="geometry-table",
    )


def _provenance(ref: DocumentRef) -> Provenance:
    return Provenance(
        spans=(Span(document=ref, page=2, language=Language.MIXED),),
        confidence=Confidence(score=0.96, method="pymupdf-native"),
        figures={
            "duty_paid": _span(ref, "duty_paid", DUTY_BOX),
            "entered_value": _span(ref, "entered_value", (312.5, 430.0, 396.25, 442.0)),
            "quantity": _span(ref, "quantity", (240.0, 448.0, 288.0, 460.0)),
            "vat_paid": _span(ref, "vat_paid", (312.5, 466.0, 396.25, 478.0)),
            "declared_value": _span(ref, "declared_value", (312.5, 484.0, 396.25, 496.0)),
        },
    )


def _lines(tenant_id: UUID) -> tuple[EntryLine, ExportLine]:
    ref = _bayan_ref()
    provenance = _provenance(ref)
    entry = EntryLine(
        line_id=uuid4(),
        tenant_id=tenant_id,
        jurisdiction=Jurisdiction.KSA,
        currency=Currency.SAR,
        declaration_number="20240115447821",
        line_number=1,
        import_date=date(2024, 1, 15),
        declaration_date=date(2024, 1, 15),
        duty_payment_date=date(2024, 2, 8),
        port_of_entry="Jeddah Islamic Port",
        country_of_origin="CN",
        hts=HTSCode(code="84713000"),
        description="Portable data processing machines",
        quantity=Decimal("1200"),
        unit_of_measure="PCE",
        entered_value=Decimal("937500.00"),
        duty_paid=Decimal("46875.00"),
        vat_paid=Decimal("147656.25"),
        provenance=provenance,
    )
    export = ExportLine(
        line_id=uuid4(),
        tenant_id=tenant_id,
        jurisdiction=Jurisdiction.KSA,
        reference="RE-20240912-0031",
        line_number=1,
        export_date=date(2024, 7, 12),
        destination_country="AE",
        hts=HTSCode(code="84713000"),
        description="Portable data processing machines, re-exported unused",
        quantity=Decimal("500"),
        unit_of_measure="PCE",
        declared_value=Decimal("390625.00"),
        linked_import_declaration="20240115447821",
        consignment_id="CNS-2024-0115-A",
        unused_and_unaltered=True,
        provenance=provenance,
    )
    return entry, export


def _match(entry: EntryLine, export: ExportLine) -> LineMatch:
    return LineMatch(
        import_line_id=entry.line_id,
        export_line_id=export.line_id,
        quantity=Decimal("500"),
        theory=MatchTheory.DECLARATION_LINKAGE,
        linked_import_declaration="20240115447821",
        duty_allocated=Decimal("19531.25"),
        refund_amount=Decimal("19531.25"),
        days_clock_start_to_export=155,
    )


# --------------------------------------------------------------------------- unit-ish


class TestTheChainIsWhatMakesItAudit:
    def test_each_entry_links_to_the_one_before_it(self, session: Session, tenant: Tenant) -> None:
        first = record(
            session, tenant_id=tenant.tenant_id, event_type="document_ingested", actor="pipeline"
        )
        second = record(
            session, tenant_id=tenant.tenant_id, event_type="extraction_run", actor="pipeline"
        )
        assert first.prev_hash is None
        assert second.prev_hash == first.entry_hash
        assert second.sequence > first.sequence
        assert verify_chain(session, tenant.tenant_id)["ok"] is True

    def test_two_tenants_do_not_share_a_chain(self, session: Session, tenant: Tenant) -> None:
        """Chained per tenant, not globally.

        A global chain would make one tenant's verification depend on rows they may not
        see, and would serialise every write in the system behind one advisory lock.
        """
        other = Tenant(tenant_id=uuid4(), name="Other", default_jurisdiction="us")
        session.add(other)
        session.flush()

        record(session, tenant_id=tenant.tenant_id, event_type="extraction_run", actor="pipeline")
        theirs = record(
            session, tenant_id=other.tenant_id, event_type="extraction_run", actor="pipeline"
        )
        assert theirs.prev_hash is None
        assert verify_chain(session, other.tenant_id)["entries"] == 1

    def test_an_altered_payload_is_detected(self, session: Session, tenant: Tenant) -> None:
        """The case the triggers cannot cover: a row changed around the application.

        Simulated with a direct UPDATE that suspends the guard, because that is what the
        threat actually is — someone with database access, not someone calling `record`.
        """
        row = record(
            session,
            tenant_id=tenant.tenant_id,
            event_type="valuation_override",
            actor="analyst@example.com",
            payload={"corrected_value": "18750.00"},
        )
        session.execute(text("ALTER TABLE audit_ledger DISABLE TRIGGER USER"))
        session.execute(
            text("UPDATE audit_ledger SET payload = :p WHERE ledger_id = :i"),
            {"p": '{"corrected_value": "99999.00"}', "i": str(row.ledger_id)},
        )
        session.execute(text("ALTER TABLE audit_ledger ENABLE TRIGGER USER"))
        session.expire_all()

        result = verify_chain(session, tenant.tenant_id)
        assert result["ok"] is False
        assert result["broken_at"] == row.sequence
        assert "altered" in result["detail"]

    def test_an_unknown_event_type_is_refused_before_it_reaches_the_database(
        self, session: Session, tenant: Tenant
    ) -> None:
        with pytest.raises(LedgerError, match="unknown ledger event type"):
            record(session, tenant_id=tenant.tenant_id, event_type="whatever", actor="pipeline")


class TestTheDatabaseRefusesToForget:
    """Append-only asserted against Postgres rather than against a docstring."""

    def test_an_update_is_refused(self, session: Session, tenant: Tenant) -> None:
        row = record(
            session, tenant_id=tenant.tenant_id, event_type="claim_transition", actor="pipeline"
        )
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                text("UPDATE audit_ledger SET actor = 'someone else' WHERE ledger_id = :i"),
                {"i": str(row.ledger_id)},
            )

    def test_a_delete_is_refused(self, session: Session, tenant: Tenant) -> None:
        row = record(
            session, tenant_id=tenant.tenant_id, event_type="claim_transition", actor="pipeline"
        )
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                text("DELETE FROM audit_ledger WHERE ledger_id = :i"), {"i": str(row.ledger_id)}
            )

    def test_a_truncate_is_refused(self, session: Session, tenant: Tenant) -> None:
        """Row triggers do not fire on TRUNCATE, so this needs its own guard.

        Without it the one command that empties the whole ledger would be the one command
        the row guards never see.
        """
        record(session, tenant_id=tenant.tenant_id, event_type="claim_transition", actor="n8n")
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text("TRUNCATE audit_ledger"))

    def test_a_tenant_cannot_be_deleted_out_from_under_its_records(
        self, session: Session, tenant: Tenant
    ) -> None:
        """RESTRICT, not CASCADE.

        A retention obligation that a tenant deletion satisfies is not a retention
        obligation. The consequence — that offboarding a tenant is a deliberate, manual
        act — is the correct one for a five-year record.
        """
        record(session, tenant_id=tenant.tenant_id, event_type="packet_built", actor="pipeline")
        with pytest.raises(IntegrityError):
            session.execute(
                text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": str(tenant.tenant_id)}
            )


class TestPersistenceWritesTheTrace:
    def test_every_figure_lands_in_the_ledger_with_its_box(
        self, session: Session, tenant: Tenant
    ) -> None:
        entry, export = _lines(tenant.tenant_id)
        persist_claim(
            session,
            tenant_id=tenant.tenant_id,
            jurisdiction=Jurisdiction.KSA,
            imports=[entry],
            exports=[export],
            matches=[_match(entry, export)],
            total_refund=Decimal("19531.25"),
            requires_review=False,
        )
        traced = [
            row
            for row in session.query(AuditLedger)
            .filter(AuditLedger.tenant_id == tenant.tenant_id)
            .all()
            if row.event_type == "figure_traced"
        ]
        assert len(traced) == 2  # one entry line, one export line
        duty = traced[0].payload["figures"]["duty_paid"]
        assert duty["document_sha256"] == BAYAN_SHA
        assert duty["page"] == 2
        assert duty["bbox"] == list(DUTY_BOX)

    def test_the_transition_is_recorded_alongside_it(
        self, session: Session, tenant: Tenant
    ) -> None:
        entry, export = _lines(tenant.tenant_id)
        result = persist_claim(
            session,
            tenant_id=tenant.tenant_id,
            jurisdiction=Jurisdiction.KSA,
            imports=[entry],
            exports=[export],
            matches=[_match(entry, export)],
            total_refund=Decimal("19531.25"),
            requires_review=False,
        )
        transition_claim(
            session,
            claim_id=UUID(result["claim_id"]),
            to_state="approved",
            actor="pipeline",
            reason="triage raised nothing",
        )
        events = [
            row.event_type
            for row in session.query(AuditLedger)
            .filter(AuditLedger.claim_id == UUID(result["claim_id"]))
            .order_by(AuditLedger.sequence)
            .all()
        ]
        assert events[0] == "claim_persisted"
        assert events[-1] == "claim_transition"
        assert verify_chain(session, tenant.tenant_id)["ok"] is True


# ------------------------------------------------------------------ through mcp-ledger


@pytest.fixture(autouse=True)
def _point_tools_at_the_test_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DRAWBRIDGE_DATABASE_URL", TEST_DSN)
    claims_db.engine.cache_clear()
    claims_db._session_factory.cache_clear()
    yield
    claims_db.engine.cache_clear()
    claims_db._session_factory.cache_clear()


@pytest.fixture
def approved_claim(engine: Engine) -> Iterator[tuple[UUID, UUID]]:
    """A committed, approved GCC claim whose duty figure carries a measured box.

    Committed because the MCP tools open their own connections and cannot see an
    uncommitted outer transaction. Torn down by hand, and the ledger rows need the guards
    suspended to remove — which is the mechanism working, not a workaround for it.
    """
    factory = sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid4()

    with factory() as session:
        session.add(
            Tenant(tenant_id=tenant_id, name="Ledger Test Tenant", default_jurisdiction="ksa")
        )
        session.flush()
        entry, export = _lines(tenant_id)
        result = persist_claim(
            session,
            tenant_id=tenant_id,
            jurisdiction=Jurisdiction.KSA,
            imports=[entry],
            exports=[export],
            matches=[_match(entry, export)],
            total_refund=Decimal("19531.25"),
            requires_review=False,
        )
        claim_id = UUID(result["claim_id"])
        transition_claim(
            session, claim_id=claim_id, to_state="approved", actor="pipeline", reason="clean"
        )
        session.commit()

    yield tenant_id, claim_id

    with factory() as session:
        session.execute(text("DELETE FROM refund_lines WHERE claim_id = :c"), {"c": claim_id})
        session.execute(text("DELETE FROM claim_transitions WHERE claim_id = :c"), {"c": claim_id})
        session.execute(text("DELETE FROM claims WHERE claim_id = :c"), {"c": claim_id})
        session.execute(text("DELETE FROM entry_lines WHERE tenant_id = :t"), {"t": tenant_id})
        session.execute(text("DELETE FROM export_lines WHERE tenant_id = :t"), {"t": tenant_id})
        session.execute(text("ALTER TABLE audit_ledger DISABLE TRIGGER USER"))
        session.execute(text("DELETE FROM audit_ledger WHERE tenant_id = :t"), {"t": tenant_id})
        session.execute(text("ALTER TABLE audit_ledger ENABLE TRIGGER USER"))
        session.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant_id})
        session.commit()


def _ledger_tools() -> Any:
    from mcp_servers.mcp_ledger import server as ledger

    return ledger


class TestTraceFigure:
    def test_the_duty_traces_to_a_box_on_the_bayan(self, approved_claim: tuple[UUID, UUID]) -> None:
        """The question 19 CFR §163 and Art. 175 actually reduce to.

        Not "show me the claim" and not "show me the document" — *show me where this
        number came from*. The answer is a hash an auditor can verify against a PDF in
        their own hands, a page, and a rectangle on it.
        """
        _, claim_id = approved_claim
        result = _ledger_tools().trace_figure(claim_id=str(claim_id), field="duty_paid")

        assert result["ok"] is True
        assert result["found"] is True
        hit = result["ledger"][0]
        assert hit["document_sha256"] == BAYAN_SHA
        assert hit["page"] == 2
        assert hit["bbox"] == list(DUTY_BOX)
        assert hit["extractor"] == "geometry-table"

    def test_the_ledger_and_the_line_rows_agree(self, approved_claim: tuple[UUID, UUID]) -> None:
        """Two independent copies, compared rather than one trusted.

        The ledger copy is append-only; the `provenance` column on the line is live. They
        should say the same thing, and `consistent` is how a disagreement surfaces instead
        of being resolved silently in favour of whichever was read first.
        """
        _, claim_id = approved_claim
        result = _ledger_tools().trace_figure(claim_id=str(claim_id), field="duty_paid")
        assert result["consistent"] is True
        assert result["live"][0]["bbox"] == list(DUTY_BOX)

    def test_a_line_that_was_persisted_but_not_claimed_is_not_a_discrepancy(self) -> None:
        """The ledger holds more than the claim shows, and that is correct.

        A GCC claim whose second re-export fell under the Art. 16 §2 minimum persists
        both export lines and claims one. The unclaimed line still has a ledger entry —
        the record of something considered and excluded, which is what an audit of a
        rejection asks for — and the comparison is containment for exactly that reason.
        """
        from mcp_servers.mcp_ledger.server import _spans_agree

        claimed = {
            "line_id": "a",
            "document_sha256": BAYAN_SHA,
            "page": 2,
            "bbox": list(DUTY_BOX),
        }
        excluded = {
            "line_id": "b",
            "document_sha256": BAYAN_SHA,
            "page": 2,
            "bbox": [10.0, 20.0, 30.0, 40.0],
        }
        assert _spans_agree([claimed, excluded], [claimed]) is True
        # The direction that must still fail: a claimed figure the ledger never recorded.
        assert _spans_agree([excluded], [claimed]) is False

    def test_a_figure_nobody_recorded_is_not_invented(
        self, approved_claim: tuple[UUID, UUID]
    ) -> None:
        """`found: false`, not a nearby box.

        Returning the closest thing available would be worse than returning nothing: an
        auditor would be shown a rectangle holding some other number, and nothing in the
        answer would say so.
        """
        _, claim_id = approved_claim
        result = _ledger_tools().trace_figure(claim_id=str(claim_id), field="hmf_paid")
        assert result["ok"] is True
        assert result["found"] is False
        assert result["ledger"] == []

    def test_a_bad_claim_id_comes_back_as_data(self) -> None:
        result = _ledger_tools().trace_figure(claim_id="not-a-uuid", field="duty_paid")
        assert result["ok"] is False
        assert result["error"] == "ValueError"


class TestTheOtherLedgerTools:
    def test_the_chain_verifies_through_the_tool(self, approved_claim: tuple[UUID, UUID]) -> None:
        tenant_id, _ = approved_claim
        result = _ledger_tools().ledger_chain(tenant_id=str(tenant_id))
        assert result["ok"] is True
        assert result["broken_at"] is None
        assert result["entries"] >= 4

    def test_the_claim_ledger_reads_in_sequence_order(
        self, approved_claim: tuple[UUID, UUID]
    ) -> None:
        """Ordered by `sequence`, not by timestamp.

        Persistence writes its rows inside one transaction, so several share a
        `recorded_at` to the microsecond. Which came first is exactly what an audit of an
        override asks about, and a timestamp cannot answer it.
        """
        _, claim_id = approved_claim
        result = _ledger_tools().claim_ledger(claim_id=str(claim_id))
        sequences = [event["sequence"] for event in result["events"]]
        assert sequences == sorted(sequences)
        assert result["events"][0]["event_type"] == "claim_persisted"
        assert result["events"][-1]["event_type"] == "claim_transition"


def test_the_test_database_is_the_one_being_used() -> None:
    """Guard on the fixture, not on the code.

    These tests delete rows and suspend triggers. Doing that against a database that was
    not the test one would be a bad afternoon.
    """
    assert os.environ["DRAWBRIDGE_DATABASE_URL"] == TEST_DSN

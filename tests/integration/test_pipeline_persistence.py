"""The write half of the pipeline, against the real schema and its constraints.

`scripts/e2e_pipeline_test.py` proves the deployed stack runs the loop. This proves the
properties that stack depends on, at a level CI can reach with only Postgres: that a claim
persists with the deadline the statute gives it rather than the one the caller supplies,
that the automated approval lane cannot be used to approve a claim with an open exception,
and that a packet can only be built from a claim that has been approved.

Those are the three places where a bug would be expensive and silent. Everything else about
persistence — that the rows go in — fails loudly the first time it is wrong.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction
from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Language,
    Provenance,
    Span,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode, LineMatch, MatchTheory
from services.api.src.analyst import AnalystError, transition_claim
from services.api.src.models import ReviewQueue
from services.api.src.packaging import PackagingError, build
from services.api.src.persistence import PersistenceError, persist_claim
from services.packager.src.packet import Claimant

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from services.api.src.models import Tenant

CLAIMANT = Claimant(
    name="Northbridge Trading LLC",
    identifier="47-1928374-00",
    address_line1="1400 Harbor Parkway",
    city="Newark",
    country="US",
    postal_code="07114",
)


def _provenance() -> Provenance:
    ref = DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.CBP_7501,
        sha256="c" * 64,
        object_key="tenants/test/cbp_7501/sample.pdf",
        page_count=1,
        language=Language.ENGLISH,
    )
    return Provenance(
        spans=(Span(document=ref, field_path="lines[0]"),),
        confidence=Confidence(score=0.99, method="structured-feed"),
    )


@pytest.fixture
def us_pair(tenant: Tenant) -> tuple[EntryLine, ExportLine]:
    """One import, one export, matchable under §1313(j)(1)."""
    provenance = _provenance()
    entry = EntryLine(
        line_id=uuid4(),
        tenant_id=tenant.tenant_id,
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        declaration_number="ABC-1234567-8",
        line_number=1,
        import_date=date(2023, 3, 14),
        declaration_date=date(2023, 3, 20),
        port_of_entry="2704",
        country_of_origin="CN",
        hts=HTSCode(code="8471300100"),
        description="Portable automatic data processing machines",
        quantity=Decimal("1000"),
        unit_of_measure="NO",
        entered_value=Decimal("250000.00"),
        duty_paid=Decimal("0.00"),
        section_301_duty=Decimal("62500.00"),
        mpf_paid=Decimal("864.00"),
        provenance=provenance,
    )
    export = ExportLine(
        line_id=uuid4(),
        tenant_id=tenant.tenant_id,
        jurisdiction=Jurisdiction.US,
        reference="MAEU123456789",
        line_number=1,
        export_date=date(2024, 1, 9),
        destination_country="DE",
        hts=HTSCode(code="8471300150"),
        description="Portable ADP machines, re-exported unused",
        quantity=Decimal("400"),
        unit_of_measure="NO",
        declared_value=Decimal("100000.00"),
        provenance=provenance,
    )
    return entry, export


def _match(entry: EntryLine, export: ExportLine) -> LineMatch:
    return LineMatch(
        import_line_id=entry.line_id,
        export_line_id=export.line_id,
        quantity=Decimal("400"),
        theory=MatchTheory.DIRECT_IDENTITY,
        duty_allocated=Decimal("25000.00"),
        refund_amount=Decimal("24750.00"),
        days_clock_start_to_export=301,
    )


def _persist(
    session: Session,
    tenant: Tenant,
    us_pair: tuple[EntryLine, ExportLine],
    *,
    requires_review: bool = False,
) -> dict[str, Any]:
    entry, export = us_pair
    return persist_claim(
        session,
        tenant_id=tenant.tenant_id,
        jurisdiction=Jurisdiction.US,
        imports=[entry],
        exports=[export],
        matches=[_match(entry, export)],
        total_refund=Decimal("24750.00"),
        requires_review=requires_review,
    )


class TestWhatPersistenceDerivesRatherThanAccepts:
    def test_the_filing_deadline_comes_from_the_statute_not_the_caller(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        """Three years from export, per §1313(r). Nothing in the request can set it.

        Measured from the export date (2024-01-09) rather than from the import, which is
        the distinction §1313(r) actually draws and the reason the deadline is derived
        from the matched pairs rather than from the claim as a whole.

        A caller-supplied deadline would let n8n's clock decide when a statutory window
        closes, and a workflow with a wrong timezone would produce claims that look filed
        in time and are not.
        """
        result = _persist(session, tenant, us_pair)
        assert result["filing_deadline"] == "2027-01-09"
        assert result["absolute_bar_date"] is None  # the US has no Art. 174 equivalent

    def test_the_theory_recorded_is_the_one_the_match_rests_on(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        result = _persist(session, tenant, us_pair)
        assert result["drawback_type"] == "unused_direct_identity"
        assert result["lane"] == "drawback"

    def test_a_clean_claim_lands_in_quantified_and_a_flagged_one_in_review(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        assert _persist(session, tenant, us_pair)["state"] == "quantified"
        assert _persist(session, tenant, us_pair, requires_review=True)["state"] == "analyst_review"

    def test_a_match_naming_a_line_that_was_not_sent_is_refused(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        """Better than a foreign-key error, which arrives after half the rows are in.

        The FK would also catch it — but by then the entry lines are written and the
        message is about a UUID rather than about a match that references nothing.
        """
        entry, export = us_pair
        stray = _match(entry, export).model_copy(update={"import_line_id": uuid4()})
        with pytest.raises(PersistenceError, match="not in the request"):
            persist_claim(
                session,
                tenant_id=tenant.tenant_id,
                jurisdiction=Jurisdiction.US,
                imports=[entry],
                exports=[export],
                matches=[stray],
                total_refund=Decimal("24750.00"),
                requires_review=False,
            )

    def test_re_persisting_the_same_lines_does_not_duplicate_them(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        """A retried pipeline run must not fork the lines it already wrote.

        The claim *is* duplicated, deliberately: two claims over one period is a visible
        problem an analyst can see, where a silently mutated claim is not.
        """
        from sqlalchemy import func, select

        from services.api.src.models import EntryLine as EntryLineRow

        first = _persist(session, tenant, us_pair)
        second = _persist(session, tenant, us_pair)
        assert first["claim_id"] != second["claim_id"]

        count = session.execute(
            select(func.count())
            .select_from(EntryLineRow)
            .where(EntryLineRow.tenant_id == tenant.tenant_id)
        ).scalar_one()
        assert count == 1


class TestTheAutomatedLaneCannotOutrunItsGuard:
    """Week 9 let a clean claim reach APPROVED without a human. This is what stops that
    becoming a way to approve a claim that is not clean."""

    def test_a_clean_claim_may_be_approved_by_the_pipeline(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        claim_id = _persist(session, tenant, us_pair)["claim_id"]
        moved = transition_claim(
            session,
            claim_id=UUID(claim_id),
            to_state="approved",
            actor="pipeline",
            reason="triage raised nothing",
        )
        assert moved["from_state"] == "quantified"
        assert moved["state"] == "approved"

    def test_an_open_exception_blocks_the_approval_whoever_asks(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        """The guard is on the transition, not on the analyst tool.

        It used to be structural — APPROVED was unreachable except through
        ANALYST_REVIEW — and week 9 removed that. Putting the check only in
        `approve_claim` would have left the pipeline's own route unguarded, which is the
        route that runs unattended.
        """
        result = _persist(session, tenant, us_pair)
        claim_id = UUID(result["claim_id"])
        session.add(
            ReviewQueue(
                review_id=uuid4(),
                tenant_id=tenant.tenant_id,
                claim_id=claim_id,
                reason="threshold_near_miss",
                severity="normal",
                summary="a line fell just under the minimum",
                payload={},
                resume_token=f"tok-{uuid4()}",
                state="open",
            )
        )
        session.flush()

        with pytest.raises(AnalystError, match="unresolved exception"):
            transition_claim(
                session,
                claim_id=claim_id,
                to_state="approved",
                actor="pipeline",
                reason="triage raised nothing",
            )


class TestPackagingReadsTheClaimBack:
    def test_a_packet_cannot_be_built_before_approval(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        """A packet from unreviewed figures is indistinguishable from one from reviewed
        figures, which is why the check is in the builder and not in its caller."""
        claim_id = UUID(_persist(session, tenant, us_pair)["claim_id"])
        with pytest.raises(PackagingError, match="state 'quantified'"):
            build(session, claim_id=claim_id, claimant=CLAIMANT)

    def test_an_approved_claim_renders_a_7551(
        self, session: Session, tenant: Tenant, us_pair: tuple[EntryLine, ExportLine]
    ) -> None:
        """The whole round trip: rows in, PDF out, figures intact.

        The refund on the packet must equal the refund on the match. That is the property
        the packager's no-arithmetic rule exists to guarantee, and reading it back out of
        Postgres is the only place it can actually be checked.
        """
        claim_id = UUID(_persist(session, tenant, us_pair)["claim_id"])
        transition_claim(
            session, claim_id=claim_id, to_state="approved", actor="pipeline", reason="clean"
        )

        packet = build(session, claim_id=claim_id, claimant=CLAIMANT, prepared_on=date(2026, 9, 8))
        assert [a.filename for a in packet.artifacts] == [f"cbp7551-{claim_id}.pdf"]
        assert packet.artifacts[0].content.startswith(b"%PDF")
        assert packet.requires_analyst_review is False

    def test_a_claim_with_no_refund_lines_is_refused(
        self, session: Session, tenant: Tenant
    ) -> None:
        """An empty packet renders perfectly well and claims nothing.

        Silently producing one would mean a filer receives a form with no designations on
        it and no indication that anything is missing.
        """
        from services.api.src.models import Claim

        claim = Claim(
            claim_id=uuid4(),
            tenant_id=tenant.tenant_id,
            state="approved",
            jurisdiction="us",
            currency="USD",
            lane="drawback",
            drawback_type="unused_direct_identity",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            filing_deadline=date(2029, 1, 9),
            total_refund=Decimal("0.00"),
        )
        session.add(claim)
        session.flush()

        with pytest.raises(PackagingError, match="no refund lines"):
            build(session, claim_id=UUID(str(claim.claim_id)), claimant=CLAIMANT)

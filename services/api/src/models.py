"""SQLAlchemy models — the persistence side of the claim state machine.

Postgres owns claim state. n8n observes transitions and fires side effects; it never
holds business state (docs/ARCHITECTURE.md §4).

Dual-jurisdiction: the same tables carry US and GCC claims. Jurisdiction-specific columns
are nullable at the database level and enforced by the rules engine and by CHECK
constraints where the invariant is absolute — a GCC re-export line without its linked
import declaration has no statutory theory at all, so the database refuses it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Money precision matches the schema package: 16 digits, 2 decimal places, Decimal only.
Money = Numeric(16, 2)
Quantity = Numeric(18, 4)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))
    # Default filing jurisdiction. A tenant may hold claims in both.
    default_jurisdiction: Mapped[str] = mapped_column(String(8), default="us")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("default_jurisdiction IN ('us','ksa')", name="ck_tenant_jurisdiction"),
    )


class Document(Base):
    """Immutable, content-addressed source document.

    Rows mirror objects in MinIO. `sha256` is unique per tenant because the object key is
    content-addressed — the same bytes ingested twice occupy one object and one row.
    """

    __tablename__ = "documents"

    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(48))
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(8), default="en")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "sha256", name="uq_document_tenant_sha256"),
        CheckConstraint("language IN ('en','ar','mixed')", name="ck_document_language"),
        CheckConstraint("char_length(sha256) = 64", name="ck_document_sha256_length"),
        Index("ix_documents_tenant_kind", "tenant_id", "kind"),
    )


class EntryLine(Base):
    """One line of an import declaration. US CBP 7501 / ACE, or KSA ZATCA Bayan."""

    __tablename__ = "entry_lines"

    line_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="RESTRICT")
    )
    jurisdiction: Mapped[str] = mapped_column(String(8))
    currency: Mapped[str] = mapped_column(String(3))

    declaration_number: Mapped[str] = mapped_column(String(64))
    line_number: Mapped[int] = mapped_column(Integer)

    import_date: Mapped[date] = mapped_column(Date)
    declaration_date: Mapped[date] = mapped_column(Date)
    # Anchors the GCC re-export window (Rules of Impl. Art. 16 §3(a)). Differs from
    # import_date whenever duty payment is postponed — ZATCA permits up to 30 days.
    duty_payment_date: Mapped[date | None] = mapped_column(Date)

    # 64, not 16. A US port is a 4-digit CBP code; a GCC one is a name — "Jeddah Islamic
    # Port" — and the narrow column made every KSA claim unpersistable until week 9,
    # when something first tried to write one. See migration e5c48b71d90a.
    port_of_entry: Mapped[str] = mapped_column(String(64))
    country_of_origin: Mapped[str] = mapped_column(String(2))

    hts_code: Mapped[str] = mapped_column(String(12))
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    unit_of_measure: Mapped[str] = mapped_column(String(16))

    entered_value: Mapped[Decimal] = mapped_column(Money)
    duty_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    mpf_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    hmf_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    section_301_duty: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    other_duty: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    # Tracked for reconciliation; never part of a drawback base in either jurisdiction.
    vat_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    excise_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    quantity_designated: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"))

    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "declaration_number",
            "line_number",
            name="uq_entry_line_declaration",
        ),
        CheckConstraint("jurisdiction IN ('us','ksa')", name="ck_entry_jurisdiction"),
        CheckConstraint("quantity_designated <= quantity", name="ck_entry_not_over_designated"),
        CheckConstraint("quantity > 0", name="ck_entry_quantity_positive"),
        # GCC clock runs from duty payment, so a KSA line without it cannot be aged.
        CheckConstraint(
            "jurisdiction <> 'ksa' OR duty_payment_date IS NOT NULL",
            name="ck_ksa_requires_payment_date",
        ),
        Index("ix_entry_lines_tenant_hts", "tenant_id", "hts_code"),
        Index("ix_entry_lines_clock", "tenant_id", "jurisdiction", "duty_payment_date"),
    )


class ExportLine(Base):
    """One line of an export, re-export, or destruction event."""

    __tablename__ = "export_lines"

    line_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="RESTRICT")
    )
    jurisdiction: Mapped[str] = mapped_column(String(8))

    reference: Mapped[str] = mapped_column(String(128))
    line_number: Mapped[int] = mapped_column(Integer)

    export_date: Mapped[date] = mapped_column(Date)
    destination_country: Mapped[str | None] = mapped_column(String(2))
    is_destruction: Mapped[bool] = mapped_column(default=False)

    hts_code: Mapped[str] = mapped_column(String(12))
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    unit_of_measure: Mapped[str] = mapped_column(String(16))
    declared_value: Mapped[Decimal | None] = mapped_column(Money)

    quantity_claimed: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"))

    # GCC linkage — Rules of Impl. Art. 15(c) and ZATCA Res. 28624.
    linked_import_declaration: Mapped[str | None] = mapped_column(String(64))
    consignment_id: Mapped[str | None] = mapped_column(String(128))
    is_partial_shipment: Mapped[bool] = mapped_column(default=False)
    unused_and_unaltered: Mapped[bool] = mapped_column(default=True)

    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", "line_number", name="uq_export_line_ref"),
        CheckConstraint("jurisdiction IN ('us','ksa')", name="ck_export_jurisdiction"),
        CheckConstraint("quantity_claimed <= quantity", name="ck_export_not_over_claimed"),
        CheckConstraint(
            "is_destruction OR destination_country IS NOT NULL",
            name="ck_export_destination_or_destruction",
        ),
        # A GCC re-export without its linked import declaration has no statutory theory.
        CheckConstraint(
            "jurisdiction <> 'ksa' OR linked_import_declaration IS NOT NULL",
            name="ck_ksa_requires_declaration_link",
        ),
        Index("ix_export_lines_linkage", "tenant_id", "linked_import_declaration"),
    )


class Claim(Base):
    """The aggregate handed to a licensed filer.

    Drawbridge never files with a customs authority; `state` tops out at HANDED_OFF for
    anything we control, and FILED/PAID reflect what the filer reports back.
    """

    __tablename__ = "claims"

    claim_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="RESTRICT")
    )

    state: Mapped[str] = mapped_column(String(24), default="intake")
    jurisdiction: Mapped[str] = mapped_column(String(8))
    currency: Mapped[str] = mapped_column(String(3))
    lane: Mapped[str] = mapped_column(String(32))
    drawback_type: Mapped[str | None] = mapped_column(String(40))

    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    filing_deadline: Mapped[date] = mapped_column(Date)
    # GCC Art. 174 three-year prescription. Null in the US, which has no equivalent bar.
    absolute_bar_date: Mapped[date | None] = mapped_column(Date)

    total_refund: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    refund_lines: Mapped[list[RefundLine]] = relationship(
        back_populates="claim", cascade="all, delete-orphan"
    )
    transitions: Mapped[list[ClaimTransition]] = relationship(
        back_populates="claim", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("jurisdiction IN ('us','ksa')", name="ck_claim_jurisdiction"),
        CheckConstraint("period_end >= period_start", name="ck_claim_period_ordered"),
        CheckConstraint(
            "state IN ('intake','extracting','extracted','classifying','classified',"
            "'matching','matched','quantified','analyst_review','approved','packaged',"
            "'handed_off','filed','paid','rejected','expired')",
            name="ck_claim_state",
        ),
        # Lanes are jurisdiction-specific. A GCC claim cannot travel a US statute.
        CheckConstraint(
            "(jurisdiction = 'us' AND lane IN "
            "('drawback','post_summary_correction','fta_retroactive')) OR "
            "(jurisdiction = 'ksa' AND lane IN "
            "('gcc_reexport_drawback','ksa_origin_refund'))",
            name="ck_claim_lane_matches_jurisdiction",
        ),
        # Substitution is a US-only theory; the GCC recognises none.
        CheckConstraint(
            "jurisdiction = 'us' OR drawback_type NOT IN "
            "('unused_substitution','manufacturing_substitution')",
            name="ck_no_substitution_outside_us",
        ),
        CheckConstraint(
            "(jurisdiction = 'us' AND currency = 'USD') OR "
            "(jurisdiction = 'ksa' AND currency = 'SAR')",
            name="ck_claim_currency_matches_jurisdiction",
        ),
        Index("ix_claims_tenant_state", "tenant_id", "state"),
        Index("ix_claims_deadline", "jurisdiction", "filing_deadline"),
    )


class RefundLine(Base):
    """One quantified refund component, traceable to the match that produced it."""

    __tablename__ = "refund_lines"

    refund_line_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    claim_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.claim_id", ondelete="CASCADE")
    )
    import_line_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entry_lines.line_id", ondelete="RESTRICT")
    )
    export_line_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("export_lines.line_id", ondelete="RESTRICT")
    )

    theory: Mapped[str] = mapped_column(String(32))
    substitution_key: Mapped[str | None] = mapped_column(String(8))
    linked_import_declaration: Mapped[str | None] = mapped_column(String(64))

    quantity: Mapped[Decimal] = mapped_column(Quantity)
    duty_component: Mapped[Decimal] = mapped_column(Money)
    mpf_component: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    hmf_component: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    refund_amount: Mapped[Decimal] = mapped_column(Money)

    days_clock_start_to_export: Mapped[int] = mapped_column(Integer)

    claim: Mapped[Claim] = relationship(back_populates="refund_lines")

    __table_args__ = (
        CheckConstraint(
            "theory IN ('direct_identity','hts_substitution','declaration_linkage')",
            name="ck_refund_theory",
        ),
        CheckConstraint("quantity > 0", name="ck_refund_quantity_positive"),
        # Each theory must carry the evidence that makes it a theory.
        CheckConstraint(
            "theory <> 'hts_substitution' OR substitution_key IS NOT NULL",
            name="ck_substitution_has_key",
        ),
        CheckConstraint(
            "theory <> 'declaration_linkage' OR linked_import_declaration IS NOT NULL",
            name="ck_linkage_has_declaration",
        ),
        Index("ix_refund_lines_claim", "claim_id"),
    )


class ClaimTransition(Base):
    """Append-only state-machine audit trail.

    Never updated, never deleted — this is part of the recordkeeping posture that has to
    answer an audit years later (19 CFR §163; GCC Art. 175 / ZATCA five-year originals).
    """

    __tablename__ = "claim_transitions"

    transition_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    claim_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.claim_id", ondelete="CASCADE")
    )
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(
        String(128), doc="'pipeline', 'n8n', or an analyst identifier"
    )
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    claim: Mapped[Claim] = relationship(back_populates="transitions")

    __table_args__ = (Index("ix_claim_transitions_claim", "claim_id", "occurred_at"),)


class ReviewQueue(Base):
    """Human-in-the-loop exception queue.

    n8n suspends a claim here rather than guessing. Three triggers, each a case where
    proceeding silently would produce a plausible-looking figure with no basis:

    - the matcher returned a status that is not provably optimal;
    - extraction confidence fell below the floor, so a figure may be misread;
    - a GCC claim landed near the Art. 16 §2 threshold, or had no rate on file.

    `payload` carries the matcher's rejections and the solver metadata verbatim, so an
    analyst opening the queue sees the provisions that failed and the figures involved
    without re-running anything.
    """

    __tablename__ = "review_queue"

    review_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="RESTRICT")
    )
    claim_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.claim_id", ondelete="CASCADE")
    )

    reason: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16), default="normal")
    summary: Mapped[str] = mapped_column(Text)
    citation: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # n8n suspends on this row and resumes when it clears. The token is what the
    # workflow waits on, so it is unique and never reused.
    resume_token: Mapped[str] = mapped_column(String(64), unique=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(128))

    state: Mapped[str] = mapped_column(String(16), default="open")
    assigned_to: Mapped[str | None] = mapped_column(String(128))
    resolution: Mapped[str | None] = mapped_column(String(24))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    # Agent pre-analysis, drafted before an analyst opens the row. Advisory only: nothing
    # in `services/agent` writes claim state, and `resolution` still requires a human.
    # Separate from `payload` so the matcher's record stays verbatim and comparable
    # against a re-run.
    agent_memo: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    agent_model: Mapped[str | None] = mapped_column(String(128))
    agent_drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "reason IN ('solver_not_optimal','solver_infeasible','low_extraction_confidence',"
            "'threshold_near_miss','rate_unavailable','unknown_field_label',"
            "'jurisdiction_ambiguous','deadline_imminent')",
            name="ck_review_reason",
        ),
        CheckConstraint("state IN ('open','claimed','resolved')", name="ck_review_state"),
        CheckConstraint(
            "severity IN ('low','normal','high','blocking')",
            name="ck_review_severity",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('approved','rejected','corrected','deferred')",
            name="ck_review_resolution",
        ),
        # A resolved row must say how it was resolved, and when. Otherwise the queue
        # empties without a record of what an analyst actually decided.
        CheckConstraint(
            "state <> 'resolved' OR (resolution IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_resolved_rows_are_explained",
        ),
        Index("ix_review_queue_open", "tenant_id", "state", "created_at"),
        Index("ix_review_queue_claim", "claim_id"),
    )

"""Tariff schedule and rulings corpus, with vector search.

Two schedules and one rulings body live here:

- **USITC HTS** — the US Harmonized Tariff Schedule, 10 digits.
- **ZATCA Integrated Customs Tariff** (التعرفة الجمركية المتكاملة) — the KSA schedule,
  bilingual, 8 or 12 digits depending on the chapter.
- **CBP CROSS** — US classification rulings, which are what make a classification
  *defensible* rather than merely plausible.

They share one table because the search is the same operation in both jurisdictions, and
because a claim that spans both needs one query surface. `jurisdiction` and `source`
partition them; nothing joins across a jurisdiction boundary.

Embeddings are 1536-dimensional to match the common text-embedding size. The column is
nullable so a schedule can be ingested and searched lexically before the embedding pass
completes — a half-embedded corpus is still useful, and blocking ingest on the model
would make the first load an all-or-nothing operation.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
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
from sqlalchemy.orm import Mapped, mapped_column

from services.api.src.models import Base

# text-embedding-3-small and most open equivalents. Changing this is a migration, not a
# config change, because the index is built against the dimension.
EMBEDDING_DIM = 1536


class TariffLine(Base):
    """One heading, subheading or statistical line of a tariff schedule."""

    __tablename__ = "tariff_lines"

    tariff_line_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    jurisdiction: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(24))
    """usitc_hts | zatca_tariff. Which schedule this line came from."""

    code: Mapped[str] = mapped_column(String(12))
    """Digits only, no dots. 10 for USITC, 8 or 12 for ZATCA."""

    heading: Mapped[str] = mapped_column(String(4))
    hs6: Mapped[str] = mapped_column(String(6))
    """The internationally harmonised portion — the only part comparable across
    jurisdictions, and therefore the only safe join key between the two schedules."""

    description_en: Mapped[str] = mapped_column(Text)
    description_ar: Mapped[str | None] = mapped_column(Text)
    """ZATCA publishes bilingually; USITC does not. Null for US lines."""

    unit_of_quantity: Mapped[str | None] = mapped_column(String(24))
    duty_rate_general: Mapped[str | None] = mapped_column(String(64))
    """Kept as published text, not parsed to a number. Rates are expressed as
    '2.5%', 'Free', '6.5c/kg', and compound forms; parsing them to a float would lose
    the specific and compound cases silently."""

    duty_rate_special: Mapped[str | None] = mapped_column(String(255))
    duty_rate_column2: Mapped[str | None] = mapped_column(String(64))

    ad_valorem_rate: Mapped[float | None] = mapped_column(Numeric(7, 4))
    """Parsed ad valorem percentage where the rate is purely ad valorem. Null where the
    rate is specific or compound — the text column stays authoritative."""

    revision: Mapped[str] = mapped_column(String(32))
    """Schedule edition, e.g. '2026-HTSA-rev3'. A claim is classified against the
    schedule in force on its entry date, so revisions are retained rather than replaced."""

    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("jurisdiction", "source", "code", "revision", name="uq_tariff_line"),
        CheckConstraint("jurisdiction IN ('us','ksa')", name="ck_tariff_jurisdiction"),
        CheckConstraint("source IN ('usitc_hts','zatca_tariff')", name="ck_tariff_source"),
        CheckConstraint("code ~ '^[0-9]{6,12}$'", name="ck_tariff_code_digits"),
        CheckConstraint("left(code, 6) = hs6", name="ck_tariff_hs6_matches_code"),
        CheckConstraint("left(code, 4) = heading", name="ck_tariff_heading_matches_code"),
        # A US line with no English description is unusable; a KSA line may legitimately
        # carry only Arabic before the translation pass.
        CheckConstraint(
            "jurisdiction <> 'us' OR length(description_en) > 0",
            name="ck_us_lines_have_english",
        ),
        Index("ix_tariff_lines_lookup", "jurisdiction", "code"),
        Index("ix_tariff_lines_hs6", "hs6"),
        Index(
            "ix_tariff_lines_desc_en_trgm",
            "description_en",
            postgresql_using="gin",
            postgresql_ops={"description_en": "gin_trgm_ops"},
        ),
    )


class TariffRuling(Base):
    """A classification ruling. CBP CROSS today; ZATCA advance rulings when available.

    Rulings are what turn a classification from an opinion into a position. A claim
    citing a ruling that actually addresses the article survives a desk audit; one citing
    a plausible-sounding heading does not.
    """

    __tablename__ = "tariff_rulings"

    ruling_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    jurisdiction: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(24))
    """cbp_cross | zatca_ruling."""

    ruling_number: Mapped[str] = mapped_column(String(48))
    ruling_date: Mapped[date] = mapped_column(Date)
    classified_code: Mapped[str] = mapped_column(String(12))
    hs6: Mapped[str] = mapped_column(String(6))

    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)

    superseded_by: Mapped[str | None] = mapped_column(String(48))
    """A revoked or modified ruling must never be cited as current. Retained rather than
    deleted, because a claim filed while it was good law relied on it."""

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "ruling_number", name="uq_ruling_number"),
        CheckConstraint("jurisdiction IN ('us','ksa')", name="ck_ruling_jurisdiction"),
        CheckConstraint("source IN ('cbp_cross','zatca_ruling')", name="ck_ruling_source"),
        Index("ix_rulings_code", "jurisdiction", "classified_code"),
        Index("ix_rulings_hs6", "hs6"),
    )


class ClassificationQuery(Base):
    """Every classification lookup, with what it returned.

    Not telemetry. A classification decision that reached a filing must be reconstructable
    years later: which schedule revision was searched, what came back, and what was
    chosen. Retaining only the answer would leave no way to show the question was asked
    properly.
    """

    __tablename__ = "classification_queries"

    query_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False))

    jurisdiction: Mapped[str] = mapped_column(String(8))
    query_text: Mapped[str] = mapped_column(Text)
    revision: Mapped[str | None] = mapped_column(String(32))
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    method: Mapped[str] = mapped_column(String(24))
    """vector | lexical | hybrid. Which path answered, so a weak lexical-only answer is
    distinguishable from a confident vector one after the fact."""

    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("method IN ('vector','lexical','hybrid')", name="ck_query_method"),
        Index("ix_classification_queries_tenant", "tenant_id", "asked_at"),
    )

"""add append-only audit ledger

Revision ID: f7a3c9d2e814
Revises: e5c48b71d90a
Create Date: 2026-09-08

The recordkeeping posture 19 CFR §163 and GCC Common Customs Law Art. 175 ask for, made
enforceable rather than conventional.

`claim_transitions` already recorded state changes append-only *by convention* — nothing
in the application updates it, and nothing stops the application from starting to. This
table is append-only by construction: triggers raise on UPDATE, DELETE and TRUNCATE, so
the prohibition survives a future route, a migration written in a hurry, and a psql
session. TRUNCATE needs its own statement-level trigger — row triggers do not fire on
it, and without one the entire ledger could be emptied by a command the row guards
never see.

The triggers are not the whole mechanism. A superuser can drop them, and a customs audit
is precisely the setting where "the application could not have done it" is a weaker claim
than "the record shows it was not done". So each row also carries `entry_hash`, a SHA-256
over the row's content chained to the previous row's hash within the tenant. Removing or
editing a row leaves every subsequent hash unverifiable, which `mcp-ledger.verify_chain`
reports on.

`claim_id` carries no foreign key. That is deliberate and is the one place this table
departs from the rest of the schema: every other claim-scoped table cascades on delete,
and a cascade here would mean deleting a claim silently deletes the evidence that it
existed. The trade-off is that the column can outlive its claim, which is the correct
direction for an audit record to fail.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from services.api.src.models import LEDGER_GUARD_FUNCTION, LEDGER_GUARD_TRIGGERS

revision = "f7a3c9d2e814"
down_revision = "e5c48b71d90a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_ledger",
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("subject", sa.String(128), nullable=True),
        sa.Column("document_sha256", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('document_ingested','extraction_run','figure_traced',"
            "'claim_persisted','claim_transition','review_opened','review_resolved',"
            "'valuation_override','packet_built')",
            name="ck_ledger_event_type",
        ),
        sa.CheckConstraint("char_length(entry_hash) = 64", name="ck_ledger_entry_hash_length"),
        sa.UniqueConstraint("sequence", name="uq_ledger_sequence"),
    )
    op.create_index("ix_ledger_tenant_sequence", "audit_ledger", ["tenant_id", "sequence"])
    op.create_index("ix_ledger_claim", "audit_ledger", ["claim_id", "sequence"])

    # Imported rather than restated. A migration is normally self-contained, but these
    # statements are also attached to the table's metadata so `create_all` installs them
    # for the integration suite, and two copies of an immutability guarantee is one copy
    # too many — the day they drift, the tests check a property production does not have.
    op.execute(LEDGER_GUARD_FUNCTION)
    for statement in LEDGER_GUARD_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    """Drops the ledger.

    Kept honest rather than kept safe: an environment that rolls this migration back is
    an environment that has decided not to keep the records, and pretending otherwise by
    leaving an orphan table behind would be worse than removing it visibly.
    """
    op.execute("DROP TRIGGER IF EXISTS audit_ledger_no_truncate ON audit_ledger")
    op.execute("DROP TRIGGER IF EXISTS audit_ledger_no_delete ON audit_ledger")
    op.execute("DROP TRIGGER IF EXISTS audit_ledger_no_update ON audit_ledger")
    op.drop_index("ix_ledger_claim", table_name="audit_ledger")
    op.drop_index("ix_ledger_tenant_sequence", table_name="audit_ledger")
    op.drop_table("audit_ledger")
    op.execute("DROP FUNCTION IF EXISTS audit_ledger_is_append_only()")

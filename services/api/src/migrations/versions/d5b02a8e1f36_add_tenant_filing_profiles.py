"""Add tenant filing profiles

Revision ID: d5b02a8e1f36
Revises: a3f79c1e04b8
Create Date: 2026-09-08

Until now the claimant's identity arrived in the body of `POST /packaging/build`. The
caller told the packager what to print on a form addressed to CBP or ZATCA, which is
workable for one tenant whose EIN a developer knows by heart and is the reason two pilot
tenants exist that neither lane can actually file for.

A separate table rather than columns on `tenants`, because the two rows have opposite
lifetimes. A `tenants` row is a tombstone: `audit_ledger.tenant_id` is RESTRICT and the
§163 / GCC Art. 175 retention obligation outlives the commercial relationship by years, so
it must not be deleted. This row holds precisely the identifiers a departing tenant is
entitled to have erased — EIN, CR number, and a bank account — and `ON DELETE CASCADE`
plus a plain `DELETE` here erases them without disturbing the tombstone.

RLS applies. `tenancy.TENANT_PREDICATES` carries `tenant_profiles`, and `install_rls`
runs from the same source the week 11 migration used, so the policy here is the policy
everywhere else. A refund destination account is the most valuable single row in this
schema to somebody who should not have it.

The CHECK constraints are shape only. The application validates the IBAN by ISO 13616
mod-97 before storing it, which is a stronger statement than a regex can make; the
constraint exists so a row inserted by psql or by a fixture cannot be a shape the packager
will fail on later. Neither check proves the account exists or that the IRS issued the
number.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from services.api.src.tenancy import install_rls

revision = "d5b02a8e1f36"
down_revision = "a3f79c1e04b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_profiles",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=False),
        sa.Column("ein", sa.String(length=16), nullable=True),
        sa.Column("broker_code", sa.String(length=3), nullable=True),
        sa.Column("cr_number", sa.String(length=10), nullable=True),
        sa.Column("vat_number", sa.String(length=15), nullable=True),
        sa.Column("iban", sa.String(length=34), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("address_line2", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("city", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("postal_code", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("country", sa.String(length=2), nullable=False, server_default=""),
        sa.Column("contact_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("contact_phone", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.CheckConstraint(
            "ein IS NULL OR ein ~ '^[0-9]{9}([A-Z0-9]{2})?$'", name="ck_profile_ein"
        ),
        sa.CheckConstraint("cr_number IS NULL OR cr_number ~ '^[0-9]{10}$'", name="ck_profile_cr"),
        sa.CheckConstraint(
            "vat_number IS NULL OR vat_number ~ '^[0-9]{15}$'", name="ck_profile_vat"
        ),
        sa.CheckConstraint(
            "broker_code IS NULL OR broker_code ~ '^[A-Z0-9]{3}$'", name="ck_profile_broker"
        ),
        sa.CheckConstraint(
            "iban IS NULL OR iban ~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$'", name="ck_profile_iban"
        ),
        sa.CheckConstraint("country = '' OR country ~ '^[A-Z]{2}$'", name="ck_profile_country"),
    )
    # The same call the week 11 migration made, over the same predicate map. Two copies of
    # a security policy eventually become two different policies.
    install_rls(op.get_bind())


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_profiles_tenant_isolation ON tenant_profiles")
    op.drop_table("tenant_profiles")

"""Add row-level security and the tenant tombstone

Two changes that only make sense together.

**The tombstone.** Week 10 made `audit_ledger.tenant_id` RESTRICT, which is correct — a
retention obligation a `DELETE` satisfies is not a retention obligation — and left
offboarding with no answer at all. The answer is that a departing tenant's rows stay and
stop being reachable: `offboarded_at` is set, and `app_current_tenant()` stops resolving
that tenant, so its documents, claims, ledger and queue leave every scoped query at once.
The CHECK refuses a tombstone with no export behind it, because a tenant marked gone whose
ledger was never exported is the one state this whole mechanism exists to prevent.

**The policies.** Every tenant-scoped table gets RLS enabled and one `FOR ALL` policy
comparing against `app_current_tenant()`. The predicate is NULL when nothing has set
`tenant.id`, so an unscoped connection reads nothing.

What this migration cannot do is make that enforcement real, and it is worth being exact
about why. Policies do not apply to superusers or to roles with BYPASSRLS, and the
`drawbridge` role that owns these tables and runs this migration is both. `FORCE ROW LEVEL
SECURITY` does not help — it binds a table's owner only where the owner is not a superuser.
So this migration installs the mechanism and `tenancy.ensure_app_role` supplies the thing
that makes it bite: an unprivileged `drawbridge_app` role for the services to connect as.
Creating that role here would mean putting its password in version control, so it is done
by `make rls-bootstrap` from the environment instead.

Revision ID: b1d6f2c93a47
Revises: f7a3c9d2e814
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from services.api.src.tenancy import TENANT_PREDICATES, install_rls

revision = "b1d6f2c93a47"
down_revision = "f7a3c9d2e814"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("offboarded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenants", sa.Column("offboard_artifact_key", sa.String(length=512), nullable=True)
    )
    op.add_column("tenants", sa.Column("offboard_signature", sa.String(length=256), nullable=True))
    op.add_column("tenants", sa.Column("offboard_public_key", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "ck_tenant_offboard_is_evidenced",
        "tenants",
        "offboarded_at IS NULL OR "
        "(offboard_artifact_key IS NOT NULL AND offboard_signature IS NOT NULL "
        "AND offboard_public_key IS NOT NULL)",
    )
    # Partial index: the common query is "the tenants that are still live", and an index
    # over the tombstoned minority would be read once a year.
    op.create_index(
        "ix_tenants_live",
        "tenants",
        ["tenant_id"],
        unique=False,
        postgresql_where=sa.text("offboarded_at IS NULL"),
    )

    # Delegated to `tenancy` rather than restated here. The integration suite builds its
    # schema through `create_all`, which installs the same statements via the metadata
    # hook; two copies of a security policy would eventually be two different policies,
    # and the one the tests cover would not be the one deployed.
    install_rls(op.get_bind())


def downgrade() -> None:
    for table in TENANT_PREDICATES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS app_tenant_of_resume_token(text)")
    op.execute("DROP FUNCTION IF EXISTS app_tenant_of_review(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_tenant_of_claim(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant()")

    op.drop_index("ix_tenants_live", table_name="tenants")
    op.drop_constraint("ck_tenant_offboard_is_evidenced", "tenants", type_="check")
    op.drop_column("tenants", "offboard_public_key")
    op.drop_column("tenants", "offboard_signature")
    op.drop_column("tenants", "offboard_artifact_key")
    op.drop_column("tenants", "offboarded_at")

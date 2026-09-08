"""Record the trace a ledger entry was written under.

One nullable column, and the interesting decision is what it is *not*: `trace_id` is not
part of `entry_hash`. The digest covers what the row asserts — the event, its subject, its
actor, the document behind it — and a trace id says where to look for how it happened.

Hashing it would have two costs and no benefit. An artifact exported by
`scripts/tenant_offboard.py` before this migration would stop verifying against a chain
recomputed after it, which is precisely the false positive that makes a tamper-evidence
mechanism worth ignoring. And the same logical event replayed after a failure would hash
differently, so a retry would look like a rewrite.

Nullable because most of the ledger predates the column, and because plenty of legitimate
writes happen outside a traced request: a CLI run, a migration, the offboarding script.

Revision ID: c8e41b7f52d9
Revises: b1d6f2c93a47
Create Date: 2026-09-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e41b7f52d9"
down_revision: str | None = "b1d6f2c93a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_ledger", sa.Column("trace_id", sa.String(length=32), nullable=True))

    # Partial: most rows have no trace and the index exists to answer "what did this
    # trace write", which only asks about the rows that do.
    op.create_index(
        "ix_audit_ledger_trace",
        "audit_ledger",
        ["trace_id"],
        postgresql_where=sa.text("trace_id IS NOT NULL"),
    )

    # The append-only triggers from f7a3c9d2e814 refuse UPDATE on this table, so the
    # column can only ever be written by the INSERT that creates the row. That is the
    # property that keeps it honest: a trace id cannot be attached after the fact.


def downgrade() -> None:
    op.drop_index("ix_audit_ledger_trace", table_name="audit_ledger")
    op.drop_column("audit_ledger", "trace_id")

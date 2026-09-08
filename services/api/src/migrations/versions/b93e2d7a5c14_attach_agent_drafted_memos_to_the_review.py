"""Attach agent-drafted memos to review_queue rows

The memo lives in its own column rather than inside `payload`. `payload` is documented as
carrying the matcher's rejections and solver metadata *verbatim*, and an analyst comparing
a queue row against a re-run of the matcher needs that to stay true. Writing a generated
narrative into it would make the two diverge and quietly break the comparison.

`agent_model` records which model and prompt version drafted the memo. A memo that
influenced a filing is a document an auditor may ask about four years from now, and
"which model wrote this" is not answerable retrospectively unless it was written down at
the time.

Nullable, with no default: a row with no memo is the normal pre-existing state and means
the analyst reads it unaided, exactly as before this column existed.

Revision ID: b93e2d7a5c14
Revises: a7c31f9d4e60
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b93e2d7a5c14"
down_revision: str | Sequence[str] | None = "a7c31f9d4e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_queue",
        sa.Column("agent_memo", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "review_queue",
        sa.Column("agent_model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "review_queue",
        sa.Column("agent_drafted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The drafting worker selects open rows that have no memo yet. Partial, because the
    # rows it must find are a shrinking minority of the table and a full index would be
    # mostly rows the query never wants.
    op.create_index(
        "ix_review_queue_undrafted",
        "review_queue",
        ["tenant_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("agent_memo IS NULL AND state = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_review_queue_undrafted", table_name="review_queue")
    op.drop_column("review_queue", "agent_drafted_at")
    op.drop_column("review_queue", "agent_model")
    op.drop_column("review_queue", "agent_memo")

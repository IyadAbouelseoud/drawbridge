"""widen port_of_entry for named GCC ports

Revision ID: e5c48b71d90a
Revises: b93e2d7a5c14
Create Date: 2026-09-08

`entry_lines.port_of_entry` was sized at 16 characters, which fits a 4-digit CBP port code
with room to spare and does not fit "Jeddah Islamic Port". The column was written when the
US lane was the only one persisting lines; the KSA lane names its ports rather than
numbering them, and week 9 was the first time a GCC line was actually written to the table
rather than only matched in memory.

The failure was a 500 from `/claims/persist` with a
`StringDataRightTruncation` from Postgres — loud, at least, rather than a truncated port
name on a filing. But it means every KSA claim would have been unpersistable, which no
test caught because no test persisted one.

64 rather than 32: ZATCA port names run long, and an Arabic name costs more bytes than its
transliteration. There is no index on the column and no storage cost to the headroom.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5c48b71d90a"
down_revision = "b93e2d7a5c14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "entry_lines",
        "port_of_entry",
        existing_type=sa.String(16),
        type_=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Truncating back to 16 would silently mangle any GCC port name already stored.

    Postgres refuses the narrowing outright when a longer value exists, which is the
    correct behaviour and is left in place rather than papered over with a USING clause.
    """
    op.alter_column(
        "entry_lines",
        "port_of_entry",
        existing_type=sa.String(64),
        type_=sa.String(16),
        existing_nullable=False,
    )

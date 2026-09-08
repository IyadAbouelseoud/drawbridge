"""Widen duty rate columns to what the published schedule actually contains

Revision ID: a3f79c1e04b8
Revises: c8e41b7f52d9
Create Date: 2026-09-08

`duty_rate_general` and `duty_rate_column2` were `varchar(64)`, `duty_rate_special` was
`varchar(255)`. Those widths were set in week 3 against a twenty-four line hand-built
corpus in which every rate read "Free", "2.5%" or "6.5c/kg".

The real 2026 HTSA does not fit. Loading it fails on 105 rows at `general` and 115 at
`column2`, the longest running 439 characters — the sugar lines that recite general note
15, and the tobacco lines carrying an entire "duty (in lieu of any other duty or tax)
equal to the sum of..." formula. 319 rows overflow `special`, which lists every free-trade
partner code inline.

`Text`, not a bigger `varchar`. A published duty rate is prose with no natural bound: the
column was truncating not because 64 was the wrong number but because there is no right
one. Postgres stores both identically, so the wider type costs nothing but the honesty.

Widening is not destructive and the downgrade would be: any row longer than 64 characters
would be truncated on the way back, which is data loss disguised as a schema change. The
downgrade therefore refuses when such a row exists rather than silently cutting it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f79c1e04b8"
down_revision = "c8e41b7f52d9"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("duty_rate_general", 64),
    ("duty_rate_special", 255),
    ("duty_rate_column2", 64),
)


def upgrade() -> None:
    for column, _ in _COLUMNS:
        op.alter_column(
            "tariff_lines",
            column,
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    for column, width in _COLUMNS:
        too_long = (
            op.get_bind()
            .execute(
                sa.text(f"SELECT count(*) FROM tariff_lines WHERE length({column}) > :width"),
                {"width": width},
            )
            .scalar_one()
        )
        if too_long:
            msg = (
                f"{too_long} row(s) in tariff_lines.{column} are longer than {width} "
                "characters; narrowing would truncate published duty rates. Delete the "
                "full-volume corpus first if this downgrade is intended."
            )
            raise RuntimeError(msg)
        op.alter_column(
            "tariff_lines",
            column,
            existing_type=sa.Text(),
            type_=sa.String(length=width),
            existing_nullable=True,
        )

"""Embed the leaf, not the chapter heading

Revision ID: f2b90d47ac13
Revises: d5b02a8e1f36
Create Date: 2026-09-08

`description_en` is the full ancestor chain, root-first, because that is what a person
needs to read: a line whose own text says "Other" means nothing without it. It is the
wrong string to embed, and loading the real schedule is what made that visible.

The published description of 8471.30.01.00 runs 240 characters, of which the first 190
are the chapter heading — shared verbatim by every line under heading 8471. The model
mean-pools over tokens, so the twenty characters distinguishing a portable computer from
a mainframe are averaged into near-irrelevance and every sibling lands at almost the same
point in the space. Against 24 hand-written flat descriptions this was invisible. Against
28,899 published ones the benchmark retrieved 1 of 10 correct lines in its top 10, and the
nearest neighbours for "ruggedised field laptop computer" were five machine-tool
subheadings whose own long headings happened to sit closer.

So a second column, holding the same chain leaf-first and truncated. Not a replacement:
`description_en` is what gets shown to an analyst and printed in a citation, and it must
stay the readable one. Nullable, and `embed_corpus` falls back to `description_en`, which
is correct for ZATCA — its export is one leaf description per row with no hierarchy to
flatten and therefore nothing to dilute.

**Existing embeddings are dropped for US lines.** They were computed over the diluted text
and are not comparable with vectors computed over the new one; leaving them would mean a
corpus whose distances mean two different things depending on when a row was loaded, which
is worse than an unembedded corpus because it still answers. `scripts/embed_corpus.py`
selects rows with a NULL embedding, so nulling them here is also what schedules the
re-embed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2b90d47ac13"
down_revision = "d5b02a8e1f36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tariff_lines", sa.Column("search_text", sa.Text(), nullable=True))
    # US only. ZATCA rows embed their description and are unaffected by the change.
    op.execute(
        "UPDATE tariff_lines SET embedding = NULL WHERE jurisdiction = 'us' "
        "AND embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tariff_lines SET embedding = NULL WHERE jurisdiction = 'us' "
        "AND embedding IS NOT NULL"
    )
    op.drop_column("tariff_lines", "search_text")

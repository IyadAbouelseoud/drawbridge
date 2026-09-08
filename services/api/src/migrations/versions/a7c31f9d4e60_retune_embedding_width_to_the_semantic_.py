"""Retune the embedding width to the semantic model (1536 -> 384)

Week 8 replaced the placeholder hashing vectorizer with a real multilingual
sentence-transformer, which emits 384 dimensions rather than the 1536 the column was
provisioned at.

The existing vectors are dropped rather than converted, because there is no conversion.
A vector written by one model and a vector written by another occupy different spaces;
cosine distance between them is not a worse answer, it is a meaningless one. Any backend
change already requires a full re-embed, so nulling the column here makes explicit what
was true anyway — and `scripts/embed_corpus.py` selects on `embedding IS NULL`, so the
re-embed is exactly the normal pass with nothing new to run.

The HNSW indexes are dropped before the type change and rebuilt after: an HNSW index is
built against a fixed dimension and cannot survive an ALTER of the column it indexes.

Revision ID: a7c31f9d4e60
Revises: d41e3e45b5f1
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a7c31f9d4e60"
down_revision: str | Sequence[str] | None = "d41e3e45b5f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    ("tariff_lines", "ix_tariff_lines_embedding"),
    ("tariff_rulings", "ix_tariff_rulings_embedding"),
)


def _resize(width: int) -> None:
    for table, index in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS {index}")
        # Null first, then alter. Altering a populated vector column to a different width
        # is an error, not a truncation, so the data has to go regardless.
        op.execute(f"UPDATE {table} SET embedding = NULL WHERE embedding IS NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({width})")
        op.execute(f"CREATE INDEX {index} ON {table} USING hnsw (embedding vector_cosine_ops)")


def upgrade() -> None:
    _resize(384)


def downgrade() -> None:
    _resize(1536)

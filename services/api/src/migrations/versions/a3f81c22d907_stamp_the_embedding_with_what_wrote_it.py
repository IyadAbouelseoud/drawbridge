"""Stamp the embedding with the backend and the text convention that wrote it.

Revision ID: a3f81c22d907
Revises: f2b90d47ac13

`services/classifier/src/embeddings.py` has said since week 8 that every backend records
`model_id` on the rows it writes and that mixing backends is "a data error the ingest can
detect". No column existed. Nothing recorded it, nothing detected anything, and the
sentence described an intention rather than a mechanism.

Week 15 is the bill. `scripts/embed_corpus.py` embedded `f"{code} {body}"`, so every one
of the 28,899 US document vectors began with a ten-digit tariff code that no analyst query
ever contains — the document side and the query side were never in the same distribution.
The stored vector for a line reproduces at cosine 1.000000 against `code + " " + text` and
0.950 against the text alone. It presented as "the multilingual model is too thin at
29,000 near-identical legal phrases" and survived two weeks of being measured as a model
problem, because there was no way to ask a row what convention had produced it.

The stamp names the **text convention** as well as the model. Two corpora embedded by the
same model from different text are as incomparable as two models, and the failure that
actually happened was the second kind — so a column holding only `model_id` would have
recorded `fastembed:...MiniLM-L12-v2` for both and caught nothing.

**This migration is additive. It deletes no vectors.** The obvious alternative was to null
every embedding, since the existing ones are unreachable by any query the application
issues. It was rejected for an operational reason rather than a sentimental one: nulling
them takes classification dark for the length of a full re-embed, whereas
`embed_corpus.py --reembed` now selects rows whose stamp is not the current convention and
overwrites each in place. The corpus converges instead of emptying, every row is either
the old vector or the new one at all times, and an interrupted run leaves a mixture that
the stamp makes visible and the next run finishes. Rows carrying the retired convention
are left with a NULL stamp, which is precisely what `--reembed` looks for.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f81c22d907"
down_revision = "f2b90d47ac13"
branch_labels = None
depends_on = None

_TABLES = ("tariff_lines", "tariff_rulings")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("embedding_model_id", sa.String(length=128), nullable=True))
        # Partial. The question anyone asks of this column is "which conventions are
        # present in the embedded corpus", which is a scan of the embedded rows and never
        # of the unembedded ones — and on a corpus mid-re-embed it is the query that says
        # how far the run has got.
        op.create_index(
            f"ix_{table}_embedding_model",
            table,
            ["embedding_model_id"],
            postgresql_where=sa.text("embedding IS NOT NULL"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_embedding_model", table_name=table)
        op.drop_column(table, "embedding_model_id")

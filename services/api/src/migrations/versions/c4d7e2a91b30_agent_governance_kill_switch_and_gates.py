"""Agent governance: the kill switch's history, two new gates, and the drafter's lookup.

Everything the v1.1.0 security pass needed from the schema, in one additive migration.
(The drafter's partial index, `ix_review_queue_undrafted`, is *not* here: b93e2d7a5c14
created it in week 8. What was missing was the model's copy of it, so every database the
integration suite built with `create_all` lacked the index production had.)

- `control_events` — append-only, global. The kill switch's state is the latest engage or
  release row per scope, so there is no flag to flip and no moment at which the switch and
  its record can disagree. Token issuance is recorded here too, because a fifteen-minute
  credential nobody logged is a credential nobody can trace.
- `ck_review_reason` gains `high_value_approval`, `suspected_prompt_injection` and
  `pipeline_failure`.
- `ck_ledger_event_type` gains the four events the audit trail was missing.
- `app_tenants_with_undrafted_reviews()` and `app_tenants_with_open_reviews()` — the
  cross-tenant questions the drafter and the review dispatcher ask, answered with ids only,
  so both can run as the app role instead of reading nothing.

**The downgrade refuses on a database that has used the new reasons.** Narrowing
`ck_review_reason` back fails while any `pipeline_failure`, `high_value_approval` or
`suspected_prompt_injection` row exists, and that is deliberate: those rows are the record
of an exception somebody raised, and a downgrade that deleted them to fit the old constraint
would be erasing audit evidence to satisfy a schema.

Grants to `drawbridge_app` are applied here when the role exists, because a table the app
role cannot read fails in a way that looks exactly like an engaged kill switch — every
mutation refused — and the fail-closed read would be right to do so.

Revision ID: c4d7e2a91b30
Revises: a3f81c22d907
Create Date: 2026-09-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d7e2a91b30"
down_revision: str | None = "a3f81c22d907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REASONS_BEFORE = (
    "'solver_not_optimal','solver_infeasible','low_extraction_confidence',"
    "'threshold_near_miss','rate_unavailable','unknown_field_label',"
    "'jurisdiction_ambiguous','deadline_imminent'"
)
_REASONS_AFTER = (
    _REASONS_BEFORE + ",'high_value_approval','suspected_prompt_injection','pipeline_failure'"
)

_EVENTS_BEFORE = (
    "'document_ingested','extraction_run','figure_traced',"
    "'claim_persisted','claim_transition','review_opened','review_resolved',"
    "'valuation_override','packet_built'"
)
_EVENTS_AFTER = (
    _EVENTS_BEFORE
    + ",'review_reopened','agent_memo_drafted','agent_memo_withheld','prompt_injection_suspected'"
)

_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION control_events_is_append_only()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'control_events is append-only: % refused', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

_TENANTS_FUNCTION = """
CREATE OR REPLACE FUNCTION app_tenants_with_undrafted_reviews() RETURNS SETOF uuid AS $$
    SELECT DISTINCT r.tenant_id
      FROM public.review_queue r
      JOIN public.tenants t ON t.tenant_id = r.tenant_id
     WHERE r.state = 'open' AND r.agent_memo IS NULL AND r.agent_model IS NULL
       AND t.offboarded_at IS NULL
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""


_OPEN_TENANTS_FUNCTION = """
CREATE OR REPLACE FUNCTION app_tenants_with_open_reviews() RETURNS SETOF uuid AS $$
    SELECT DISTINCT r.tenant_id
      FROM public.review_queue r
      JOIN public.tenants t ON t.tenant_id = r.tenant_id
     WHERE r.state = 'open' AND t.offboarded_at IS NULL
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
"""


def upgrade() -> None:
    op.create_table(
        "control_events",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("scope_value", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('kill_switch_engaged','kill_switch_released','token_issued',"
            "'circuit_breaker_tripped')",
            name="ck_control_event_type",
        ),
        sa.CheckConstraint(
            "scope_kind IN ('global','tenant','principal')", name="ck_control_scope_kind"
        ),
        sa.CheckConstraint(
            "actor_kind IN ('human','machine','local')", name="ck_control_actor_kind"
        ),
        sa.CheckConstraint("char_length(reason) >= 20", name="ck_control_reason_stated"),
    )
    # Partial: the state query reads engage/release rows only, and the table also takes a
    # `token_issued` row per credential exchange. See the model for the measurement.
    op.create_index(
        "ix_control_events_switch",
        "control_events",
        ["scope_kind", "scope_value", "event_id"],
        postgresql_where=sa.text("event_type IN ('kill_switch_engaged', 'kill_switch_released')"),
    )
    op.execute(_GUARD_FUNCTION)
    for verb, when in (("update", "ROW"), ("delete", "ROW"), ("truncate", "STATEMENT")):
        op.execute(
            f"CREATE TRIGGER control_events_no_{verb} BEFORE {verb.upper()} ON control_events "
            f"FOR EACH {when} EXECUTE FUNCTION control_events_is_append_only()"
        )

    op.drop_constraint("ck_review_reason", "review_queue", type_="check")
    op.create_check_constraint("ck_review_reason", "review_queue", f"reason IN ({_REASONS_AFTER})")

    op.drop_constraint("ck_ledger_event_type", "audit_ledger", type_="check")
    op.create_check_constraint(
        "ck_ledger_event_type", "audit_ledger", f"event_type IN ({_EVENTS_AFTER})"
    )

    op.execute(_TENANTS_FUNCTION)
    op.execute(_OPEN_TENANTS_FUNCTION)

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'drawbridge_app') THEN
                GRANT SELECT, INSERT ON control_events TO drawbridge_app;
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO drawbridge_app;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_tenants_with_open_reviews()")
    op.execute("DROP FUNCTION IF EXISTS app_tenants_with_undrafted_reviews()")

    op.drop_constraint("ck_ledger_event_type", "audit_ledger", type_="check")
    op.create_check_constraint(
        "ck_ledger_event_type", "audit_ledger", f"event_type IN ({_EVENTS_BEFORE})"
    )
    op.drop_constraint("ck_review_reason", "review_queue", type_="check")
    op.create_check_constraint("ck_review_reason", "review_queue", f"reason IN ({_REASONS_BEFORE})")

    op.drop_table("control_events")
    op.execute("DROP FUNCTION IF EXISTS control_events_is_append_only()")

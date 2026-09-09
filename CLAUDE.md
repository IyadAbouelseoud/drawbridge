# DRAWBRIDGE

Autonomous customs duty recovery & trade remediation.

## Git attribution rule (non-negotiable)

Never add Co-Authored-By lines to commits. Git history must remain solely under the user's name.

No `Co-Authored-By:`, no `Generated with`, no tool attribution of any kind in commit
messages, PR bodies, or tags.

## Persistent context

Architecture lives in `docs/ARCHITECTURE.md`. The week-by-week roadmap is closed and
archived at `docs/ROADMAP_ARCHIVE.md` — a record, not a plan. Read both at the start of
any session before making structural decisions.

**The repository is feature-frozen at v1.0.0.** Do not open new work against the archive's
closed items without being asked to.

## Conventions

- Python 3.12, managed by `uv`. Never `pip install` into the system interpreter.
- All shared data contracts live in `packages/schemas` — single source of truth.
- Every figure in a claim must be traceable to a source-document span. The LLM writes
  narratives and judgment calls; it never originates a number.
- n8n holds no business state. Claim state is a Postgres state machine.
- `mypy --strict` and `ruff` are gating. `pytest` fixtures in `tests/golden/` are
  known-answer claims and must reproduce to the cent.

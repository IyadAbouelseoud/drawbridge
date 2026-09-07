# DRAWBRIDGE — 14-Week Roadmap

> Persistent context. Read with `ARCHITECTURE.md` at the start of any session.

## Status

| | |
|---|---|
| Current week | 1 complete → 2 |
| Current milestone | `mcp-docs` + extraction service |
| Week 1 exit gate | **PASSED** — 10/10 containers healthy, MCP handshakes verified |

---

## Milestones

| Wk | Milestone | Exit criterion |
|---|---|---|
| 1 | Scaffold, compose stack, CI, schemas package | `docker compose up` — all services green |
| 2–3 | `mcp-docs` + extraction service | 7501 + commercial invoice to typed lines, >=95% field accuracy with provenance spans |
| 3–4 | `mcp-hts`: USITC schedule ingest, CROSS corpus + pgvector | Classification query returns cited rulings |
| 4–6 | **Matcher** — §1313(j)(1)/(j)(2) rules, CP-SAT allocation | Golden fixture set: known-answer claims reproduce to the cent |
| 6–7 | `mcp-claims` + rules engine: windows, deadlines, routing | Refund quantification with full derivation trail |
| 7–8 | Agent layer: exception handling, interchangeability narratives | Analyst in Claude Code can close an exception via MCP tools |
| 8–9 | n8n orchestration + HITL approval gates | End-to-end run with no manual intervention outside gates |
| 9–10 | `packager`: 7551/7552/PSC/1520(d) output | Packet accepted by a licensed broker for filing |
| 10–11 | `mcp-ledger` + §163 recordkeeping posture | Any figure traceable to source span, four years later |
| 11–12 | Multi-tenant hardening: RLS, Authentik, secrets, OTel | Second tenant onboarded with zero code change |
| 12–13 | Pilot: one real importer, backward-looking claim | Filed claim, refund in motion |
| 14 | Broker white-label packaging + on-prem compose | Reproducible `.onprem.yml` deploy |

---

## Week 1 task breakdown

- [x] Anonymity lock: `.claude/settings.json` (repo + global), `CLAUDE.md`
- [x] Persistent docs: `ARCHITECTURE.md`, `ROADMAP.md`
- [x] Toolchain: `uv`, `pre-commit`, `make`, `psql`
- [x] `git init`, GitHub repo created and pushed
- [x] Repository scaffold: `services/`, `mcp_servers/`, `packages/`, `n8n/`, `tests/`, `infra/`
- [x] `packages/schemas` — shared Pydantic contracts
- [x] `docker-compose.yml` — Postgres/pgvector, Redis, MinIO, n8n, API, MCP placeholders
- [x] CI: ruff + mypy + pytest on push
- [x] `docker compose up` verified green — Week 1 exit gate PASSED

## Week 2 entry checklist

1. Real `Dockerfile` per service (currently a shared base image).
2. Alembic baseline migration for the claim state machine.
3. `mcp-docs`: MinIO-backed document store, upload + span-addressable retrieval.
4. Extraction: pdfplumber/PyMuPDF native path before any OCR path.
5. First golden fixture: one synthetic 7501 with a hand-verified answer key.

---

## Sequencing rationale

Extraction (wk 2–3) precedes matching (wk 4–6) because the matcher's input contract is
defined by what extraction can actually guarantee — building the matcher first would fix
a contract against imagined data.

The agent layer (wk 7–8) lands *after* the deterministic core, so the LLM is scoped to
exactly the residue the rules engine cannot decide. Building it earlier invites the model
to absorb work that belongs in testable code.

The pilot (wk 12–13) targets a backward-looking claim rather than live flow: a 5-year
lookback has a known answer set and no operational dependency on the customer's current
quarter.

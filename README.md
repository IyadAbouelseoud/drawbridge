# Drawbridge

Autonomous customs duty recovery and trade remediation.

US Customs refunds 99% of duties, taxes and fees on imported goods later exported or
destroyed (19 U.S.C. §1313), reaching back five years. An estimated $2–3B/year goes
unclaimed because the reconciliation work is brutal. Drawbridge does the reconciliation
and produces a filing-ready packet.

**Drawbridge does not file with CBP.** Filing requires a licensed customs broker and a
Power of Attorney. We produce the packet; the client's broker files it. That constraint
is load-bearing — see `docs/ARCHITECTURE.md` §5.

## Quick start

```bash
cp .env.example .env          # set DRAWBRIDGE_ANTHROPIC_API_KEY
docker compose up -d --build
curl localhost:8000/ready
```

| Surface | URL |
|---|---|
| API | http://localhost:8000/docs |
| n8n | http://localhost:5678 |
| MinIO console | http://localhost:9001 |
| MCP servers | :8101 ace · :8102 hts · :8103 docs · :8104 claims · :8105 ledger |

## Local development

```bash
uv sync --extra dev
uv run pre-commit install
make check          # ruff + mypy --strict + pytest
```

## Layout

| Path | Role |
|---|---|
| `packages/schemas` | Shared Pydantic contracts. Single source of truth |
| `services/` | api · ingest · extraction · classifier · matcher · rules · packager · agent |
| `mcp/` | Five typed MCP servers — the tool boundary |
| `n8n/workflows` | Orchestration, version-controlled as JSON |
| `tests/golden` | Known-answer claims that must reproduce to the cent |
| `docs/` | `ARCHITECTURE.md`, `ROADMAP.md` — persistent project context |

## Invariants

- Every figure in a claim traces to a source-document span. The LLM writes narratives and
  judgment calls; it never originates a number.
- Money is `Decimal`, never `float`.
- n8n holds no business state. Claim state is a Postgres state machine.
- Document objects are immutable; a correction writes a new object.

# Drawbridge

Autonomous customs duty recovery and trade remediation, across two jurisdictions.

Importers overpay customs duty and mostly do not get it back, because the reconciliation
that proves a refund is owed is brutal: matching import entries to exports line by line,
tracing every figure to a source document, and doing it inside a statutory window. That
work is what Drawbridge automates, and it stops at the point where a licence is required.

**Drawbridge does not file.** US drawback is transmitted through ABI by a licensed customs
broker under a Power of Attorney; a GCC refund is lodged by the establishment of record.
We produce the packet and the audit trail; the client's broker files it. That constraint
shapes the whole design — see `docs/ARCHITECTURE.md` §5.

---

## Two jurisdictions, one pipeline

The same documents, matcher and packager serve both lanes. What differs is the statute,
and the rules engine holds the difference rather than the pipeline branching around it.

| | **United States** | **Saudi Arabia / GCC** |
|---|---|---|
| Statute | 19 U.S.C. §1313 drawback | GCC Common Customs Law Art. 16 |
| Look-back | 5 years from import to claim | **Art. 174** — 3 years from duty payment, absolute |
| Windows | 5 years import→export, 5 years export→claim | 1 year duty→re-export, then 6 months re-export→claim |
| Minimum | none | USD 5,000 per re-export (Art. 16 §2) |
| Refund rate | **99%** — §1313 retains 1% | **100%** — Art. 16 §6 takes no haircut |
| Recovers | duty, MPF, HMF, §301 remedies | duty and customs fees |
| Output | CBP 7551 / 7552, or a PSC or §1520(d) payload | ZATCA refund payload, *Bayan* references |
| Also | Post Summary Correction, §1520(d) FTA claims | — |

Art. 174 is a **bar, not a preference**. A duty payment more than three years before the
filing date produces no claim at any amount, and the rules engine says so in those words
rather than returning a smaller number. `scripts/pilot_run.py` asserts exactly that: the
time-barred corpus must refund `0.00` and every line must be rejected naming the article.

Both lanes reproduce **to the cent**. `tests/golden/` holds known-answer claims, and the
pilot asserts the refund equals a figure computed from the jurisdiction's own profile —
not merely that it is non-zero, which is an assertion a wrong number passes.

---

## The four properties that make it auditable

### Every figure traces to a document span

The LLM writes narratives and judgment calls. It never originates a number. Each amount on
a packet carries a provenance box — document, page, bounding box — and
`assert_not_evidence` refuses to act on a figure whose box traces to a fixture rather than
to a scan. A claim is defensible because the arithmetic can be walked backwards, not
because a model was confident.

### The tenant boundary is in the database

Row-level security on **11 tables**, enforced by Postgres against `app_current_tenant()`.
The services connect as `drawbridge_app` — no `SUPERUSER`, no `BYPASSRLS` — so the policies
apply to them. The owner role, which policies do not apply to, is a separate DSN used only
by migrations and `scripts/rls_bootstrap.py`. The tenant comes from the **verified token**,
not from the request body: before week 12 a caller could name their own tenant, which was
isolation from a caller who filled the form in honestly.

### The ledger is append-only and hash-chained

`audit_ledger` records every state transition with the trace id of the request that caused
it. Rows cannot be updated or deleted — a database trigger refuses, not an ORM convention —
and each row carries the hash of its predecessor, so a removed row breaks the chain that
`verify_chain` walks. `tenant_id` is `RESTRICT`: a tenant row is a tombstone that outlives
the commercial relationship, because §163 and GCC Art. 175 retention run for years after it
ends. What a departing tenant *is* entitled to have erased lives in `tenant_profiles` and
cascades.

### Traces join across every process

OpenTelemetry from the API through the five MCP servers. The SDK propagates W3C context
through the JSON-RPC `_meta` (SEP-414), so an analyst tool call lands under the parent API
span; `tests/integration/test_trace_propagation.py` pins that, because a property nobody
wrote is a property nobody notices losing. Spans go to Jaeger and are disposable — the
trace id that has to survive four years is in `audit_ledger`, so recordkeeping does not
depend on a collector being up.

---

## Orchestration, and what holds state

n8n fires the transitions. **It holds no business state at all**: claim state is a Postgres
state machine, and the workflows are version-controlled JSON in `n8n/workflows` that call
`mcp-claims` over HTTP with a service token. A workflow can be deleted and re-imported
without a claim noticing. The five MCP servers are the tool boundary — typed, per-domain,
and the only way the agent layer reaches data:

| | |
|---|---|
| `mcp-ace` | ACE / entry summary data |
| `mcp-hts` | tariff classification and CROSS rulings |
| `mcp-docs` | document store and extraction |
| `mcp-claims` | the claim state machine |
| `mcp-ledger` | the audit ledger |

---

## Running it

### Development

```sh
make secrets-init                 # generate .secrets.json (gitignored)
make rls-bootstrap                # create the unprivileged app role
make up                           # the whole stack
curl localhost:8000/ready
```

| Surface | URL |
|---|---|
| API | http://localhost:8000/docs |
| n8n | http://localhost:5678 |
| Jaeger | http://localhost:16686 |
| MinIO console | http://localhost:9001 |
| MCP servers | :8101 ace · :8102 hts · :8103 docs · :8104 claims · :8105 ledger |

Optional profiles, both of which replace a development shortcut with the real thing:

```sh
make vault-up                     # secrets from HashiCorp Vault over AppRole, not a file
make identity-up TENANT=<uuid>    # Authentik: OIDC provider, tenant claim, RS256 round trip
```

### On premises

`docker-compose.onprem.yml` is a **standalone** deployment, not an overlay — an overlay
inherits every bind mount and published port from the development file, and forgetting one
`-f` deploys development under a production name.

```sh
cp .env.onprem.example .env.onprem     # fill in; see secrets/README.md for the six files
docker compose -f docker-compose.onprem.yml --env-file .env.onprem up -d
```

One container binds a port. The data plane sits on an `internal: true` network with no
default gateway, so Postgres, Redis and MinIO cannot originate outbound traffic at all.
Secrets come from Vault with **no file to fall back to**; identity is Authentik with no
shared-secret fallback, so nothing inside the deployment can mint a token. Migrations run
as a job the API waits on. Every container drops all capabilities and runs read-only where
the image allows it.

**White label is a compliance surface, not a logo.** The preparer notice on a CBP form is a
representation to a customs authority. A licensed broker running this prepares filings
under their own licence, so our *"is not a customs broker and does not transmit to CBP"*
disclaimer is false on their form — the notice is composed from what is true of the
deployer, and a deployment claiming a broker licence must supply the filer code that makes
the claim checkable or it is refused. See `services/packager/src/branding.py`.

### Local development

```sh
uv sync --extra dev
uv run pre-commit install
make check                        # ruff + mypy --strict + pytest
make pilot-run                    # both corpora end to end against the deployed API
```

---

## Layout

| Path | Role |
|---|---|
| `packages/schemas` | Shared Pydantic contracts. Single source of truth |
| `services/` | api · ingest · extraction · classifier · matcher · rules · packager · agent |
| `mcp_servers/` | Five typed MCP servers — the tool boundary |
| `infra/` | Vault and Authentik bootstrap, Caddy, Dockerfiles, Postgres init |
| `n8n/workflows` | Orchestration, version-controlled as JSON |
| `scripts/` | Ingest, embedding, token minting, offboarding, the pilot |
| `tests/golden` | Known-answer claims that must reproduce to the cent |
| `docs/` | `ARCHITECTURE.md`, `ROADMAP.md` — persistent project context |

## Invariants

- Every figure in a claim traces to a source-document span. The LLM writes narratives and
  judgment calls; it never originates a number.
- Money is `Decimal`, never `float`.
- n8n holds no business state. Claim state is a Postgres state machine.
- Document objects are immutable; a correction writes a new object.
- A corpus is loaded from a downloaded snapshot, never from a live endpoint — a
  classification that reached a filing must be reproducible after the publisher moves the
  URL.

## What this does not do yet

Stated here rather than discovered later. The full list, with the reasoning, is in
`docs/ROADMAP.md`.

- **Classify reliably.** Against the full 28,899-line HTSA the benchmark retrieves 5 of 10
  correct subheadings at six digits. That is a measured improvement on 1 of 10 and it is
  not a working classifier.
- **Carry the ruling corpus.** CBP publishes no bulk export; `scripts/ingest_cross.py`
  draws a term-sampled ~120 rulings. A sample is not CROSS.
- **Back anything up.** The on-prem volume is not a backup, and the retention job that
  would run `verify_chain` on a schedule is not written.
- **File anything.** Both pilot corpora are fiction, every figure carries a
  `pilot-fixture` provenance box, and `assert_not_evidence` refuses to act on one.

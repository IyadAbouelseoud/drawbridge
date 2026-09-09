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

Classification is two-stage as of week 16: pgvector narrows 28,899 lines to 50 in about
20 ms, then a multilingual cross-encoder re-scores those 50 and its confidence is blended
70/30 against the retrieval score. The blend is the part that matters — taking the
cross-encoder's order outright is no better than retrieval alone, because it demotes as
many answers as it rescues. `make calibrate-rerank` prints one stage beside two.

```
9/10 retrieved in the top 10   ·   5/10 at rank 1
one stage was 6/10 in the top 10 and 3/10 at rank 1
```

The workflows are imported and run:

```
POST /webhook/drawbridge/ingest
  documents/batch 201 · extraction 200 · classification 200 · matching 200
  triage 200 · claims/persist 201 · claims/transition 200
  packaging/build 200 · claims/transition 200
{"state":"packaged","refund":"25092.14","transmittable":true,
 "artifacts":[{"filename":"cbp7551-....pdf","bytes":13345}]}
```

`make n8n-import` imports, activates and restarts — all three, because
`n8n import:workflow` deactivates what it imports and n8n reads neither until it restarts.
That, and six other defects, is what running these files for the first time found; they had
been committed and validated as JSON since week 9. `docs/ARCHITECTURE.md` §20.6 lists them.

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
make vault-agent-up               # the infra path vault-agent renders Postgres/MinIO/n8n from
make calibrate-rerank             # the labelled set, one stage beside two
make restore-drill                # newest backup into a scratch database, then dropped
make identity-up TENANT=<uuid>    # Authentik: OIDC provider, tenant claim, RS256 round trip
make n8n-import                   # import the workflows, activate them, restart n8n
```

### On premises

`docker-compose.onprem.yml` is a **standalone** deployment, not an overlay — an overlay
inherits every bind mount and published port from the development file, and forgetting one
`-f` deploys development under a production name.

```sh
make images VERSION=1.2.3               # the seven images the deployment file names
cp .env.onprem.example .env.onprem      # fill in; nothing goes in ./secrets any more
docker compose -f docker-compose.onprem.yml --env-file .env.onprem up -d
```

One container binds a port. The data plane sits on an `internal: true` network with no
default gateway, so Postgres, Redis and MinIO cannot originate outbound traffic at all.
Secrets come from Vault with **no file to fall back to**; identity is Authentik with no
shared-secret fallback, so nothing inside the deployment can mint a token. Migrations run
as a job the API waits on. Every container drops all capabilities and runs read-only where
the image allows it. Every third-party image is pinned to a SHA-256 digest, so a `pull`
either fetches identical bytes or fails; `make pin-check` fails the build if a tag has been
repointed under an unchanged version number.

**No credential is on the host filesystem.** Postgres, MinIO and n8n read passwords from a
file and cannot call a manager, so until week 15 six plaintext files sat next to the compose
file. A `vault-agent` sidecar now renders them into tmpfs from `secret/drawbridge-infra`,
under an AppRole that is denied both the application's Vault path and `list` on the mount —
the API has no business being able to read the database owner's password, and it could not
before the move. `secrets/README.md` has the detail.

**Backups are immutable, and that is expensive on purpose.** `scripts/retention.py` takes a
nightly `pg_dump` into MinIO under S3 object lock in COMPLIANCE mode and recomputes every
tenant's ledger hash chain hourly. COMPLIANCE and not GOVERNANCE, because GOVERNANCE can be
lifted by whoever holds `s3:BypassGovernanceRetention` — on a single-tenant on-prem MinIO,
the operator, who is the party a records dispute is about. The consequence is that nothing
prunes: storage grows for the length of the obligation, five years by default. Verified
against a live MinIO, root credentials cannot delete or shorten a written backup.

One nuance worth knowing before an incident: object lock protects the bytes, not the
listing. `DeleteObject` still succeeds by writing a delete marker, and the protected version
survives underneath but disappears from an ordinary `ls`. `retention.py catalogue`
enumerates versions and names anything a delete marker is masking.

**And the backups have now been restored.** `retention.py restore` downloads the newest
object, checks it against the digest recorded when it was written, restores into a scratch
database it refuses to point at production, and recomputes every tenant's ledger hash chain
inside the restored copy — because row counts prove `pg_restore` moved data and only the
chain proves it is the data that went in. It runs weekly on the schedule loop rather than
when somebody remembers. Measured: 28,908 tariff lines and 280 ledger entries back in **15
seconds**, `pg_restore` clean. Its first run failed on a masked password, which is the only
kind of defect this repository has been finding lately.

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
| `infra/` | Vault + Authentik bootstrap, the vault-agent sidecar, image pinning, Caddy, Dockerfiles |
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

- **Classify reliably.** Week 16 took it from 6 of 10 to **9 of 10** correct subheadings
  at six digits against the full 28,899-line HTSA, by adding a cross-encoder over the fifty
  nearest candidates. That is real movement and it is still ten queries — a demonstration,
  not a validation, and 5 of 10 at rank one is not a classifier an analyst can stop reading.
  Two queries fail at any retrieval depth because 596 leaf lines lost their ancestor chain
  during ingest, so 8471.41 — what a desktop PC classifies under — carries text that never
  says it is a computer. A larger labelled set and that ingest fix are week 17.
- **Carry the ruling corpus.** CBP publishes no bulk export; `scripts/ingest_cross.py`
  draws a term-sampled ~120 rulings. A sample is not CROSS.
- **Fail a pipeline run.** Every n8n HTTP node sets `neverError: true`, so a 500 from
  `/claims/persist` becomes `{data: "Internal Server Error"}`, the run continues through
  packaging, returns HTTP 200 and records `success`. Understood, reproduced, and not yet
  fixed — it is a structural change to a 22-node graph and it has been the first item on
  the entry checklist for two weeks running, which is the argument for doing it next.
- **Classify a whole entry interactively.** Reranking costs ~2.2 s a line, so a 200-line
  entry is a seven-minute request. It is opt-in for exactly that reason; batching it is
  week 17.
- **File anything.** Both pilot corpora are fiction, every figure carries a
  `pilot-fixture` provenance box, and `assert_not_evidence` refuses to act on one.

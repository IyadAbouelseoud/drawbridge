# DRAWBRIDGE — Architecture

> Persistent context. Read this file and `ROADMAP.md` at the start of any session
> before making structural decisions.

---

## 1. What this is

**Autonomous duty drawback + tariff misclassification recovery for mid-market
importers/exporters.**

US Customs refunds **99% of duties, taxes and fees** on imported goods that are
subsequently exported or destroyed (19 U.S.C. §1313). Claims reach back **5 years**.
Parallel recovery lanes:

| Lane | Statute | Window | Cycle time |
|---|---|---|---|
| Drawback (unused / manufacturing) | 19 U.S.C. §1313(j)(1), §1313(j)(2), §1313(a)/(b) | 5-yr import lookback, 3-yr filing deadline | 6–18 months |
| Post Summary Correction (misclassification, valuation) | 19 CFR §141.11 | 300 days from entry | 60–90 days |
| Retroactive FTA claim | 19 U.S.C. §1520(d) | 1 year from import | 60–90 days |

Industry estimates put **$2–3B/year of eligible drawback unclaimed** — not from lack of
appetite, but because claiming requires line-item reconciliation of import entries to
export shipments across ERP, broker, forwarder and CBP data, under substitution rules
pivoting on 8-digit HTS identity, producing a packet that must survive a CBP desk audit.
Roughly 200 forensic hours per claim cycle.

Mid-market importers ($5M–$50M annual duty spend) are too small for Charter Brokerage or
Alliance Drawback to court, too large to do it by hand. They leave the money.

**Why now:** Section 301, IEEPA and reciprocal tariff layers inflated effective duty rates
3–5x since 2024. The same trade flow that yielded a $200k claim in 2023 yields ~$900k
today. The unclaimed pile exploded; the labor cost of claiming did not move.

---

## 2. Value proposition

> **We find money the customer already spent and get it wired back.
> They pay us a share of it.**

No behavior change, no adoption curve, no productivity hand-waving. The deliverable is a
Treasury disbursement.

### Unit economics

| | |
|---|---|
| Pricing | Contingency, 18–25% of recovered duty. Zero-risk for buyer, collapses the sales cycle |
| Reference customer | $8M/yr duty spend, 12% export/destruction rate, ~$950k duty base, ~$940k refundable, **~$188k/yr revenue at 20%** |
| Retroactive load | First engagement claims 5 years back — a single onboarding can be a $1M+ event |
| Cost to serve | ~$3–6k/yr compute + LLM per tenant, **>95% gross margin** |
| Recurrence | Every quarter of exports creates new claims. Annuity revenue |
| Fast-cash lane | PSC and §1520(d) settle in 60–90 days vs. drawback 6–18 months, proving the engine early |
| Second market | White-label the engine to customs brokers and trade consultancies currently staffing this with offshore data entry. SaaS pricing, no contingency |

### Why the opening is untapped

- Incumbents are 40-year-old brokerages running Excel and offshore labor. No agentic product exists.
- Trade-tech capital went to visibility (project44, Flexport) and screening (Descartes). Nobody attacked **recovery**.
- The domain barrier (drawback law) filters out generic AI builders; the AI barrier filters out trade people. The intersection is empty.

---

## 3. The pipeline (what the agent actually does)

Eight stages. Not a chatbot.

1. **Ingest** — ACE entry-summary line detail (client-exported, never scraped), CBP 7501s,
   commercial invoices, packing lists, BOLs/AWBs, proofs of export, ERP SKU master, BOMs.
2. **Extract** — structured line records from mixed scanned/native PDFs and EDI 350/309,
   with per-field confidence and **provenance spans** back to source coordinates.
3. **Classify and audit** — reconcile declared HTS against the USITC schedule and CBP CROSS
   rulings. Flag misclassification, valuation and country-of-origin exposure in **both**
   directions (refund *and* liability — never hide the liability).
4. **Match** — the core engine. Pair import lines to export lines under direct-identity or
   substitution (8-digit HTS) rules, respecting 5-year import windows, 3-year filing
   deadlines, unused vs. manufacturing drawback, and per-entry duty apportionment.
   Combinatorial optimization, not lookup.
5. **Prove** — assemble the evidentiary packet: interchangeability narrative, chain of
   custody, destruction certificates, ruling citations.
6. **Quantify** — refund per claim, per entry line, with an audit-defensible derivation trail.
7. **Package** — filing-ready CBP 7551/7552 set plus PSC and §1520(d) submissions, handed to
   the licensed filer.
8. **Defend** — maintain the §163 recordkeeping posture so a CBP audit four years later is
   answerable in minutes.

---

## 4. Architectural spine — and the critique

### Python — reasoning and domain core

FastAPI, Pydantic v2, SQLAlchemy 2, the extraction stack, the matching engine, the rule
engine, the Claude Opus 5 agent loop.

The matching engine is constraint optimization over typed records: it must be
deterministic and unit-testable, **not prompted**. Python is the only stack carrying both
the OR/optimization libraries and the Anthropic SDK.

*Weakness: none. This is the correct home for correctness-critical logic.*

### n8n — orchestration edge

Ingest triggers, per-client workflow variants, human-in-the-loop approval gates, retries,
notifications, and the client-facing "what is running" view.

Claims run for weeks with mandatory human sign-off before filing. n8n gives durable HITL
and per-tenant workflow variation without a code deploy per client.

*Weakness: weaker than Temporal for multi-week compensating transactions.*
**Mitigation — architectural invariant: n8n never holds business state.** Claim state
lives in Postgres as an explicit state machine; n8n only fires transitions.

### MCP — the tool boundary

Every capability is a typed MCP server: `mcp-ace`, `mcp-hts`, `mcp-docs`, `mcp-claims`,
`mcp-ledger`.

The decisive bet. The same tools must be callable by:

- (a) the autonomous pipeline,
- (b) a human trade analyst in Claude Code/Desktop doing exception handling,
- (c) the n8n MCP client node.

Hard-wiring tools into one agent loop forecloses (b) and (c) — and (b) is how the ~15% of
claims the agent cannot close alone actually get closed.

*Weakness: a process hop and latency vs. direct calls.* Justified only by multi-surface
reuse plus the fact that every MCP call is a natural audit-log boundary. Single-surface,
this would be pure overhead.

### Docker — isolation, reproducibility, deployability

Customs data is commercially sensitive and often ITAR-adjacent. Brokers and 3PLs will not
let it leave their perimeter. A `docker compose up` on-prem stack is what passes their
security review — a **sales** asset, not just an ops one.

*Weakness: per-tenant stacks get expensive past ~30 tenants.* Path: shared stack with
tenant-scoped Postgres schemas and RLS; isolated stacks reserved for on-prem/enterprise.

---

## 5. Risks, stated plainly

1. **Licensure.** Filing with CBP requires a licensed customs broker and a Power of
   Attorney. **Resolution: we do not file.** Drawbridge produces the filing-ready packet
   and hands it to the client's existing broker or a partner filer. Roughly 90% of the
   labor, 100% of the value, none of the regulatory surface. This constraint is
   load-bearing — do not design around filing.
2. **ACE access.** Scraping the ACE portal violates its terms. **Resolution:** ingest
   client-exported ACE reports (ES-001 / entry summary line detail) and broker feeds.
   Adds an onboarding step; removes legal risk.
3. **The hard part is extraction, not reasoning.** Scanned 7501s from 2021 forwarders are
   the real engineering risk — not the LLM. The extraction and confidence layer is the
   longest single milestone. Budget accordingly.
4. **Hallucination is unacceptable here.** A fabricated entry number is a false claim to a
   federal agency. **Resolution:** every number in a claim traces to a source-document
   span. The LLM writes narratives and judgment calls; it never originates a figure.
   Enforced by schema, not by prompt.

---

## 6. Tech stack

```
Runtime        Python 3.12 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic
Agent          Anthropic SDK — claude-opus-5 (reasoning) / claude-haiku-4-5 (extraction triage)
MCP            Python MCP SDK — 5 servers, stdio + streamable-HTTP transports
Orchestration  n8n (self-hosted, queue mode) · Redis
Data           Postgres 16 + pgvector (CROSS rulings, prior-claim precedent) · MinIO (documents)
Extraction     pdfplumber · PyMuPDF · Tesseract/PaddleOCR fallback · Claude vision for scans
Matching       Typed rule engine + OR-Tools CP-SAT for line-level allocation
Infra          Docker Compose (dev/on-prem) -> Kubernetes (multi-tenant SaaS) · Traefik · Authentik
Quality        pytest + hypothesis · ruff · mypy --strict · pre-commit
Observability  OpenTelemetry -> Grafana/Tempo · immutable append-only claim ledger
```

## 7. Repository layout

```
drawbridge/
├── CLAUDE.md                     # attribution rule + conventions
├── .claude/settings.json
├── docker-compose.yml            # + .prod.yml, .onprem.yml
├── services/
│   ├── api/                      # FastAPI: tenants, claims, review queue, webhooks
│   ├── ingest/                   # ACE/7501/invoice/BOL parsers, EDI 350/309
│   ├── extraction/               # OCR + vision + confidence + provenance spans
│   ├── classifier/               # HTS audit, CROSS retrieval, origin/valuation checks
│   ├── matcher/                  # CP-SAT import<->export allocation under §1313
│   ├── rules/                    # eligibility windows, deadlines, drawback-type routing
│   ├── packager/                 # 7551/7552, PSC, 1520(d) generation
│   └── agent/                    # Claude loop: narratives, exceptions, judgment calls
├── mcp/
│   └── mcp_ace/ mcp_hts/ mcp_docs/ mcp_claims/ mcp_ledger/
├── n8n/workflows/                # exported JSON, version-controlled
├── packages/schemas/             # shared Pydantic contracts (single source of truth)
├── tests/                        # unit · golden-claim fixtures · property-based rules
└── infra/
```

## 8. Claim state machine (Postgres-owned)

```
INTAKE -> EXTRACTING -> EXTRACTED -> CLASSIFYING -> CLASSIFIED
       -> MATCHING -> MATCHED -> QUANTIFIED -> ANALYST_REVIEW
       -> APPROVED -> PACKAGED -> HANDED_OFF -> FILED -> PAID
                                             \-> REJECTED / EXPIRED
```

Exceptions branch to `ANALYST_REVIEW` from any stage. n8n observes and notifies; it does
not own these transitions.

## 9. Service port map (dev compose)

| Service | Port | Notes |
|---|---|---|
| api (FastAPI) | 8000 | REST + webhooks |
| postgres (pgvector) | 5432 | `drawbridge` db |
| redis | 6379 | queue + cache |
| minio | 9000 / 9001 | S3 API / console |
| n8n | 5678 | queue mode |
| mcp-ace | 8101 | streamable-HTTP |
| mcp-hts | 8102 | streamable-HTTP |
| mcp-docs | 8103 | streamable-HTTP |
| mcp-claims | 8104 | streamable-HTTP |
| mcp-ledger | 8105 | streamable-HTTP |

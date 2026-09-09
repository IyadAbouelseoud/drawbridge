# DRAWBRIDGE — Architecture

> Persistent context. Read this file, `ROADMAP.md`, and `COMPLIANCE-GCC.md` at the
> start of any session before making structural decisions.
>
> **Scope: dual-jurisdiction.** United States (CBP) and GCC/Saudi Arabia (ZATCA).
> The GCC lane is not a US clone with different constants — see §3.5.

---

## 1. What this is

**Autonomous duty drawback + tariff misclassification recovery for mid-market
importers/exporters, across the US and GCC customs unions.**

US Customs refunds **99% of duties, taxes and fees** on imported goods that are
subsequently exported or destroyed (19 U.S.C. §1313). Claims reach back **5 years**.
Parallel recovery lanes:

| Jx | Lane | Statute | Window | Refund |
|---|---|---|---|---|
| US | Drawback (unused / manufacturing) | 19 U.S.C. §1313(j)(1), §1313(j)(2), §1313(a)/(b) | 5-yr import lookback, 3-yr filing deadline | 99% |
| US | Post Summary Correction | 19 CFR §141.11 | 300 days from entry | 100% of overpayment |
| US | Retroactive FTA claim | 19 U.S.C. §1520(d) | 1 year from import | 100% of overpayment |
| GCC | Drawback on re-export | GCC Common Customs Law Art. 97 + Rules of Impl. Art. 16 | Re-export within 1 Gregorian yr of **duty payment**; claim within 6 Gregorian months of re-export; 3-yr absolute bar (Art. 174) | 100% of duty paid |
| KSA | National Rules of Origin refund | Ministerial Decision 3852 | Documents within 90 days of clearance | 100% of guaranteed duty |

Full primary-source rules, thresholds, and citations: **`docs/COMPLIANCE-GCC.md`**.

Industry estimates put **$2–3B/year of eligible drawback unclaimed** — not from lack of
appetite, but because claiming requires line-item reconciliation of import entries to
export shipments across ERP, broker, forwarder and CBP data, under substitution rules
pivoting on 8-digit HTS identity, producing a packet that must survive a CBP desk audit.
Roughly 200 forensic hours per claim cycle.

Mid-market importers ($5M–$50M annual duty spend) are too small for Charter Brokerage or
Alliance Drawback to court, too large to do it by hand. They leave the money.

**Why now — US:** Section 301, IEEPA and reciprocal tariff layers inflated effective duty
rates 3–5x since 2024. The same trade flow that yielded a $200k claim in 2023 yields
~$900k today. The unclaimed pile exploded; the labor cost of claiming did not move.

**Why now — GCC:** Vision 2030 turned KSA into a re-export hub. Fasah put every *Bayan*
into one electronic single window, so the data needed for an Article 97 claim now exists
in structured form for the first time. Meanwhile Ministerial Decision 3852 (2021) made
GCC preferential origin conditional on 40% local value added and 25% workforce
localization, creating a second recovery lane — duty paid or guaranteed at import,
refundable once origin is proven. No incumbent is working either lane at mid-market scale.

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

1. **Ingest** — **US**: ACE entry-summary line detail (client-exported, never scraped) and
   CBP 7501s. **GCC**: ZATCA customs declarations (*Bayan*) exported from Fasah, and broker
   host-to-host feeds. Both: commercial invoices, packing lists, BOLs/AWBs, proofs of
   export, certificates of origin, ERP SKU master, BOMs.
2. **Extract** — structured line records from mixed scanned/native PDFs and EDI 350/309,
   with per-field confidence and **provenance spans** back to source coordinates.
   **Bilingual Arabic/English** throughout the GCC lane — see §6.
3. **Classify and audit** — reconcile declared HTS against the USITC schedule and CBP CROSS
   rulings. Flag misclassification, valuation and country-of-origin exposure in **both**
   directions (refund *and* liability — never hide the liability).
4. **Match** — the core engine, and the place the two jurisdictions genuinely diverge.
   **US**: combinatorial allocation over a substitution-eligible pool (8-digit HTS),
   respecting 5-year windows, 3-year deadlines, and per-entry duty apportionment.
   **GCC**: declaration-linkage — each re-export declaration resolves to exactly one
   import declaration. No substitution exists. See §3.5.
5. **Prove** — assemble the evidentiary packet: interchangeability narrative, chain of
   custody, destruction certificates, ruling citations.
6. **Quantify** — refund per claim, per entry line, with an audit-defensible derivation trail.
7. **Package** — **US**: CBP 7551/7552 plus PSC and §1520(d) submissions. **GCC**: ZATCA
   e-Services *Customs Duty Refund Request* payload plus evidence bundle. Both handed to
   the licensed filer.
8. **Defend** — maintain recordkeeping posture so an audit years later is answerable in
   minutes: 19 CFR §163 (US) and GCC Art. 175 / ZATCA five-year original retention (GCC).

### 3.5 The jurisdictions are not a config flag

The tempting design is one pipeline with a `jurisdiction` column. It is wrong, and the
reason is the matcher.

US drawback permits **substitution**: an imported article may be matched against a
*different* exported article sharing the first 8 HTS digits (TFTEA). That turns matching
into a combinatorial allocation problem over a pool — which is why CP-SAT is in the stack.

GCC drawback permits **no substitution at all**. Rules of Implementation Art. 16 §4–5 and
Art. 15(c) require a single identified consignment, unaltered, with the import declaration
number affixed to the re-export declaration. Matching is a linkage walk: one re-export
declaration to exactly one import declaration.

These are different algorithms, not different parameters. The design is therefore a
**strategy interface** — `MatchStrategy` — with `UsSubstitutionMatcher` and
`GccLinkageMatcher` behind it, selected by jurisdiction at claim creation. Shared: the
schemas, provenance, extraction, ledger, packaging skeleton, and state machine. Divergent:
matching, eligibility windows, refund rate, minimum thresholds, and output format.

Corollaries that fall out and must not be papered over:

| Concern | US | GCC |
|---|---|---|
| Refund rate | 99% of duty + fees | 100% of duty actually paid |
| Substitution | 8-digit HTS | **None** |
| Clock anchor | Import date | **Duty-payment date** (ZATCA permits 30-day postponement, so these differ) |
| Filing deadline | 3 years from export | 6 Gregorian **months** from re-export; 3-year absolute bar |
| Minimum claim | None | **USD 5,000** re-export value |
| Currency | USD | SAR, with a USD-threshold conversion |
| Consumption tax | MPF/HMF recoverable | **VAT and excise are not drawback** — recovered via VAT return |

`DRAWBACK_REFUND_RATE` as a module-level constant was a Week 1 simplification valid only
while the US was the sole jurisdiction. It is now a property of the jurisdiction profile.

### 3.6 Path A — US substitution allocation (CP-SAT)

`services/matcher/src/us_substitution.py`

**Shape.** Many-to-many. Any import line may feed several export lines; any export line
may draw from several import lines. With substitution the eligible pairs are every
(import, export) sharing an 8-digit HTS key inside the window, so the search space is the
product of two pools, not a list of pairs. Choosing *which* pairings to make, and how much
quantity to route through each, is an optimisation problem — greedy pairing leaves money
on the table whenever a high-duty import is consumed early by a low-value export.

**Model.**

| Element | Definition |
|---|---|
| Decision var | `x[i,e] ∈ [0, min(avail_i, avail_e)]` — integer quantity allocated from import *i* to export *e*, in minor units |
| Candidate set | pairs where `hts_i[:8] == hts_e[:8]` (or identical article for direct identity) **and** the pair is inside the window |
| Import capacity | `Σ_e x[i,e] ≤ quantity_available(i)` — an import line cannot be over-allocated across all exports |
| Export capacity | `Σ_i x[i,e] ≤ quantity_available(e)` — an export line cannot be claimed twice |
| Window | pairs outside 5 years import→export, or past the 3-year filing deadline, are never generated |
| Objective | `maximise Σ x[i,e] × duty_per_unit(i)` — total refundable duty, not total quantity |

**Why the objective is duty-weighted and not quantity-weighted.** Maximising units matched
maximises paperwork, not money. Two imports of the same HTS may carry very different
per-unit duty — a Section 301 line and a pre-301 line of the same article differ by 25
points. The solver must prefer to consume the expensive one.

**Integrality.** Quantities are scaled to integer minor units before entering the model.
CP-SAT is an integer solver, and duty apportionment must reproduce to the cent; floats in
the model would surface as cent-level drift in a filed figure.

**Determinism.** The solver is seeded and single-worker by default, and candidate pairs are
generated in a stable sort order. Two runs over the same input must produce the same
allocation, or a claim reviewed on Monday differs from the same claim refiled on Tuesday
with no explanation an auditor would accept.

**Fallback.** Where the model is infeasible or hits its time limit, the matcher returns the
best incumbent solution *with its status recorded*, and the claim routes to
`ANALYST_REVIEW`. It never silently returns a partial allocation as if it were optimal.

### 3.7 Path B — GCC direct-identification linkage

`services/matcher/src/gcc_linkage.py`

**Shape.** One-to-many from a single import declaration, never many-to-one. Rules of
Implementation Art. 16 §4 permits a consignment to be re-exported in part shipments, so
one import declaration may serve several re-export declarations. It does **not** permit a
re-export to draw on two import declarations — that would defeat the identification the
article requires.

**This is not an optimisation.** There is nothing to choose. Art. 15(c) puts the import
declaration number on the re-export declaration; the link is a fact recorded on the
document, not a pairing we select. The algorithm is a deterministic trace plus a gate.

**Gate order — cheapest and most disqualifying first:**

1. `linked_import_declaration` present and resolving to a known import line (Art. 15(c)).
2. Re-export declared value ≥ **USD 5,000**, or local equivalent (Art. 16 §2).
3. Re-export within **one Gregorian year of the duty-payment date** (Art. 16 §3(a)).
4. Claim filed within **six Gregorian months of re-export** (Art. 16 §3(b)).
5. Not past the **three-year absolute bar** from duty payment (Common Customs Law Art. 174).
6. Goods unused and unaltered (Art. 16 §5).
7. Single consignment, or part shipments sharing a proven `consignment_id` (Art. 16 §4).
8. Claimant is the importer of record, or proves purchase (Art. 16 §1).

Value screening precedes date arithmetic because it is the one gate that disqualifies a
claim before any extraction spend is worth making.

**Rejections are explicit.** Every gate failure returns the article it failed and the
figures involved. A GCC claim that dies must be able to say *why* in the words of the
statute — "re-exported 2025-03-14, 400 days after duty payment 2024-02-08, exceeding the
one Gregorian year permitted by Rules of Implementation Art. 16 §3(a)". Silent filtering
would make an unclaimable position indistinguishable from an unexamined one.

**Substitution is unreachable.** `GccLinkageMatcher` never emits `HTS_SUBSTITUTION`, the
jurisdiction profile does not permit it, the `Claim` validator rejects it, and a database
CHECK constraint refuses it. Four independent layers, because this is the failure that
would look plausible all the way to a filing.

### 3.6b Manufacturing drawback — BOM explosion (19 U.S.C. §1313(a)/(b))

> **Week 6: the bill of materials is a tree, not a list.** A component may itself be a
> subassembly manufactured from imported parts. Yield compounds down the route — a finished
> good needing one subassembly at 90% yield, itself needing two castings at 80%, consumes
> 2 / 0.8 / 0.9 = 2.7778 castings per unit. Only **leaves** are designatable: an
> intermediate node was manufactured, not imported, so there is no entry line to designate
> against it. The CP-SAT designation ceiling is keyed on the **route** rather than the leaf,
> because the same casting reached through two subassemblies is consumed at two different
> rates and one route's allocation must not eat the other's headroom.

Week 3 covered **unused merchandise** (§1313(j)): the article exported is the article
imported. Manufacturing drawback is the case where it is not — imported raw material is
consumed to produce a different finished good, and the refund follows the material
*through* the manufacture.

**What changes in the model.** Under §1313(j) an allocation of *q* units of import
satisfies *q* units of export: the exchange rate between the two sides is 1. Under
§1313(a)/(b) it is the BOM multiplier. Exporting one finished good consumes
`quantity_per_unit` of each component, so the units no longer cancel and the constraint
matrix is no longer a plain transportation problem.

| | §1313(j) unused | §1313(a)/(b) manufacturing |
|---|---|---|
| Sides | import article ↔ same/substitutable article | raw material ↔ finished good |
| Exchange rate | 1:1 | BOM `quantity_per_unit`, plus yield |
| Extra constraint | none | one per (finished good, component) pair |
| Evidence | interchangeability narrative | bill of materials or formula |

**Statutory anchors:**

- §1313(a) — direct identity: the *same* imported merchandise is used in manufacture.
- §1313(b) — substitution: merchandise classifiable under the **same 8-digit HTS
  subheading** as the imported merchandise may be substituted.
- A **bill of materials or formula** must accompany the claim, identifying merchandise and
  article by 8-digit HTS subheading and the quantity of merchandise (TFTEA).
- The designated quantity must be **the quantity actually used** to produce the exported
  article — not the quantity purchased, and not the quantity on hand.
- Merchandise must be used within **5 years** of import.
- Where one manufacturing process yields **several products**, drawback is distributed
  across them by their **relative values at the time of separation** (19 CFR 190 subpart B).

**Model extension.** For each finished-good export line *e* and each component *c* in its
BOM, with multiplier `m[e,c]` (component units per finished unit) and yield `y[e,c] ∈ (0,1]`:

```
required[e,c] = exported_quantity[e] × m[e,c] / y[e,c]
Σ_i x[i,e,c] ≤ required[e,c]          # cannot designate more than was used
Σ_e,c x[i,e,c] ≤ available[i]         # import capacity, unchanged
```

The decision variable gains a component index. Objective is unchanged — maximise
refundable duty — but a finished good now draws from several import pools at once, and the
solver must decide which lot of each component to designate.

**Yield is not optional.** Scrap, waste and process loss mean the material consumed
exceeds the material embodied in the finished article. Claiming only the embodied quantity
under-claims; claiming input without evidencing yield over-claims. The multiplier is
therefore stored as `quantity_per_unit / yield`, and both halves are kept so the derivation
trail can show an auditor which is which.

**Relative-value distribution.** Where a process yields joint products, the component cost
attributable to each is apportioned by value share at separation, not by quantity. The BOM
schema carries `relative_value_share` for exactly this; it defaults to `None` for
single-output processes, where the question does not arise.

**What is deliberately not modelled yet.** Multi-level BOMs (a component that is itself
manufactured from other imports) are flattened to one level at ingest. Nested explosion is
a week 6+ concern and needs the ERP integration to be real first — the model above holds
either way, since a flattened BOM is a special case of a nested one.

### 3.8 Shared contract

Both paths implement `MatchStrategy`:

```
match(imports, exports, profile, as_of) -> MatchResult
```

`MatchResult` carries the accepted `LineMatch` tuple, a typed rejection list, and solver
metadata (status, wall time, candidate count). The API selects the strategy from the
claim's jurisdiction; nothing downstream branches on jurisdiction again.

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
2. **Portal access — ACE and Fasah.** Scraping the ACE portal violates its terms; the same
   posture applies to Fasah. **Resolution:** ingest client-exported reports (ACE ES-001 /
   entry summary line detail; Fasah *Bayan* exports) and broker host-to-host feeds. We do
   not hold client portal credentials in either jurisdiction. Adds an onboarding step;
   removes legal risk.
3. **The hard part is extraction, not reasoning.** Scanned 7501s from 2021 forwarders are
   the real engineering risk — not the LLM. Bilingual Arabic/English *Bayan* and invoice
   processing raises that risk rather than lowering it (§6). The extraction and confidence
   layer is the longest single milestone. Budget accordingly.
4. **Hallucination is unacceptable here.** A fabricated entry number is a false claim to a
   federal agency. **Resolution:** every number in a claim traces to a source-document
   span. The LLM writes narratives and judgment calls; it never originates a figure.
   Enforced by schema, not by prompt.
5. **GCC rules are partly unverified in English.** ZATCA Resolution 28624's article numbers
   could not be confirmed from English sources; only the Arabic Umm Al-Qura text is
   authoritative. The GCC Common Customs Law and its Rules of Implementation — which carry
   every operative drawback constant — *were* obtained from the GCC Secretariat's own
   publication and are reliable. **Resolution:** `COMPLIANCE-GCC.md` marks verification
   status per section and lists open questions; each unresolved question routes to
   `ANALYST_REVIEW` rather than being guessed. Confirm against Arabic source before the
   first live KSA filing.
6. **Licensure applies in KSA too.** Saudi customs clearance is a licensed activity.
   The §5.1 posture is unchanged and jurisdiction-independent: Drawbridge produces the
   filing-ready packet; a licensed broker files it.

---

## 6. Tech stack

```
Runtime        Python 3.12 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic
Agent          Anthropic SDK — claude-opus-5 (reasoning) / claude-haiku-4-5 (extraction triage)
MCP            Python MCP SDK — 5 servers, stdio + streamable-HTTP transports
Orchestration  n8n (self-hosted, queue mode) · Redis
Data           Postgres 16 + pgvector (CROSS rulings, prior-claim precedent) · MinIO (documents)
Extraction     pdfplumber · PyMuPDF · Tesseract (eng+ara) / PaddleOCR fallback · Claude vision
               Arabic-Indic digit normalisation · RTL/bidi handling · bilingual field aliases
Matching       MatchStrategy interface: OR-Tools CP-SAT (US substitution) | linkage walk (GCC)
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
│   ├── ingest/                   # ACE/7501, Fasah Bayan, invoice/BOL, EDI 350/309
│   ├── extraction/               # native PDF -> OCR fallback, bilingual ar/en, provenance
│   ├── classifier/               # HTS audit, CROSS retrieval, origin/valuation checks
│   ├── matcher/                  # us_substitution.py (CP-SAT) | gcc_linkage.py
│   ├── rules/                    # per-jurisdiction windows, thresholds, lane routing
│   ├── packager/                 # 7551/7552, PSC, 1520(d) | ZATCA refund request
│   └── agent/                    # Claude loop: narratives, exceptions, judgment calls
│                                 #   worker.py — the drafting loop as a process (wk 14)
├── mcp_servers/            # named to avoid shadowing the `mcp` SDK package
│   └── mcp_ace/ mcp_hts/ mcp_docs/ mcp_claims/ mcp_ledger/
├── n8n/workflows/                # exported JSON, version-controlled
├── packages/schemas/             # shared contracts incl. jurisdiction.py profiles
├── scripts/                      # ingest_tariff.py · embed_corpus.py · e2e_pipeline_test.py
│                                 # rls_bootstrap.py · tenant_offboard.py (operator, owner DSN)
│                                 # mint_token.py · pilot_us.py / pilot_ksa.py / pilot_run.py
│                                 # manage_secrets.py · calibrate_thresholds.py (wk 13)
│                                 # ingest_cross.py — fetch a CROSS sample, then load it (wk 14)
├── tests/                        # unit · golden-claim fixtures · property-based rules
│   └── fixtures/                 # tariff_benchmark.json · bayan.py (synthetic RTL table)
├── secrets/                       # gitignored; the six files Postgres, MinIO and n8n read
└── infra/
    ├── authentik_bootstrap.py     # OIDC provider + tenant claim + RS256 round trip (wk 14)
    ├── vault_bootstrap.py         # KV v2 mount, one-path policy, AppRole (wk 14)
    ├── caddy/Caddyfile            # the only container that binds a port on-prem
    ├── docker/                    # base.Dockerfile + the MCP Dockerfile generator
    └── postgres/init.sql          # the n8n and authentik databases
```

`scripts/` and `infra/` are type-checked under `mypy --strict` alongside the services. They
build customs payloads and configure the identity provider, neither of which is a lower
standard of correctness than the code that consumes them.

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


---

## 10. Bilingual extraction (GCC lane)

The *Bayan* and Saudi domestic commercial invoices are bilingual Arabic/English, and GCC
certificates of origin are frequently Arabic-only. Five concrete failure modes drive the
design — none are solved by "turn on Arabic OCR".

| Failure mode | Handling |
|---|---|
| **Arabic-Indic digits** (٠١٢٣٤٥٦٧٨٩) and Eastern variants (۰۱۲۳۴۵۶۷۸۹) in amounts | Normalise to ASCII digits at token level, before any parse. A duty figure read as `٥٠٠٠` must become `5000`, never `0` or a mojibake string |
| **Bidi reordering** — RTL Arabic interleaved with LTR numbers and Latin HS codes | Extract with bidi-aware ordering; store the logical-order string, not the visual-order one. Visual order silently reverses multi-part numbers. Week 10 measured which half of this actually bites — see §15.6 |
| **Arabic presentation forms and diacritics** — the same word in several Unicode encodings | NFKC-normalise, strip tashkeel, unify alef/ya/ta-marbuta variants before matching a field label |
| **Bilingual field labels** — the same field labelled `رقم البيان` or "Declaration No." depending on issuer | Field resolution goes through a bilingual alias table, not a regex per layout |
| **Arabic-language scans** where the native text layer is absent or wrong | Tesseract `ara+eng` (or PaddleOCR `arabic`) fallback, gated on the native-path confidence score, with Claude vision as the last resort |

**Ordering invariant, unchanged from the US lane:** the native PDF text layer is always
tried first and OCR is a *fallback*, never the default. OCR is slower, less accurate, and
produces weaker provenance spans. A document that yields clean native text must never be
sent through OCR.

**Provenance under OCR.** OCR spans carry bounding boxes but no reliable character offsets,
so `Span.raw_text` holds the recognised token and `Confidence.method` records the engine.
Any figure whose only provenance is OCR below the confidence floor sets
`Confidence.needs_review`, which routes the claim to `ANALYST_REVIEW` regardless of score.

## 11. Output targets

| Jurisdiction | Target | Form |
|---|---|---|
| US | CBP 7551 / 7552, PSC, §1520(d) | Rendered forms + derivation trail, to the licensed filer |
| GCC / KSA | ZATCA e-Services → Customs Services → **Customs Duty Refund Request** | Typed payload (importer identity, IBAN, original import declaration no., linked re-export declaration no., duty paid, amount claimed, SAR) + evidence bundle + derivation trail |

ZATCA publishes no field-level machine schema for the refund request, so the GCC payload is
modelled as a typed Pydantic object rendered to both a human-completable form and JSON.
When ZATCA publishes an API, only the renderer changes — the claim data core does not.

### 11.1 The packager (week 6)

`services/packager` is a renderer and nothing else. Every figure it prints arrives already
quantified by the matcher and already checked by the rules engine — the same constraint the
LLM operates under, applied one layer further out. That is what lets a packet be regenerated
years later and reproduce byte-for-byte from stored input.

| | US lane | KSA lane |
|---|---|---|
| Output | CBP 7551, plus 7552 where a transferor or manufacturing theory is involved | ZATCA refund-request JSON |
| Format | AcroForm PDF — real fields, generated appearance streams | UTF-8 JSON, money as strings |
| Why | A filer must be able to correct a figure in place, and a downstream reader must be able to pull values back out without parsing a layout | Arabic survives JSON intact; the PDF base-14 fonts cannot encode it |
| Citations | All verified — the statute and the CFR are published | GCC law verified; **ZATCA 28624 procedural citations are `ANALYST_REVIEW` placeholders** |

**The one thing the packager decides.** Whether the packet may be transmitted. A packet
carrying an open citation sets `requires_analyst_review` and is blocked. That check lives in
`FilingPacket` rather than in the caller, because a caller that forgets it produces a packet
that looks finished.

**Two rendering modes.** `pdf.fill_template` writes into an official CBP AcroForm template
where a tenant has one on file — that output is literally CBP's form. Without one, the same
field set renders as a paginated transcription that says on its face it is not a CBP-issued
document. A packet that quietly *looked* like the official form while not being it would be
worse than one that does not pretend.

### 11.2 Tariff corpus ingestion (week 6)

Three sources, three parsers, one table. Two decisions are the opposite of the obvious one:

**Revisions are inserted, not replaced.** A claim is classified against the schedule in
force on its entry date, and a five-year lookback spans several revisions. The unique key
includes `revision`; reloading the same revision updates descriptive fields only, and the
loader reports inserts against updates so a duplicated revision is visible rather than
silently searched twice under two names.

**Duty rates stay as published text.** "Free", "2.5%", "6.5c/kg", "4.4c/kg + 2.8%". A float
column loses the specific and compound forms silently, and silently is how a compound rate
becomes an understated claim. `ad_valorem_rate` is populated only where the rate is purely
ad valorem.

The USITC export is a tree flattened into rows, so a line reads "Other" and means four
ancestors concatenated. Rows with no code are skipped but still walked, because those are
exactly the rows holding the description their children inherit. The ZATCA export is
bilingual and its Arabic is **cleaned but not folded** — NFKC and bidi-control stripping are
encoding fixes, while the orthographic folding used for field matching would misspell a
legal description of goods on every packet quoting the line.

Ingest reads a downloaded file, never a live endpoint. A corpus assembled by a network call
is reproducible only for as long as the publisher keeps the URL alive.


---

## 12. Week 7 additions

### 12.1 Embeddings — a choice, not a default

`services/classifier/src/embeddings.py` makes the backend configurable rather than baked
in, because a corpus embedded with one model and queried with another produces confident
nonsense: cosine distance between two embedding spaces is meaningless, not merely
inaccurate. Every backend carries `model_id`, and a corpus is queryable only by the backend
that wrote it.

`HashingEmbedder` is deterministic character-trigram projection — offline, reproducible
forever, and **lexical**. It reports `is_semantic = False` so nothing presents it as
semantic search. It exists to make the whole ingest-and-search path exercisable end to end
before a model endpoint is chosen. `OllamaEmbedder` is the real one, local by choice: a
classification that reached a filing must be reproducible without a third-party API still
being alive, and tenant document text should not leave the deployment.

`scripts/embed_corpus.py` selects only rows with a NULL embedding, so it is resumable by
construction and a second run over a finished corpus is a no-op. `FOR UPDATE SKIP LOCKED`
lets two workers share a corpus without blocking or double-writing. It never prints a
vector.

### 12.2 OCR: recognition and acceptance are different steps

Nothing used to stand between the OCR engine and the matcher, which meant a
43%-confidence digit could become a filed figure. `ocr.gate` now decides what may leave
extraction, against three deliberately different thresholds:

| Threshold | Value | What it catches |
|---|---|---|
| `OCR_CONFIDENCE_FLOOR` | 0.90 | A single unreliable token |
| `OCR_FIELD_FLOOR` | 0.85 | A field where *nothing* failed and *everything* is mediocre — the ordinary shape of a bad scan |
| `OCR_NUMERIC_FLOOR` | 0.95 | A digit, held stricter than a letter |

The numeric asymmetry is the point. A misread letter in a goods description is a defect an
analyst corrects by eye; a misread digit in a duty amount is a wrong number filed with a
customs authority. So a low-confidence *word* is `REVIEW` and a low-confidence *digit* is
`REJECT` — it never reaches the matcher in any form.

The field mean is weighted by character count, so a one-character fragment at 0.99 cannot
carry a twelve-character amount at 0.80 over the line. A numeric field carrying letters
from a second script is rejected outright: it means the engine merged an adjacent label
into the value, and the number that survives that is not the number on the document.

**0.90 is conservative, not measured.** Tuning needs a labelled scanned *Bayan* corpus,
which does not exist yet. At 0.90 the pipeline over-rejects, and over-rejection costs
analyst time while under-rejection costs a misfiled claim. `OcrConfig` carries the floors
per tenant, so calibration is a config change.

### 12.3 ERP-sourced nested bills

An ERP does not return a tree. SAP's CS_BOM_EXPL and Oracle's BOM_COMPONENTS both return a
flat parent-child list, and `services/ingest/src/erp_mock.py` reconstructs the tree from
it. Three things that list does, all handled rather than assumed away:

- **Scrap, not yield.** ERPs record 5% scrap, not 0.95 yield. Backwards, every level
  understates consumption, compounding with depth.
- **Phantom assemblies.** A grouping level never stocked or built. It has no imported
  article behind it, so it collapses into its parent with its quantity multiplied through
  — leaving it in place would create a designation level with nothing importable under it.
- **Cycles.** A part listed as its own ancestor. Fatal, not truncated: cutting the loop
  would yield a plausible multiplier from an incoherent bill.

The mock is keyed by `(tenant, part)`. A mock that ignored tenancy would pass every test
and then leak one customer's bill into another's claim the day a real connector replaced it.

### 12.4 The two US lanes that are not drawback

Drawback recovers duty on goods that left. These recover duty never owed.

**PSC (19 CFR §141.11).** There is no paper form — a PSC is transmitted through ABI as a
full replacement entry summary, so the output is the structured payload plus a human
summary for authorisation. Its deadline is the **earlier** of 300 days from entry and 15
days before liquidation; liquidation usually binds first, and an offset-only check calls
claims timely that CBP will refuse. A correction that *increases* duty is a valid PSC and
is refused here, because presenting an amount owed as an amount recoverable would misstate
the claim.

**§1520(d).** A written post-importation preference claim, so it renders as a document as
well as a payload. One year from importation, no extension. The preferential rate is not
assumed to be zero — several agreements phase rates down rather than to nothing — and a
line with no certification of origin on file is named on the claim itself, because filing
without it is a claim CBP will deny and the year cannot be reclaimed.

`PacketRequest.lane` now routes. The router refuses the two alternates with a pointer
rather than rendering a 7551, since a 7551 for a PSC is a coherent form for the wrong claim.

---

## 13. The agent layer (week 8)

### 13.1 Where the LLM sits, and why it sits there

`services/agent/` is the last thing built, deliberately. Everything upstream of it —
extraction, classification, the CP-SAT allocator, the rules engine, the quantifier — is
deterministic and testable, and produces every figure a claim contains. The agent operates
on that output and adds two things the deterministic core cannot: prose, and judgment about
a record it can read but not compute over.

The rule from `CLAUDE.md` — *the LLM writes narratives and judgment calls; it never
originates a number* — is enforced, not requested. `grounding.py` scans every generated
memo and rejects any numeric token not traceable to the fact set the model was given.

This ordering is what makes the enforcement possible. Had the agent been built first there
would have been no authoritative fact set to check against, and the rule would have been a
convention: true until a prompt happened to break it.

### 13.2 What the guard actually catches

Not the model lying. The model being *fluent*.

| Generated | Record says | Why it is dangerous |
|---|---|---|
| "approximately 4,800 units" | `12500.0000` units, `4812.50` duty | Reads as helpful rounding. A reader cannot tell which figure was meant. |
| "duty of 4,821.50" | `4812.50` | A transposition. Perfectly plausible; survives no comparison. |
| "0.42 confidence" | `0.71` | Confidence scores are figures too, and are the ones most casually restated. |

Folding handles the cases where one figure has several correct spellings: `4,812.50`,
`4812.50` and `٤٨١٢.٥٠` are one number, and a guard that rejected the readable spelling
would be worked around. Integers at or below 12 read as prose — "the first of two
conditions" is English, and a customs quantity is never 2.

Citations are checked differently: by membership in the closed set the drafter was given,
not numerically. Scanning `19 CFR §163.1` for figures would reject it as three invented
numbers, which is the guard failing closed on correct output — the way guards get disabled.

### 13.3 The two drafters

**Interchangeability** (`interchangeability.py`) — the substitution justification for a US
§1313(j)(2) pairing. One point of law matters here: post-TFTEA the operative test is
classification, not commercial equivalence. Substitution requires the same 8-digit HTS
subheading, narrowing to the 10-digit statistical reporting number where the 8-digit
description begins with "other". Commercial interchangeability is *supporting* analysis —
it is what persuades a reviewing officer the pairing is real — but it has not been the
statutory standard since 2016, and the schema separates the two so a memo cannot present
the support as the test.

The schema also requires `distinguishing_facts`. A memo listing only favourable facts reads
as advocacy, and a difference CBP finds unaided is worse than one the claimant raised.

**Exceptions** (`exceptions.py`) — pre-analysis of one `review_queue` row, drafted before an
analyst opens it. Each `ReviewReason` gets its own framing, because the question genuinely
differs: a GCC threshold near-miss is an arithmetic and valuation question with a
bright-line answer, a superseded CROSS ruling is a classification question, a low-confidence
OCR field is a question about one glyph. A single generic prompt produces a memo that is
fluent about all three and useful for none.

`blocking_unknowns` being non-empty is a healthy outcome. A confident memo over a thin
record is the failure mode — it is the one an analyst is most likely to accept without
checking.

### 13.4 The call itself

Hardcoded in `client.py`, not parameterised, because each is a property of the product:

- `max_tokens = 1024`. A memo needing more is padding, and the cap bounds a runaway loop.
- `temperature = 0`. Two analysts opening the same claim must see the same memo, and a
  filing built on a sampled narrative cannot be explained when an auditor asks four years
  later why it says what it says.
- **Forced tool use.** One tool, the memo schema, required by `tool_choice`. The model has
  no prose path, so there is no JSON parsing step anywhere in the service and nothing that
  can fail on a preamble.

One retry on schema violation, with the validation error fed back. A second failure at
temperature 0 will not become a third success — it means the schema and the task disagree.

### 13.5 Queue integration

The memo lives in its own column (`review_queue.agent_memo`), not inside `payload`.
`payload` is documented as carrying the matcher's output verbatim, and an analyst comparing
a row against a re-run of the matcher needs that to stay true.

`agent_model` records the model *and* prompt version. A memo that influenced a filing is a
document an auditor may ask about, and a prompt revision changes the output as surely as a
model change does.

Drafting is a separate call (`POST /review/draft`, or `draft_exception_memo` in
`mcp-claims`), never part of `/review/suspend`. n8n waits on the suspend call; coupling
workflow suspension to a model round trip would make it fail for an unrelated reason. The
memo is wanted when the analyst arrives — minutes to hours later — so drafting then costs
nothing.

A failed draft leaves the column NULL. The row is then exactly what an analyst would have
read before this service existed. An error blob in the memo column would mean someone
scanning for pre-analysis finds something and reads it; nothing is better than noise.

### 13.6 Embeddings became real (week 8)

`fastembed` replaces the trigram placeholder: a quantised ONNX sentence-transformer running
in-process on CPU. No API, no key, no per-token cost; one model download, then offline.

Multilingual by necessity rather than preference. Half the corpus is Arabic, and an
English-only encoder has no useful geometry for it. It also retrieves across the language
boundary — an Arabic *Bayan* description reaches an English USITC line — which is the
property the dual-jurisdiction corpus needed and did not have.

Two things this broke, both found by running it rather than by reading it:

**Width.** The column was 1536; the model emits 384. Migration `a7c31f9d4e60` moves it and
nulls the existing vectors. There is no conversion between embedding spaces, so any backend
change already required a full re-embed; nulling makes explicit what was true anyway.

**The distance ceiling.** `VECTOR_CEILING = 0.55` was measured against trigram distances,
which collapse fast. This model's distances are compressed — a good match near 0.6, an
unrelated one near 0.86 — so every correct hit fell outside the threshold and search
reported `method=lexical` with nothing found, indistinguishable from a corpus that was never
embedded. Nothing raised.

The ceiling now belongs to the backend (`Embedder.vector_ceiling`), because a distance
threshold is meaningful only inside one embedding space. It does not degrade gracefully
across a model change: it suppresses everything or admits everything.

The value week 8 chose was provisional and said so. Week 9 measured it against a labelled
set and replaced it — see §14.6, which also explains why a ceiling was the wrong instrument
for the job it was being asked to do.
---

## 14. The closed loop (week 9)

### 14.1 What was actually missing

The n8n pipeline had existed since week 4 and referenced four endpoints that did not exist:
`/documents/batch`, `/extraction/run`, `/claims/persist`, `/claims/transition`. It described
the intended shape and could not run. Week 9 built the endpoints, added
`/classification/run` and `/packaging/build`, and rewrote the workflow around all six.

The registration order in `services/api/src/main.py` is the loop, and is deliberately not
alphabetical:

```
/documents/batch      bytes into MinIO, content-addressed, registered against the tenant
/extraction/run       reads them back; native or scan, and how confidently
/classification/run   corroborates the declared codes against the schedule
/matching/run         CP-SAT (US) or declaration linkage (GCC)
/triage/evaluate      does a human need to see this
/review/*             the human
/claims/persist       the computation becomes state
/claims/transition    the state machine
/packaging/build      7551/7552 or ZATCA JSON
```

### 14.2 The two paths, and where they rejoin

```
                                     ┌── triage: nothing ──> approved ──┐
ingest -> extract -> classify -> match -> persist                       ├─> package -> packaged
                                     └── triage: exception ──> suspend ─┘
                                            -> draft memo -> wait
                                            -> analyst resolves -> approved
```

The claim is persisted **before** the branch. An analyst opening a suspended run needs a
claim to look at — an id, a refund figure, a derivation — not a workflow variable, and
`mcp-claims` needs something to point at. It also means a crashed run loses the workflow
and not the work, which is the whole reason §4 puts the state in Postgres.

### 14.3 Approval without a human

`QUANTIFIED -> APPROVED` opened in week 9. Before it, every claim passed through
`ANALYST_REVIEW`, including the ones triage had nothing to say about — a queue of
non-decisions, which is a queue people stop reading.

The guarantee did not go away, it moved. `analyst.transition_claim` refuses **any**
transition into `APPROVED` while the claim carries an unresolved `review_queue` row,
whoever is asking. It had to move out of `approve_claim`, because the pipeline is now a
caller and it is the caller that runs unattended. Transition rows record `pipeline` as the
actor, so which claims took the automated lane is a query rather than an inference.

### 14.4 What the pipeline is not allowed to decide

Two endpoints report rather than act, and the distinction is load-bearing.

**`/classification/run` corroborates; it never rewrites.** The tariff codes on a claim are
the codes that were actually declared. A mismatch between a declared code and what the
schedule's own text points to becomes a review item with the schedule text attached — not a
correction. Reclassifying merchandise is a customs matter with its own procedure; a
classifier that quietly changed a code would put an embedding model between a description
and a duty rate.

**`/extraction/run` reports readability; it does not produce lines.** It says whether a
document has a usable native text layer, what fields it found, and how confidently — and it
is allowed to refuse. Typed `EntryLine`/`ExportLine` objects come from the structured source
(ERP feed, broker export). Assembling them from a scanned *Bayan* table needs the glyph
x-coordinate work deferred since week 3. That division is not a placeholder: the figures
come from a system of record and the documents are what evidences them, which is the shape
19 CFR §163 asks for.

### 14.5 Classification confidence: what confirms an answer

A `TariffHit` carries `needs_analyst_confirmation`, and week 9 changed what sets it.

The old rule was "the vector path alone found this". The week 9 benchmark
(`tests/fixtures/tariff_benchmark.json`) showed that treating agreement between the two
paths as corroboration ignores how weak either was: "wooden lead pencils" reached wooden
office furniture on a trigram coincidence over the word *wooden* at 0.157, with a mediocre
vector distance agreeing, and came back as an answer needing no analyst.

The rule now: **the lexical path confirms, alone, above
`search.CONFIRMATION_LEXICAL_FLOOR`.** A strong trigram match against published tariff text
is the schedule saying so. A near vector neighbour is a model saying so. Those are different
claims, and only the first is evidence. The vector path finds and ranks — which is what it
is good at, and is why cross-lingual retrieval still works — and never confirms.

The floor in force is stored on the hit rather than looked up, because a classification an
auditor asks about in 2030 has to be explicable against the threshold that was actually
applied, not the one the constant holds by then.

### 14.6 The vector ceiling is a rubbish filter, not a precision mechanism

Week 8 left `vector_ceiling` at 0.75, measured against nine lines, and said so. Measured
against twenty labelled queries it admitted eight of the ten hard negatives.

The result that matters is the one no value fixes: the worst true positive sits at 0.624 and
the nearest unanswerable query at 0.508. The ranges **overlap**. "Ruggedised field laptop
computer" and "portable cordless electric hand drill" are not separable by distance against
this corpus at any threshold, so no ceiling delivers precision.

0.68 is therefore calibrated for recall alone — the smallest value that still retrieves the
correct code for all ten positives, keeping "live breeding cattle" and "marine cargo
insurance brokerage" outside. Everything under it is a candidate; §14.5 decides which
candidates are answers.

`test_the_two_distance_ranges_overlap` asserts the overlap. If a future model separates the
classes, the suite says so rather than carrying a mechanism nobody re-examines.

### 14.7 Synchronous work inside an async API

Three paths cannot run on the async session: the agent worker (the SDK call blocks), the
packager (rendering is CPU work), and persistence, which is shared verbatim with
`mcp-claims`. `services/api/src/sync_db.py` gives them one process-wide engine and runs them
in a worker thread.

The alternative was two implementations of the claim state machine, one async for the API
and one sync for the MCP servers, with the interesting bugs in whichever the tests missed.

### 14.8 What running it found

**`entry_lines.port_of_entry` was `String(16)`.** Wide enough for a 4-digit CBP port code,
too narrow for "Jeddah Islamic Port". Every KSA claim was unpersistable and nothing caught
it, because until week 9 nothing persisted a claim — the GCC matcher was exercised entirely
in memory. Migration `e5c48b71d90a`.

This is the argument for building orchestration after the components rather than before.
The defect is not in a component; it is a disagreement between the schema's US assumptions
and the GCC lane's data, and only a run that crosses both surfaces it.

### 14.9 `scripts/e2e_pipeline_test.py`

Drives the deployed API the way n8n drives it — one call per node, same order, same payload
shapes. Case A is a clean US claim that reaches a 7551 with no human in the transition
trail. Case B is a GCC claim with one re-export ~7% under the Article 16 §2 minimum: the
short line is rejected, the rejection raises a `threshold_near_miss` row, the run suspends,
`mcp-claims` resolves it, and a ZATCA payload comes out.

Case B is expected to end **untransmittable**. The packet carries five open Resolution 28624
citations and the packager blocks it (`COMPLIANCE-GCC.md` §8.4.1). A run reporting Case B as
ready to file would mean that guard had been lost, so the script asserts the block rather
than the absence of one.
\n

---

## 15. The record (week 10)

### 15.1 What §163 and Art. 175 actually ask for

Both regimes reduce to the same operation: produce, on request and years later, the record
supporting a figure. Not the claim, not the document — the figure. Week 9 could answer
"which document" and week 10 answers "where on it".

Three pieces, each doing one job:

```
ProvenanceSpan     hash + page + box, all mandatory, on every figure a line states
audit_ledger       append-only, hash-chained, one row per event that touched a claim
trace_figure       claim id + field name -> the box, from the ledger and from the line
```

### 15.2 `ProvenanceSpan` versus `Span`

`Span` addresses a *region* and always could be loose: a page, a field path, optionally a
box. That is right for saying "this record came from this document".

`ProvenanceSpan` addresses a *figure* and has no optional fields. `document_sha256` sits
alongside `document_id` because the id is ours and the hash is the document's — an auditor
holding a PDF can verify the hash without access to our database, which is what makes the
trace checkable rather than asserted. A degenerate box is rejected at construction: a
swapped coordinate pair renders as a highlight over nothing, and nothing downstream would
notice.

`EntryLine` and `ExportLine` refuse to construct when a figure they state has no entry in
`provenance.figures` — but only when the provenance cites a paginated document
(`provenance.STRUCTURED_KINDS` is the exclusion). An EDI feed has records rather than
pages; a 7501 has pages, and no longer gets to address its duty figure as `lines[0]`.

Zero and `None` are exempt. A duty of 0.00 on a line that paid none is a default, not a
figure someone read, and demanding coordinates for it would mean pointing at whitespace —
which is the failure the whole mechanism exists to prevent.

### 15.3 The ledger, and why it is chained

`claim_transitions` records state changes and is append-only *by convention*: nothing
updates it, and nothing stops a future route from starting to. `audit_ledger` is append-only
by construction — triggers refuse UPDATE, DELETE and TRUNCATE, and TRUNCATE needs its own
statement-level trigger because row triggers do not fire on it.

Triggers stop the application. They do not stop a role that can drop them, so each row also
carries `entry_hash`: SHA-256 over the row's own content plus its predecessor's hash, per
tenant. `verify_chain` recomputes the lot and distinguishes the two failure modes — a
mismatched `prev_hash` means a row was removed, a mismatched `entry_hash` means one was
altered.

Per tenant rather than globally, for two reasons: a global chain makes one tenant's
verification depend on rows they cannot see, and it serialises every write in the system
behind one advisory lock. `record` takes a transaction-scoped advisory lock keyed on the
tenant, so the read-then-write of `prev_hash` cannot fork under concurrency.

Ordering is `sequence`, a database-assigned identity column, not `recorded_at`. Persistence
writes several rows inside one transaction and they share a timestamp to the microsecond;
"which came first" is exactly what an audit of an override asks.

**What the chain does not detect** is the removal of an entire tenant's chain, because an
empty chain verifies. The TRUNCATE guard covers the obvious route; anything beyond it is
off-site retention, which is a deployment question this section does not answer.

### 15.4 Two schema consequences

**`tenant_id` is RESTRICT.** A tenant with ledger rows cannot be deleted, so offboarding is
a deliberate manual act. A retention obligation that a `DELETE` satisfies is not a retention
obligation.

**`claim_id` carries no foreign key at all.** Every other claim-scoped table cascades. A
cascade here would mean deleting a claim silently deletes the evidence it existed, so the
column is allowed to outlive its claim instead — the correct direction for an audit record
to fail.

### 15.5 `trace_figure` reads two copies and compares them

The ledger copy is written at persistence time and cannot change. The `provenance` column
on the line is live. `trace_figure` returns both and reports `consistent`, rather than
preferring one — because which of them is wrong is the finding, and a corrected extraction
and an altered record look identical from one side.

The comparison is **containment**, not equality: every box the claim shows must be one the
ledger recorded, and the ledger may hold more. A GCC claim whose second re-export fell under
the Art. 16 §2 minimum persists both export lines and claims one; the unclaimed line's
ledger entry is the record of something considered and excluded, which an audit of a
rejection wants.

### 15.6 RTL tables: the corruption is in the numerals

`services/extraction/src/geometry.py` was stubbed in week 2 on the theory that glyph advance
direction would reveal which producers stored Arabic visually. Measured, that was the wrong
half of the problem.

MuPDF applies its own bidi pass to Arabic *letter* runs before anything downstream sees
them, so words usually arrive readable whichever way the producer wrote them. It does not do
the same for Arabic-Indic *numerals*: a quantity of ١٢٠٠ comes out of the text layer as
٠٠٢١, normalising to 21. A reversed word is noticed by whoever reads it. A reversed quantity
is filed.

So the reconstruction stopped consulting the stream. `extract_table` reads per-glyph boxes
from `rawdict`, bands them into rows by vertical overlap, splits them into cells at
horizontal gaps wider than a word space, and emits each cell in the order the coordinates
say a reader meets it — right to left for an Arabic cell, left to right for the Latin and
numeric runs inside one. That last part is not a nicety: a *Bayan* writes its HS code inside
an Arabic cell, and reversing the whole cell turns 84713000 into 00031748, which is
well-formed, classifiable, and a different chapter.

Word spaces are restored from the physical gaps, because space glyphs are dropped on the way
in and the gap they leave is the only remaining evidence that a cell holds a label and a
value rather than one long token.

`order_of` survives as a diagnostic on `Cell.glyph_order` and nothing branches on it — see
§15.7 for why the tests are built the way they are.

**What this does not do** is decide which cell is which field. Column semantics come from a
template or from the table header, supplied by the caller. Inferring them from position
would put a layout heuristic between an Arabic table and a duty figure. Week 11 supplies
both halves — see §16.2.

### 15.7 Testing a document format with no font

No font in the environment carries Arabic glyphs, and depending on a system font would make
the suite pass on one machine and skip on another. `tests/fixtures/bayan.py` therefore
assembles the fixture at the PDF object level: a Type0/Identity-H font with a ToUnicode CMap
and no embedded font program, one `Tm` per glyph. MuPDF resolves each code through the CMap
and positions it from the text matrix, which is exactly the pair of facts the geometry
module consumes. The page renders as empty boxes and no test looks at how it renders.

The suite writes the same table twice — once with the glyphs emitted in visual order, once
in logical — and asserts identical output. A test against one storage order would pass just
as well if the module were quietly reading the stream.

### 15.8 What this cost the e2e

Week 9's `scripts/e2e_pipeline_test.py` sent `field_path="lines[0]"` and its docstring said
that claiming a rectangle it had not measured would be a fabricated provenance record. That
was correct, so week 10 changed the input rather than the standard.

Each case now ingests **two** documents — the import declaration and the export evidence —
because a re-export value is not printed on an import *Bayan* and no box on that page holds
it. Figures are located with `page.search_for` on the rendered bytes rather than computed
from the layout constants, so the coordinates are where the text is and not where the script
intended to put it. `_boxes` raises when a label is not on the page; there is no approximate
fallback.

Both cases then trace one figure back through `mcp-ledger` before finishing: Case A the
Section 301 duty on the 7501, Case B the re-export value the Art. 16 §2 decision turned on.

---

## 16. The boundary (week 11)

### 16.1 Three pieces

```
templates.py        geometry cells -> named fields, by heading first and index second
RLS + tenancy.py    Postgres decides which rows a connection may see, not the WHERE clause
tenant_offboard.py  sign the ledger, archive it, tombstone the tenant; delete nothing
```

### 16.2 Templates: what geometry deliberately left undone

§15.6 ends by saying `extract_table` returns cells and does not decide which is the duty,
because inferring that from position would put a layout heuristic between an Arabic table
and a figure on a refund claim. `services/extraction/src/templates.py` supplies the
decision rather than deriving it.

A `ColumnSpec` carries three things: the index the column is expected at, the headings it
is expected to carry, and the schema field it becomes. The last of those exists because the
document and the schema speak different languages — a *Bayan* column headed الرسوم is
`duty_amount` on the form and `duty_paid` on `EntryLine`, and collapsing them would hide a
translation that a reader of either side needs to see.

**The heading wins over the index.** `bind` locates each field among the header row's cells
and returns the mapping it actually used. A form revision that inserts a column shifts every
index after it; a template trusting its indices would keep parsing and report the quantity
column as the value — a claim that is internally consistent, reconciles against itself, and
is wrong. A declared field whose heading is nowhere on the page raises `TemplateError`.
Where there is no header row at all, indices are all there is, and the caller has said so by
setting `has_header_row=False`.

**Coercion failures are collected, not raised.** `TemplateResult.issues` carries them and
the row keeps its other fields. A dash struck through one duty cell is a review signal, and
discarding four good columns for it would be worse than the problem.

`spans_for` emits a `ProvenanceSpan` per numeric field, keyed by the schema name, measured
from the cell's own box — which closes the loop opened in §15.2: an Arabic table cell can
now reach `EntryLine` with the rectangle the validator demands.

`BAYAN_LINE_TABLE` is labelled a mock and is one. Its column set comes from the
declaration's published structure (`COMPLIANCE-GCC.md` §5), not from a measured form, and
the clustering constants under it were tuned against a fixture this repository wrote. See
the roadmap's **B3**.

### 16.3 RLS: what the tenant column was not doing

Every tenant-scoped table has carried `tenant_id` since week 2 and every query has filtered
on it. That is not isolation. A filter is a thing a developer remembers, the first query
that forgets is a data breach with a passing test suite, and nothing in the type system
tells the two apart.

Ten tables now carry one `FOR ALL` policy comparing against `app_current_tenant()`.
`refund_lines` and `claim_transitions` have no `tenant_id` of their own and are reached
through their claim; duplicating the column to simplify a policy would create a second place
for the answer to be wrong. `classification_queries` allows NULL, because a corpus query
belongs to nobody. `tariff_lines` and `tariff_rulings` stay outside: the same schedule for
every tenant, and a policy there would cost a join and protect nothing.

One `FOR ALL` policy rather than four per operation. Split policies let SELECT and INSERT
drift apart, and a row a tenant can write but cannot read is a bug that only appears under a
second tenant.

### 16.4 The role is the control

`drawbridge` owns these tables and is a superuser with `BYPASSRLS`. Policies do not apply to
it, and `FORCE ROW LEVEL SECURITY` does not change that — FORCE binds an owner only where
the owner is not a superuser.

So the enforcement is not in the migration. It is in the DSN: the services connect as
`drawbridge_app`, created by `scripts/rls_bootstrap.py` with `NOSUPERUSER NOBYPASSRLS` and
granted exactly the table privileges they need. The bootstrap refuses to finish if the role
it just created could bypass a policy, because a role in that state passes every functional
test in the suite and isolates nothing.

Deliberately outside the boundary, each for a stated reason:

| Path | Connects as | Why |
|---|---|---|
| Alembic migrations | owner | DDL, and the policies themselves |
| `scripts/ingest_tariff.py`, `embed_corpus.py` | owner | shared reference data, no tenant |
| `scripts/tenant_offboard.py` | owner | reads a tenant it is about to make invisible |
| e2e onboarding and teardown | owner | operator actions; a tenant cannot scope to itself before it exists |
| Integration fixtures | owner | build cross-tenant data; `tests/integration/test_rls.py` is the one suite that uses the app role |

### 16.5 Scoping a session

`SET LOCAL "tenant.id"`, written through `set_config(..., is_local => true)` — transaction
scoped, so it cannot survive into the next checkout of a pooled connection, and a bind
parameter rather than interpolated SQL. Unset resolves to NULL, `tenant_id = NULL` is never
true, and an unscoped connection therefore reads nothing. Falling open would be satisfied by
exactly the set of paths that forgot, which is the set this exists to catch.

Four entry points are addressed by an identifier and never see a tenant: `GET /claims/{id}`,
`POST /packaging/build`, `POST /review/{id}/resolve`, `GET /review/pending/{token}` — and
most `mcp-claims` and `mcp-ledger` tools. They resolve the owner first through a
`SECURITY DEFINER` lookup (`app_tenant_of_claim`, `app_tenant_of_review`,
`app_tenant_of_resume_token`), then scope the transaction to it.

That is a hole, so it is worth stating its exact size. Each function returns one uuid and
nothing else, and each pins `search_path` so the definer privilege cannot be aimed at
another schema. What someone learns by guessing a claim UUID is which tenant owns it. The
row stays invisible until the scope is set, and
`test_the_lookup_returns_an_owner_and_nothing_else` pins both halves.

### 16.6 Offboarding, and why RESTRICT was survivable after all

§15.4 said a tenant with ledger rows cannot be deleted and left it there. The procedure is
export, sign, tombstone.

**In that order.** A failed upload leaves a live tenant and no artifact, which is fixed by
running the script again. The reverse order leaves an invisible tenant whose ledger was
never exported — the state the whole mechanism exists to prevent — so the database refuses
it too: `ck_tenant_offboard_is_evidenced` rejects a tombstone lacking the artifact key, the
signature and the public key.

The artifact is the chain as JSON lines, every column including `prev_hash` and
`entry_hash`, so an auditor can recompute it from the file without our database. The
manifest carries the sequence range, the `verify_chain` verdict at the moment of export,
and the SHA-256 of the body; the Ed25519 signature is over the manifest, which is what
makes it cover the body too. A signature made after the relationship ended would prove
much less, which is why this runs at offboarding rather than on request.

The private key comes from `DRAWBRIDGE_OFFBOARD_SIGNING_KEY` and the script refuses to
generate one. A key that existed for the duration of a single process produces artifacts
that look signed and verify against nothing.

**The tombstone is one predicate.** `app_current_tenant()` resolves the GUC through the
`tenants` table and returns NULL once `offboarded_at` is set, so an offboarded tenant's
documents, claims, ledger and queue leave every scoped query at once — without ten policies
having to remember. The rows stay exactly where 19 CFR §163 and GCC Art. 175 require them.

### 16.7 What this does not do

- **Authenticate.** RLS answers which rows a connection may see. Nothing here answers who
  the caller is; that is Authentik, and it is the other half of onboarding a second tenant.
  Week 12 supplies it — see §17.1 for exactly where the two join.
- **Protect against the owner.** Stated rather than mitigated — see 16.4. Anyone holding
  the owner DSN reads every tenant, and the answer to that is secret management, not SQL.
- **Verify an archived artifact from anywhere.** `verify_artifact` is written and tested and
  is not exposed to an auditor.
- **Assemble a declaration from a template.** Typed cells and their provenance, yes; a whole
  `EntryLine` off a *Bayan*, not yet — that needs a header block and a real form.

---

## 17. The caller (week 12)

### 17.1 Where this attaches

§16.7 says row-level security answers which rows a connection may see and that nothing
answers who the caller is. This is that, and it joins the week 11 machinery at one point:

```
Authentik / local issuer   who
   -> JWT tenant_id claim  which tenant
      -> set_tenant()      SET LOCAL tenant.id
         -> RLS policy     which rows          (unchanged from week 11)
```

Before this, the value in `tenant.id` came out of the request body. The policies were
comparing every row against a number the caller supplied, which is isolation from a client
that fills in the form honestly and nothing at all from one that edits a field. Postgres
cannot see the difference: both look like a correctly scoped connection.

### 17.2 The middleware, and what it deliberately does not do

`services/api/src/auth.py` verifies the bearer token and puts a `Principal` on a context
variable for the duration of the request. It does **not** run `SET LOCAL tenant.id`.

A request here does not hold one connection. It opens a session per unit of work — some
async on `app.state`, some synchronous in a worker thread — and `SET LOCAL` is
transaction-scoped by design, because a value set outside a transaction survives the
connection's return to the pool and arrives on somebody else's next request. So the scope
statement stays in `sync_session` and `set_tenant_async`, where the transaction is. What
changed is where those callers get the tenant: `auth.authorise_tenant(body.tenant_id)`
rather than `body.tenant_id`.

A context variable rather than `request.state`, because the code that needs the answer is
several layers down — `sync_session`, the packager, the agent worker — and threading a
`Request` through them would put a web framework in the signature of the claim state
machine.

### 17.3 Two kinds of principal

| | tenant claim | `drawbridge:service` | may act for | `expected_tenant()` |
|---|---|---|---|---|
| User | required | absent | its own tenant only | that tenant |
| Service | forbidden | required | any tenant it names | None |

A token carrying both is refused at `decode`. Resolving it either way would make "may this
caller act for tenant X" depend on which field the reader looked at first, and there are two
readers.

The service principal exists because n8n runs one workflow against whichever tenant its
trigger names, and is addressed by claim id four times in a single run. It is a cross-tenant
credential and the most valuable secret in a deployment. Stated rather than mitigated — the
same posture as §16.4 takes toward the owner DSN.

### 17.4 Identifier-addressed routes

§16.5 listed four entry points that hold an identifier and no tenant, gave each a
`SECURITY DEFINER` owner lookup, and recorded that this made a claim id sufficient to read a
claim. `expected_tenant()` supplies the constraint: `tenancy._require` compares the resolved
owner against the token's tenant and raises `TenantScopeError` on a mismatch, which the
routes already report as a 404.

The same 404 a nonexistent id gets, with the same body. Distinguishing "not yours" from
"does not exist" turns a guessed uuid into a membership oracle, and the caller cannot act on
the difference.

### 17.5 Authorise before the side effect

`/documents/batch` and `/extraction/run` call `authorise_tenant` as their first statement,
before `store.put` or any read. Both touch MinIO under the tenant's prefix, and a check that
runs after the object is written refuses the request while leaving the object behind under
someone else's key.

The general form: the tenant check belongs before the first effect, not before the first
database write. Being inside the right function is not the same as being in the right place.

### 17.6 Tracing

`services/api/src/telemetry.py` installs one provider per process. Every service configures
one — the API at import, each MCP server in `main()`, the agent through the module it
imports.

**The exporter is optional and the tracing is not.** Without one, spans are created,
sampled and dropped. The trace id still reaches `audit_ledger`, so the recordkeeping value
does not depend on an observability container being up, and an API that refuses to start
because Jaeger is down has traded a real dependency for an imaginary one. The import is
lazy and its absence is logged.

Instrumentation is installed at module scope and last. Starlette builds its middleware
stack once, so `instrument_app` in a lifespan hook adds a middleware to a list nothing reads
again; last means the OpenTelemetry middleware is outermost, so a request rejected by auth
still produces a span.

`audit_ledger.trace_id` is stamped by `ledger.record` and is **not** part of `entry_hash`.
The digest covers what the row asserts; a trace id says where to look for how it happened.
Hashing it would break verification of artifacts exported before the column existed, and
would make a retry of one logical event read as a rewrite. The append-only triggers refuse
`UPDATE`, so the id can only be written by the `INSERT` that creates the row.

Not yet propagated across the MCP transport: an analyst tool call is its own trace.

### 17.7 What this does not do

- **Authenticate against Authentik.** The containers are in compose behind an `identity`
  profile and the API verifies RS256 against a JWKS URL, but no provider or property mapping
  is scripted and no RS256 token has ever reached this API. The exercised path is local
  HS256. The API is shaped to accept Authentik; it has not met it.
- **Manage a secret.** `DRAWBRIDGE_JWT_SECRET`, `DRAWBRIDGE_SERVICE_TOKEN` and
  `DRAWBRIDGE_APP_DB_PASSWORD` are environment variables with development defaults.
- **Authorise anything but the tenant.** There are no roles: every user principal for a
  tenant can do everything to that tenant. An analyst and a read-only auditor are the same
  caller.
- **Revoke or rotate.** Tokens expire and nothing refreshes or revokes them.
- **Say who the caller *is* to a customs authority.** The token names a tenant; nothing
  named the claimant. Week 13 supplies it — see §18.1.

---

## 18. The claimant, the secret and the corpus (week 13)

### 18.1 Filing identity is a row, not a request field

`POST /packaging/build` took the claimant in its body until week 13: the caller told the
packager what to print on a document addressed to CBP or ZATCA. `claimant` is now optional
and resolves from `tenant_profiles` — four identifiers, each load-bearing in a different
way.

| | Jurisdiction | What it decides | Validated by |
|---|---|---|---|
| `ein` | US | claimant of record on a 7551 | format, stored without its hyphen |
| `broker_code` | US | the licensed filer who transmits | three alphanumerics |
| `cr_number` | KSA | the establishment ZATCA holds responsible | ten digits |
| `iban` | KSA | **where the money lands** | ISO 13616 mod-97 |

The IBAN is the only one checksummed, because it is the only one whose error moves cash to
a stranger: a transposed pair of digits is still a well-formed IBAN and only mod-97 sees
it. None of these checks is authoritative. Mod-97 proves the number was not mistyped, not
that the account exists or belongs to this tenant; the field docs say so, because a caller
reading "validated" as "verified" skips the confirmation that matters.

An EIN is stored as nine digits and printed as `95-4417293`. Two spellings of one
identifier is how a tenant acquires two identities.

**Sufficiency is per jurisdiction and decided at build time.** `require_for` runs when a
packet is being rendered — the first moment the answer is both knowable and actionable. A
US-only tenant is never asked for a CR number, and `broker_code` is never required at all,
because a self-filer has none and a placeholder there names a broker who does not exist.
Missing fields come back as one 422 listing all of them; one round trip per missing field
is how onboarding takes a week.

**A separate table from `tenants`, because the lifetimes are opposite.** §16.6's tombstone
outlives the commercial relationship by years. This row is what a departing tenant is
entitled to have erased, and `ON DELETE CASCADE` erases it without touching the tombstone.
It carries the same RLS policy as every other tenant table — added to `TENANT_PREDICATES`,
not to a second list.

### 18.2 Secrets: what moved, and what that is worth

`services/api/src/secrets.py`. Resolution order is init > environment > `.secrets.json` >
`.env`. Environment beats the file so an orchestrator can override a stale one; the file
beats `.env` because that is the migration the module exists to perform.

It does not make a secret secret from anyone who can read the file. What it removes is
enumerable: `docker inspect` and `docker compose config` no longer print them, child
processes no longer inherit them, `/proc/<pid>/environ` no longer carries them, and
crash-reporter environment blocks no longer collect them. What it adds is one place to
rotate and a `SecretProvider` seam. `AwsSecretsManagerProvider` is written as a working
shape whose `load` refuses and names what it would need — not a stub returning empty,
which would let a deployment start with nothing configured and fail at the first request
instead of at startup.

`check_secret_posture` runs beside `check_auth_configuration` in the lifespan and
**refuses** outside development when a known placeholder is load-bearing, including one
left inline in the DSN. Inside development it logs the field names. Same posture as
`rls_bootstrap` toward a bypassing role: a control that is present and inert is worse than
one that is absent, because it looks finished.

Two secrets the generator will not mint. An Anthropic key is issued by Anthropic; a
service token is a JWT signed with `jwt_secret` and cannot precede it. Minting either
would report a configured credential and move the failure to first use.

n8n, Authentik and MinIO read credentials from the environment and cannot be taught to
read a file, so `make up` bridges exactly those into the compose invocation's own
environment. `manage_secrets.py env` refuses a terminal; a pipe is the only correct use.

### 18.3 What gets embedded is not what gets read

`description_en` is the ancestor chain root-first, because a line whose own text reads
"Other" means nothing alone. That is the right string for a person and the wrong one for
an embedding, and only the real schedule could show it: for 8471.30.01.00 the chain is 240
characters of which the first 190 are the chapter heading, shared verbatim by every line
under heading 8471. Mean pooling averages the twenty distinguishing characters into
nothing, so siblings collapse onto each other. Measured: 1 of 10 benchmark subheadings
retrieved in the top 10, with machine-tool lines as the nearest neighbours for "ruggedised
field laptop computer".

`search_text` (migration f2b90d47ac13) holds the same chain leaf-first, trailing colons
stripped, capped at 200 characters, and is what `embed_corpus` embeds. Null falls back to
the description, which is correct for ZATCA: one leaf per row, no hierarchy, nothing to
dilute.

```
description_en   Automatic data processing machines and units thereof; magnetic or
                 optical readers, ... , Portable automatic data processing machines,
                 weighing not more than 10 kg      <- what an analyst reads
search_text      Portable automatic data processing machines, weighing not more than
                 10 kg, consisting of at least ...  <- what the model sees
```

The cap is a measurement. At 120 the immediate parent of 8471.41.01.50 is five characters
too long to sit beside a leaf reading "Other", so the line embeds as the word "Other" and
sits 0.868 from a plain-language query; at 200 the parent fits and it is 0.592.

Re-embedding all 28,899 lines against it took recall at hs6 from 1 of 10 to 5 of 10 in the
top ten, and from 0 to 4 at rank 1. Better, and not yet a working classifier.

### 18.4 Six digits is what a description can decide

`scripts/calibrate_thresholds.py` scores retrieval at hs6, not at the ten-digit line. The
published schedule splits 0901.21 eight ways on organic certification, variety and
container size — facts absent from "roasted cofee beans, not decafinated". Scoring a
plain-language query against a statistical suffix measures the model on information the
query does not contain. Six digits is also the internationally harmonised level, which is
why `tariff_lines.hs6` is the cross-jurisdiction join key (§5).

Neither `vector_ceiling` nor `CONFIRMATION_LEXICAL_FLOOR` changed in week 13, and the
measurement is why. After the re-embed the worst correct answer sits at distance 0.475 and
the one query that is not a good at all sits at 0.492. **No ceiling lives in seventeen
thousandths.** §8 said a threshold could not separate adjacent subheadings; this is the
same conclusion reached from the other direction, against real data. Precision is decided
by `needs_analyst_confirmation`; the ceiling only bounds how much noise a human reads.

Both constants are still the week 8 numbers, measured against twenty-four lines, and both
are still labelled as such in the source.

### 18.5 Trace context across MCP

Nothing in this repository propagates it, and nothing needs to. The SDK's client
dispatcher injects W3C context into the JSON-RPC `_meta` (SEP-414) and
`OpenTelemetryMiddleware` — installed by default and outermost on every server — extracts
it. Verified live: a tool call under a client span produced `drawbridge-mcp-hts` spans
carrying the client's trace id.

`tests/integration/test_trace_propagation.py` pins it, because a property nobody wrote is
a property nobody notices losing. What is still ours is configuring a provider in each
server's `main()`: without one the middleware runs and its spans are dropped, which looks
exactly like working instrumentation until somebody reads Jaeger — the same failure
§17.6 records from week 12.

### 18.6 What this does not do

- **Authenticate against Authentik.** Unchanged from §17.7. No RS256 token has reached
  this API. *Closed in week 14 — see §19.1.*
- **Encrypt a secret.** `.secrets.json` is plaintext; the manager seam is unimplemented.
  *Closed in week 14 — Vault and Secrets Manager are implemented, §19.2.*
- **Carry the ruling corpus.** `tariff_rulings` is empty. CBP publishes CROSS through a
  search interface with no bulk export, so `find_rulings` has nothing to cite. *Partly
  closed in week 14: a 120-ruling sample is loaded, and loading it showed that
  `search_rulings` had never returned a row — §19.3.*
- **File anything.** Both pilot corpora are fiction. Every figure carries a
  `pilot-fixture` box tracing to no document, and `assert_not_evidence` refuses to act on
  one.

---

## 19. The deployment (week 14)

### 19.1 Identity is real now

Weeks 12 and 13 both closed with the same sentence: no RS256 token has ever reached this
API. `infra/authentik_bootstrap.py` closes it, declaratively — every step reads the current
state and makes only the change that is missing, so a second run is a no-op and a run
against a half-built configuration finishes it.

| Built | Why it is the load-bearing part |
|---|---|
| RSA signing keypair | Under HS256 the verifier and the issuer share one secret, so the service that checks tokens can also forge them. RS256 means the API holds only a public key. |
| Scope property mapping | Emits `tenant_id`. `tenancy.set_tenant` writes it into `tenant.id` and every RLS policy compares against it, so this mapping *is* the join between the directory and the database boundary. |
| OAuth2 provider + application | `redirect_uris` empty. Client credentials has nowhere to redirect to, and a permissive URI on a provider that never uses one is an open redirect waiting for the code flow to be enabled. |
| Service account with `tenant_id` | So the round trip needs no browser and exercises the machine-to-machine path n8n and the worker will use. |

The mapping reads the claim from the user record rather than emitting a constant, because a
constant would give every user in the directory the same tenant. A user without the
attribute gets a token without the claim and `auth.decode` refuses it — correct: a caller
whose tenant nobody has decided must fail at the door, not at a policy that would return
zero rows and look like an empty account.

Verification runs **through `services.api.src.auth.decode`**, the function every request
runs. A bootstrap that re-verified with its own `jwt.decode` would prove PyJWT works.
`--api-url` goes further and sends the token to the deployed API.

**The negative half is the half that matters.** The same claims re-signed HS256 with the
shared secret this service used until an OIDC URL was configured must come back 401. If
they do not, the JWKS path was added *beside* the shared secret rather than in place of it,
and every value in `.secrets.json` is still a token-minting key. Observed: `RS256 -> 200`,
`HS256 -> 401 no verification key`.

One compose defect surfaced: `DRAWBRIDGE_JWT_ISSUER` was hard-coded to `drawbridge`. That
was fine while `make token` was the only issuer and wrong the instant a real one appeared —
`iss` is the provider's own URL and the API has to be told what it will be.

### 19.2 Three backends, and only one at a time

`AwsSecretsManagerProvider` was a seam in week 13 whose `load` refused. It is implemented,
and so is `VaultSecretProvider` — KV v2 over AppRole, through `httpx`, because the read is
one GET and the login one POST and a client library for two endpoints is a supply-chain
edge bought for nothing.

```
DRAWBRIDGE_SECRETS_PROVIDER = file | vault | aws
default_providers() -> (EnvSecretProvider(), BACKENDS[name]())
```

**Exactly one backend, never a chain.** Naming Vault removes the file entirely. A fallback
to disk when Vault is unreachable would start, work, and be running on whatever stale
plaintext was last checked out — the failure this module exists to remove, reintroduced as
a convenience. An unrecognised backend name is fatal for the same reason:
`PROVIDER=valut` quietly reading the local file is a deployment that is not using the
manager anyone believes it is using.

AppRole rather than a token, because `VAULT_TOKEN` in an environment variable is `.env`
with a better name. AppRole splits the credential: `role_id` is configuration and may sit
in a compose file, `secret_id` is short-lived and injected at start, and what the process
ends up holding is a token Vault issued with its own TTL and its own audit trail. The
policy grants **read on one path** and withholds `list`, which reads as harmless and is
not — a token that can enumerate a mount turns one leaked credential into a plan.

Proved rather than asserted: the API container was restarted against Vault, logged in with
the AppRole and resolved every secret from `secret/drawbridge`. The `.secrets.json` mount
was still in the container and still unread — a value written only into Vault came back
from `resolve()`.

### 19.3 Ruling search had never returned a row

`search_rulings` scored with `similarity(subject || ' ' || body, :q)`. `similarity` is
set-symmetric — shared trigrams over the union of both sides — so a four-word query against
a four-thousand-word ruling is dominated by the denominator. Against the loaded CROSS
sample the best score any query reached was **0.127**, under a `LEXICAL_FLOOR` of 0.15:
zero rows, for every query, since week 3. Three fixture rulings with two-sentence bodies
hid it entirely.

```
score = greatest(
    word_similarity(:q, subject),
    0.6 * word_similarity(:q, left(body, 4000))
)
```

`word_similarity` scores the query against the best-matching *extent* of the document
rather than against the whole of it, which is the shape this problem actually has. Over
subjects it separates cleanly: twelve goods queries each retrieved their own ruling at rank
1, four non-goods queries topped out at 0.314. The body is scored because a CROSS subject
is not always descriptive — a protest ruling is titled "Application for further review of
protest number 1601-..." and names its goods only in the text — and it is discounted
because a long document can always find some matching extent: the non-goods queries reach
0.425 against bodies and 0.056 against subjects.

`LEXICAL_FLOOR` did not move. Paraphrased goods queries land between 0.20 and 0.25 and the
non-goods queries reach 0.314; the ranges overlap and no floor separates them. Same finding
as §18.4 and the same response: `find_rulings` returns candidates for a person to read, and
tuning the floor until the overlap disappeared would tune it until real matches did too.

Second week running that real data broke something a fixture had been certifying — the
embedding text last week, the lexical scorer this week. Both were sized against corpora too
small for the defect to express itself.

### 19.4 White label is a representation, not a logo

The preparer notice on a 7551 says who produced the document and what they may do with it.
Ours says *"Drawbridge is not a customs broker and does not transmit to CBP"*, which is
true of us and is the sentence §5 turns on. It is false on a licensed broker's form.

`services/packager/src/branding.py` composes the notice from what is true of the deployer
rather than substituting a name into ours. `is_licensed_broker` selects the second
sentence, and a deployment that sets it must supply the filer code that makes the claim
checkable — `Preparer` refuses construction otherwise, because an unverifiable claim of
licensure on a customs filing is worse than none. A deployment that renamed the preparer
and kept our disclaimer would be worse than one that changed nothing, because it would read
as deliberate.

Not brandable: the certifications, the statutory citations, the form titles, and the "not a
CBP-issued form" line. Those are the authority's words or facts about the document, and a
deployment able to edit them could quietly weaken a declaration somebody signs.

`preparer` rides on `PacketRequest`, not on configuration read inside the renderer, so a
packet regenerated in four years reproduces the notice that was on it.

### 19.5 The on-prem stack

`docker-compose.onprem.yml` is **standalone**. An overlay would be shorter and would be a
trap: `-f a.yml -f b.yml` merges rather than replaces, so every bind mount, published port
and `--reload` survives, and forgetting one `-f` deploys development under a production
name.

| | Development | On-prem |
|---|---|---|
| Published ports | 11 services | one: Caddy, TLS |
| Source | bind-mounted, `--reload` | baked into the image |
| Secrets | `.secrets.json` | Vault, no fallback |
| Identity | HS256 shared secret | Authentik RS256, no fallback |
| Data plane egress | full | none — `internal: true` |
| Migrations | by hand | a job the API waits on |
| Capabilities | default | `cap_drop: [ALL]`, `no-new-privileges`, `read_only` |

The `internal: true` network is the cheapest meaningful control here. Postgres, Redis,
MinIO, Jaeger and all five MCP servers have no default gateway and cannot originate
outbound traffic at all — none of them has any use for it, and the database cannot phone
home.

Six credentials still arrive as files under `./secrets`, because Postgres, MinIO and n8n
read a `_FILE` variant and cannot be taught to call a manager. That is two copies of each
secret. `secrets/README.md` says so and names the fix (a `vault agent` sidecar templating
them at start), which is not built.

### 19.6 The agent finally has a process

`services/agent/src/queue.py` could draft memos from week 8. Nothing ran it — no
entrypoint, which is why "a live agent run against real queue rows" sat on the roadmap for
six weeks while the code that would do it was passing its own tests.
`services/agent/src/worker.py` is a loop around `draft_pending` and nothing else.

It polls, on purpose. `LISTEN`/`NOTIFY` is fewer wasted queries and one more thing to lose
silently: a dropped notification is a memo that never appears and no evidence anything
happened. The consumer is a person arriving minutes to hours later, so the interval is
minutes and a poll that finds nothing is one indexed query.

It runs unscoped — `tenant_id=None`, drafting across every tenant — which is the same
posture as the service token n8n carries and for the same reason: one worker serves
whichever tenants have queued work. It is therefore the second cross-tenant process in the
deployment, and the on-prem stack gives it its own credential so the blast radius is
something somebody can revoke.

### 19.7 What this still does not do

- **Back anything up.** `postgres-data` is a volume on one host. The retention obligation
  is years and a volume is not a backup. The largest remaining hole. *Closed in week 15 —
  object-locked `pg_dump`, §20.4.*
- **Classify.** 5 of 10 at hs6, unchanged. Nothing this week touched that path. *Week 15
  found why: the corpus was embedded from text no query resembles — §20.1.*
- **Carry CROSS.** 120 rulings drawn round-robin across twelve terms. A sample is not the
  corpus, and `scripts/ingest_cross.py` says so in its own docstring.
- **Run n8n for real.** Carried from weeks 9–13. *Closed in week 15, and it took seven
  fixes to get there — §20.6.*
- **File anything.** Both corpora are fiction; `assert_not_evidence` refuses to act on a
  `pilot-fixture` box.

---

## 20. What running it found (week 15)

Week 15 set out to fix retrieval, add a Vault Agent sidecar, take backups, pin digests and
run n8n for real. All five happened. But the week has one finding underneath it, and it is
the same finding five times:

> Every defect below was invisible to a check that passed. The embedding text was valid
> text. The workflow files were valid JSON. The image tags were valid tags. The secret was
> a valid secret. Each artefact was being verified for **form** and never for **use**, and
> each survived for between three and seven weeks because the thing that would have caught
> it — running it — is exactly what a roadmap item defers.

### 20.1 The corpus and the queries were never in the same space

The oldest number in this repository is *5 of 10 at hs6*. Week 13 measured it, week 14
carried it, and the week 15 entry checklist said the model was now the suspect: "a
384-dimension multilingual MiniLM over 29,000 near-identical legal phrases is a thin
representation".

It is not the model. `scripts/embed_corpus.py` embedded `f"{code} {body}"`.

Every one of the 28,899 US document vectors began with its own ten-digit tariff code — a
token no analyst query has ever contained. The document side and the query side were in
measurably different distributions, and had been since week 8.

```
stored vector vs. embed(code + " " + text)   cosine 1.000000
stored vector vs. embed(text)                cosine 0.950
```

The intent was reasonable and is stated in the script's own docstring: a query like
"8471.30 portable machines" should reach the line "by either half". It does not work that
way. A query naming a code is a *lookup*, and hoping a subword tokeniser puts `8471300100`
near `8471.30` is both worse retrieval and an answer nobody can defend in an audit.
`search_tariff` now matches the digits (`search.py::code_prefix`), and a code hit is the
one hit that does not need analyst confirmation — nothing was inferred.

**And the benchmark had the same defect, which is why it survived.**
`tests/golden/test_tariff_benchmark.py` embedded its fixture corpus as
`f"{code} {description}"` too, under a docstring saying — correctly — that a benchmark
embedding its corpus differently from production would measure a threshold nothing else
uses. Both sides agreed, and both were wrong in the same way. The one test whose job is to
catch a mismatch between the corpus and the queries was built to match.

**Fixing the text did not fix retrieval, and the direction of the change says so.**
Re-measured over the fixture corpus with the prefix removed:

| | code + description | description alone |
|---|---|---|
| Worst true positive | 0.625 | **0.635** (further) |
| Nearest hard negative | 0.508 | **0.497** (closer) |

The overlap got *wider*. The convenient reading of week 15 is that a string bug was hiding
a working classifier; it is not supported. What the fix bought is that every measurement
from here is over a corpus the queries can actually reach — the numbers before it were
unsound, not merely worse. `vector_ceiling` at 0.68 still clears the worst positive with
0.045 of headroom, and the finding this file has asserted since week 9 stands: the two
ranges overlap, so no single distance threshold delivers precision at any value.

**And the full-corpus number, after the re-embed converged.** 28,899 lines, all on the
`.../desc` convention, `stale: 0`:

| | code + description | description alone |
|---|---|---|
| Retrieved in top 10 | 5/10 | **6/10** |
| At rank 1 | 4/10 | **3/10** |
| Worst top-hit distance | 0.475 | 0.529 |
| Nearest non-good | 0.492 | 0.467 |

One better on recall at ten, one worse at rank one, on a ten-query benchmark — which is to
say: **no measurable improvement.** And the two ranges have now crossed. Before the fix the
worst true positive (0.475) sat *nearer* than the nearest non-good (0.492), which looked
like a threshold might separate them. After it the worst positive is at 0.529 and the noise
at 0.467, so the non-good is closer than the answer. `vector_ceiling` at 0.68 admits both.

That is the same result the fixture corpus gave, at 1,200× the scale, and it is worth being
blunt about: seven weeks of "5 of 10" was measured over a corpus the queries could not
reach, and correcting that did not move the number. The defect was real and had to be
fixed — every measurement before it was unsound — but the classifier's problem was never
the code prefix. `2 of 10 typo queries` and `1 of 2 Arabic queries` retrieved at all is
what a 384-dimension multilingual encoder over 29,000 near-identical legal phrases does,
and week 16 gets to test that claim against measurements that finally mean something.

**What the model measurements were worth.** Before the cause was found, two first-stage
replacements and two rerankers were measured. Recording them because they are the reason
the retrieval work stopped where it did rather than continuing into a model swap:

| Approach | rank-1 | top-10 | Cost |
|---|---|---|---|
| Baseline (multilingual MiniLM) | 2/10 | 5/10 | — |
| `+ Xenova/ms-marco-MiniLM-L-6-v2` rerank @200 | 1/10 | 5/10 | 1.1 s/query |
| `+ jinaai/jina-reranker-v2-base-multilingual` @200 | 1/10 | 6/10 | 4.2 s/query, 1.1 GB |

The English cross-encoder is *worse*, and it is worse in the way that matters: the Arabic
smartphone query went from rank 1 to rank 12. A bilingual corpus needs a bilingual
reranker, and the bilingual one buys one position out of ten for four seconds a query. So
no reranker ships. That was measured against a corpus in the wrong space, which makes the
comparison less useful than it looks — but it is the reason to fix the space before buying
a model, and the numbers are here so the next attempt starts from evidence.

### 20.2 The invariant that had no column

`services/classifier/src/embeddings.py` has said since week 8:

> Every backend records `model_id` on the rows it writes. A corpus is queryable only by the
> backend that wrote it, and mixing them is a data error the ingest can detect.

There was no column. Nothing recorded it, nothing detected anything, and the sentence
described an intention. Seven weeks later the corpus turned out to be unreachable by its
own queries and there was no way to ask a row what had produced it — the diagnosis took a
cosine comparison against three candidate texts.

`tariff_lines.embedding_model_id` and `tariff_rulings.embedding_model_id` exist now
(`a3f81c22d907`), and the stamp names the **text convention** as well as the model:

```
fastembed:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/desc
```

Because the failure that happened was one model over two texts. A column holding only
`model_id` would have recorded the same string for the broken corpus and the fixed one and
caught nothing. `embed_corpus.py --reembed` selects rows whose stamp is not the current
convention and overwrites them in place, so the corpus converges rather than emptying and
search is never dark. The migration is additive for the same reason: nulling 29,000 vectors
would have taken classification offline for the length of a re-embed.

### 20.3 Vault Agent, and the split worth preserving

Six credentials reached Postgres, MinIO and n8n as plaintext files under `./secrets`,
because those three images read a `_FILE` variant and cannot call a manager. That directory
is gone. `vault-agent` logs in with its own AppRole and renders the six into four tmpfs
volumes, one per consumer group.

The part worth stating is not the sidecar, it is the **path split**. The API reads
`secret/drawbridge`; vault-agent reads `secret/drawbridge-infra`; the policies are
disjoint. That separation existed by accident before — the API could not read the Postgres
owner password or the n8n encryption key because they were not in `.secrets.json` — and a
move into a manager that merged the two would have been a downgrade with a better name.

Verified against a live Vault, from inside a container:

```
8/8 files rendered, no <no value> holes, no trailing newline
same postgres_password to postgres, authentik and n8n     sha256 27cb9a1073749f46
/run/vault-agent empty                                    no token on a shared volume
infra role -> secret/drawbridge                           denied
infra role -> list on the mount                           denied
```

One credential still arrives from outside — the AppRole's own `secret_id`, which is the
chicken-and-egg every secrets manager has. It comes from the environment as a Docker
secret sourced with `environment:` rather than `file:`, so the directory does not need to
exist at all.

**No template declares a `command`.** Postgres reads its password once at initdb; MinIO
reads its root credentials at start. A template that rewrote the file and signalled the
process would change the file and not the credential in force, and a rendered file that
disagrees with what the service is using is worse than a stale one you know is stale.

### 20.4 Backups, and what "immutable" costs

`scripts/retention.py` takes a `pg_dump` into MinIO under **S3 object lock, COMPLIANCE
mode**, and recomputes every tenant's ledger hash chain on its own interval.

COMPLIANCE and not GOVERNANCE, because GOVERNANCE can be lifted by anyone holding
`s3:BypassGovernanceRetention` — on a single-tenant on-prem MinIO, the operator. A
retention control the operator can lift is a retention policy. The threat is not disk
failure; it is a dispute in year four in which the party holding the records is also the
party who could have changed them.

The cost is real and is the point: **nothing prunes.** Storage grows monotonically for five
years. Proven live — a 52 MB dump written, then attacked with root credentials:

```
mc retention info      COMPLIANCE, expiring in 29 days
mc rb --force          Failed ... is WORM protected and cannot be overwritten
mc retention set 1d    Unable to find any object/version to set its retention
```

**And one thing the probe found that the design had not.** `mc rm` on a locked object
*succeeds*: it writes a delete marker. The protected version survives underneath and cannot
be removed, but the object vanishes from an ordinary listing. Object lock protects the
bytes and says nothing about visibility. So the failure to guard against is not losing a
backup — it is looking for backups during an incident, seeing an empty bucket, and
concluding there are none. `retention.py catalogue` enumerates *versions* and names any key
a delete marker is masking.

Verification does not write to the ledger. Recording the result as an `audit_ledger` event
would extend the chain being verified and put the attestation inside the structure it
attests to; an altered ledger would carry an altered record of having been checked.

### 20.5 Digests

Every third-party image in the on-prem stack carries a SHA-256 digest.
`infra/pin_images.py` resolves them against the registry — `buildx imagetools inspect`, not
`docker image inspect`, because the latter reports what happens to be in the local cache.
`make pin-check` is the CI form and fails on drift.

The tag stays beside the digest. Docker resolves the digest and ignores the tag, so the tag
is documentation and is not load-bearing — which is the point: `redis@sha256:ff02b5...`
alone tells a reviewer nothing, and a digest nobody can read is a digest nobody checks.

Images this repository builds are *not* pinned. They carry `${DRAWBRIDGE_VERSION}` and come
from the same commit that deploys them; a digest would pin them to whenever somebody last
ran the script.

What this does not give you is provenance. A digest says the bytes have not changed since
somebody wrote it down, not that they were trustworthy then. Signature verification is the
control that answers that and it is not here.

### 20.6 n8n ran, and seven things were wrong with it

"Import the workflows into n8n and run one for real" has been on the roadmap since week 9.
It is done: a webhook POST produced a claim in `packaged`, a refund of `25092.14`, and a
13 KB CBP 7551.

```
POST /webhook/drawbridge/ingest                                    200
  documents/batch 201 · extraction 200 · classification 200 · matching 200
  triage 200 · claims/persist 201 · claims/transition 200
  packaging/build 200 · claims/transition 200
{"state":"packaged","refund":"25092.14","transmittable":true,
 "artifacts":[{"filename":"cbp7551-....pdf","bytes":13345}]}
```

Getting there took seven fixes, and none of them was findable without running it:

| # | Defect | Effect |
|---|---|---|
| 1 | No `id` on any workflow | `null value in column "id"` — import failed outright |
| 2 | All three carried the tag `drawbridge` | `tag_entity.name` is unique; batch import failed on the second file |
| 3 | The `Authorization` header was in the JSON but not in the generator | regenerating strips the bearer token from all twelve API calls |
| 4 | `N8N_BLOCK_ENV_ACCESS_IN_NODE` unset | "access to env vars denied" — every call goes out with an empty token |
| 5 | `Validate Payload` read `$json`, not `$json.body` | the webhook hands on the whole request; every field looked missing |
| 6 | `errorWorkflow` referenced by name | n8n resolves it as an id; the error handler never ran |
| 7 | `$('Node').item` throughout, and `workflow_run_id: $execution.id` | see below |

Number 7 is the one worth reading twice. `$('Node').item` resolves through n8n's *item
pairing*, which this pipeline loses at every Code node that builds a fresh array. When it
fails it does not raise — it yields `undefined`, `JSON.stringify` drops the key, and the
request goes out **missing a field rather than carrying a wrong one**. `/review/suspend`
returned 422 "Field required: body.tenant_id" against a payload whose `tenant_id` was
present three nodes upstream. Every node in this pipeline emits exactly one item, so
`.first()` is not a workaround for the pairing — it is the accessor that matches what these
nodes produce. Alongside it, `workflow_run_id: $execution.id` sent an integer into a
`str | None` field and Pydantic v2 correctly refused it, on the one node whose whole job is
to record that a run needs a human.

Number 3 deserves a note of its own. Week 12 added the service-token header to the three
generated JSON files by hand and did not touch `n8n/generate_workflows.py`. It survived
three commits because nobody ran the generator. The fix is not the missing lines — it is
that they were per-node lines at all; the header is applied once in `http()` now, where it
cannot be forgotten from one node.

`tests/unit/test_week15.py` pins all seven against the generated files, including a test
that runs the generator and asserts it reproduces what is committed.

### 20.7 Two more, found on the way

**MinIO was unreachable from the API, in development, for weeks.** `s3_secret_key` is in
`GENERATED_FIELDS`, so `make secrets-init` mints a random one — while `docker-compose.yml`
hardcoded MinIO's root password as `drawbridge` and had no way to learn the new value. The
two disagreed by construction from the moment a secrets file was generated, and every
document write failed with a bare 403 from `HeadObject`. It survived because nothing in the
suite talks to a real MinIO and the pilot runs against fixtures already in the bucket. Both
halves now read the same two variables. On-prem was never affected: both sides read the
same value from Vault.

**The pipeline cannot fail.** Every HTTP node sets `neverError: true`, so a 500 from
`/claims/persist` became `{data: "Internal Server Error"}` and the run continued through
packaging, returned HTTP 200 to the caller, and recorded `success`. There are gates after
extraction and classification and none after persist. Not fixed this week — it is a
structural change to a 22-node graph and it wants doing deliberately. It is the first item
on the week 16 checklist.

### 20.8 The agent worker, and what is still blocked

`worker.py` was run against the live queue. It connects, selects the three open undrafted
rows, and stops at the model boundary reporting `unavailable=3` — correct behaviour, since
`AgentUnavailableError` breaks the batch rather than burning a call per row.

**No `anthropic_api_key` is configured in this deployment, so the drafting itself has still
never run live.** With a stub standing in for the model — everything Drawbridge owns
running for real: the queue select, `build_facts`, the grounding check, the `_ATTACH`
update, the per-row commit — all three rows drafted and committed with `agent_memo`,
`agent_model` and `agent_drafted_at` populated. The stub memos were reverted afterwards.
So the write path is proven and the model call is not, and no amount of further work here
substitutes for a key.

---

## 21. The second stage (week 16)

Week 15 ended with a number that had stopped moving and an untested explanation for it.
Retrieval was 6 of 10 at hs6 in the top ten, the corpus and the queries were finally in
one embedding space, and the standing claim — *a 384-dimension multilingual MiniLM is too
thin for 29,000 near-identical legal phrases* — had been carried since week 14 without
anyone measuring it.

Week 16 measured it, and the claim is half right in a way that changes what to build.

### 21.1 The answer is usually in the pool, and ranked wrong

The measurement nobody had taken is recall at depth. One query, the fifty nearest lines,
and the question of whether the correct subheading is anywhere among them:

| depth | 10 | 25 | **50** | 100 | 200 | 500 |
|---|---|---|---|---|---|---|
| correct hs6 present | 6/10 | 6/10 | **8/10** | 8/10 | 8/10 | 8/10 |

Two of the ten answers live between position 10 and position 50. That is not a
representation too thin to find them — it found them — it is a first stage that cannot
order what it retrieved. Cosine distance between a four-word commercial description and a
tariff leaf is a weak ordering signal over a corpus where thousands of leaves differ by a
qualifier, and it does not become a strong one at any threshold.

It also puts a hard ceiling on the week: **8 of 10 is the most any reranker can score**,
because a candidate the first stage never returns cannot be re-scored. The two it misses —
"desktop tower PC sold with its monitor and keyboard in one unit" and "roasted cofee beans,
not decafinated" — are absent at depth 500, and §21.5 is about why.

### 21.2 What the cross-encoder is, and why it is not an embedder

An `Embedder` maps one text to a point. That is what makes a corpus searchable: 29,000
documents are embedded once, indexed, and every subsequent query is a distance computation
the database can do. The query and the document are never in the same forward pass, which
is exactly the property that makes it cheap and exactly the information that is lost.

A `Reranker` scores a *pair*. It reads "ruggedised field laptop computer" and "Portable
automatic data processing machines, weighing not more than 10 kg..." together, in one pass,
and answers whether the second responds to the first. There is nothing to precompute and
nothing to index: the cost is one model call per candidate, every time. On CPU, about two
seconds for fifty.

That asymmetry is the whole architecture. Retrieval narrows 29,000 to 50 for 20
milliseconds; reranking orders 50 for 2 seconds. Reranking 29,000 would take nineteen
hours a query.

### 21.3 The measurement, and the model that did not ship

Three cross-encoders over the corrected corpus, depth 50, against the ten labelled
positives:

| Reranker | rank-1 | top-10 | Cost |
|---|---|---|---|
| none (retrieval alone) | 3/10 | 6/10 | ~20 ms |
| `BAAI/bge-reranker-base` | 2/10 | 6/10 | 2.1 s |
| `jinaai/jina-reranker-v2-base-multilingual` | 3/10 | **8/10** | 2.2 s |
| **jina-v2, blended at 0.70** | **4/10** | **8/10** | 2.2 s |

`bge-reranker-base` is multilingual, a third of the size, and does not work here: it
reorders confidently over tariff text it has no notion of, and rank-1 falls. Being
multilingual is necessary and not sufficient — week 15 had already established the
necessary half by watching an English cross-encoder move the Arabic smartphone query from
rank 1 to rank 12.

**And the blend is the finding, not the reranker.** Taking the cross-encoder's order
outright scores 3 of 10 at rank one — no better than retrieval — because it demotes two
queries the first stage already had right while rescuing two it did not. Blending
`0.70 · confidence(logit) + 0.30 · retrieval_score` keeps both:

| w | 0.0 | 0.4 | 0.5 | 0.7 | 0.85 | 1.0 |
|---|---|---|---|---|---|---|
| rank-1 | 3/10 | 4/10 | 4/10 | **4/10** | 4/10 | 3/10 |
| top-10 | 6/10 | 7/10 | 8/10 | **8/10** | 8/10 | 8/10 |

The plateau from 0.50 to 0.85 matters more than the value at its centre. A constant that
only works at one setting has been fitted to ten queries; this one survives a 70% change
in its own value.

The two signals are blended after `confidence()` squashes the logit, because they are not
otherwise commensurable: cosine similarity is bounded in [0, 1] by construction and a logit
is unbounded in both directions. Weighting an unbounded score against a bounded one does
not produce a weighted average, it produces whichever number was larger.

### 21.4 End to end, against the published schedule

`scripts/calibrate_thresholds.py --rerank`, full 28,899-line HTSA, both stages measured in
the same run so the delta is on the page:

```
POSITIVES — does the right subheading still come back at volume?
  9/10 retrieved in the top 10
  5/10 at rank 1
  one stage was 6/10 in the top 10 and 3/10 at rank 1
  reranked query latency: median 2854 ms, max 8541 ms
```

Nine, not the eight §21.1 capped it at, because production merges the lexical path too and
"roasted cofee beans, not decafinated" — invisible to the vector path at any depth — comes
back through trigram similarity at rank 1. The hybrid design earns its keep on precisely
the query the semantic half cannot see.

Against the number this repository has carried since week 13:

| | wk 13–14 | wk 15 (re-embed) | **wk 16 (two-stage)** |
|---|---|---|---|
| top-10 at hs6 | 5/10 | 6/10 | **9/10** |
| rank-1 at hs6 | 4/10 | 3/10 | **5/10** |

Ten queries is a small set and this is not a claim that classification is solved. It is the
first time the number has moved for a reason that was measured before the change rather
than after it.

### 21.5 What is still wrong, and it is in the corpus

The two queries that miss at depth 500 are one defect. `search_text` is the leaf
description with its ancestor chain flattened onto the front, and the chain is not always
there:

```
8471.49.00  "Other, entered in the form of systems, Other automatic data processing machines"
8471.41.01  "Comprising in the same housing at least a central processing unit and an
             input and output unit, whether or not..."
```

The second line is the one a desktop PC classifies under, and its text never says it is a
computer. Its parent heading — *Other automatic data processing machines* — is present on
its sibling and absent on it. No encoder retrieves that from "desktop tower PC sold with
its monitor and keyboard in one unit", and no reranker can rescue what retrieval cannot
return. 596 of the ten-digit US lines carry a single-segment `search_text`, which is the
same shape of gap at ~3% of the leaves.

That is an ingest defect, not a model one, and fixing it is a re-ingest and a re-embed
rather than a constant. Week 17.

### 21.6 The thresholds did not move, and one of them changed jobs

`vector_ceiling` stays 0.68, `CONFIRMATION_LEXICAL_FLOOR` stays 0.20, `LEXICAL_FLOOR` stays
0.15 — but for the first time there is a measured bound rather than an absence of one.

Under reranking the ceiling stopped being the gate on what an analyst is shown and became
the gate on what the cross-encoder is allowed to consider. A candidate cut here cannot be
rescued, and rescuing distant candidates is what the second stage does: the correct
subheading for "ruggedised field laptop computer" sits at distance **0.601**, position 28
in the shortlist, and comes back at 6. Across the ten positives, 0.601 is the worst correct
candidate anywhere in the depth-50 pool. So 0.68 clears it by 0.079, and **any value below
about 0.61 is now demonstrably wrong** rather than merely tight. Raising it further buys
nothing measured and admits more noise into a stage that costs two seconds a query.

**And the reranker's confidence is not a precision threshold either**, which is worth
recording because it is the obvious next thing to reach for. The one benchmark query that
is genuinely unanswerable — "marine cargo insurance brokerage arranged for a shipper", not
a good, no tariff line for it at any volume — scores the lowest confidence of all twenty
queries at 0.0735. Encouraging, and it does not survive contact with the set: "ruggedised
field laptop computer" is a *positive* and scores 0.0888. Two points 0.015 apart, on
opposite sides of the only question a threshold would be asked. There is no floor there.

### 21.7 A backup that has now been restored

Week 15 shipped `scripts/retention.py`: `pg_dump` into MinIO under COMPLIANCE object lock,
proven undeletable by root, verified against a live bucket. What it had never done was read
one back. The week 16 checklist put it plainly — *a backup nobody has restored is a file* —
and the first run of `retention.py restore` failed in thirty seconds:

```
FATAL:  password authentication failed for user "drawbridge"
```

`str(sqlalchemy.URL)` renders the password as `***`. That is the right default everywhere
except when the string is going to be connected with, and the failure is a URL that reads
correctly in the traceback and cannot connect. Nothing short of running it finds that,
which is the week 15 finding wearing a different hat.

The drill, once it ran:

```json
{"job": "restore", "pg_restore_exit": 0, "pg_restore_warnings": [],
 "rows": {"audit_ledger": 280, "claims": 8, "entry_lines": 8, "tariff_lines": 28908},
 "ledger": {"tenants": 2, "entries": 280, "ok": true, "broken": []},
 "timings": {"download_seconds": 0.74, "restore_seconds": 12.75,
             "count_seconds": 0.04, "verify_seconds": 0.07, "total_seconds": 14.97},
 "ok": true}
```

Four things about its shape:

- **It restores into a scratch database and refuses to restore into the source.** The
  natural way to test a restore is to point it at the database you already have, and doing
  that once replaces production with a copy of itself from last night. The guard is checked
  before anything else runs, and is tested.
- **It checks the bytes against the digest recorded when they were written**, not against
  the object's ETag. An ETag is computed by the same party that stored the object, so
  comparing an object to its own ETag proves the transfer worked and not that the bytes are
  the ones `pg_dump` produced.
- **It recomputes every tenant's hash chain inside the restored copy.** Row counts prove
  `pg_restore` moved data; the chain proves the data that came back is the data that went
  in. A backup that restores a corrupted ledger restores a record nobody can rely on, and
  it passes every check short of this one.
- **It runs weekly on the `schedule` loop.** The failure it guards against — a dump
  silently unrestorable for months — is bounded only by how long it can go unnoticed, and a
  drill that depends on being remembered stops after the incident it was added for. The
  first drill waits a full interval, because on a cold start the bucket is empty.

The number the recovery plan actually needed: **15 seconds**, 12.75 of it `pg_restore`, on
a 52 MB dump of a 28,908-line corpus.

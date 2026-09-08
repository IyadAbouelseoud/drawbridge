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
├── mcp_servers/            # named to avoid shadowing the `mcp` SDK package
│   └── mcp_ace/ mcp_hts/ mcp_docs/ mcp_claims/ mcp_ledger/
├── n8n/workflows/                # exported JSON, version-controlled
├── packages/schemas/             # shared contracts incl. jurisdiction.py profiles
├── scripts/                      # ingest_tariff.py · fasah_sandbox_probe.py
├── tests/                        # unit · golden-claim fixtures · property-based rules
└── infra/
```

`scripts/` is type-checked under `mypy --strict` alongside the services. It builds customs
payloads, which is not a lower standard of correctness than the code that consumes them.

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
| **Bidi reordering** — RTL Arabic interleaved with LTR numbers and Latin HS codes | Extract with bidi-aware ordering; store the logical-order string, not the visual-order one. Visual order silently reverses multi-part numbers |
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

# DRAWBRIDGE — 14-Week Roadmap

> Persistent context. Read with `ARCHITECTURE.md` and `COMPLIANCE-GCC.md` at the start
> of any session.

## Status

| | |
|---|---|
| Current week | 9 |
| Scope | **Dual-jurisdiction: US (CBP) + GCC/KSA (ZATCA)** as of week 2 |
| Current milestone | **The closed loop** — n8n orchestration end to end, both jurisdictions |
| Week 1 exit gate | **PASSED** — 10/10 containers healthy, MCP handshakes verified |

---

## Milestones

| Wk | Milestone | Exit criterion |
|---|---|---|
| 1 | Scaffold, compose stack, CI, schemas package | `docker compose up` — all services green |
| 2–3 | `mcp-docs` + bilingual extraction service | 7501 **and ZATCA Bayan** to typed lines, >=95% field accuracy with provenance spans; Arabic/English |
| 3–4 | `mcp-hts`: USITC schedule ingest, CROSS corpus + pgvector | Classification query returns cited rulings |
| 3–6 | **Matcher** — `MatchStrategy`: US CP-SAT substitution + GCC declaration linkage | Golden fixture set per jurisdiction: known-answer claims reproduce to the cent |
| 6–7 | `mcp-claims` + per-jurisdiction rules: windows, thresholds, lane routing | Refund quantification with full derivation trail; GCC USD 5k gate and 6-month clock enforced |
| 7–8 | Agent layer: exception handling, interchangeability narratives | Analyst in Claude Code can close an exception via MCP tools |
| 8–9 | n8n orchestration + HITL approval gates | End-to-end run with no manual intervention outside gates |
| 9–10 | `packager`: 7551/7552/PSC/1520(d) + ZATCA refund request payload | Packet accepted by a licensed broker in each jurisdiction |
| 10–11 | `mcp-ledger` + §163 recordkeeping posture | Any figure traceable to source span, four years later |
| 11–12 | Multi-tenant hardening: RLS, Authentik, secrets, OTel | Second tenant onboarded with zero code change |
| 12–13 | Pilot: one US importer + one KSA re-exporter, backward-looking claims | Filed claim in each jurisdiction, refund in motion |
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

## Week 2 task breakdown

- [x] GCC/ZATCA compliance research — primary source captured in `COMPLIANCE-GCC.md`
- [x] Dual-jurisdiction architecture: `MatchStrategy`, jurisdiction profiles
- [x] Per-service multi-stage Dockerfiles (api + 5 MCP servers)
- [x] Alembic baseline: claim state machine, both jurisdictions
- [x] `mcp-docs`: MinIO-backed store, span-addressable retrieval
- [x] Extraction: native PDF path + bilingual ar/en OCR fallback
- [x] Golden fixtures: hand-verified CBP 7501 and ZATCA Bayan answer keys

## Week 3 task breakdown

- [x] Path A: US substitution allocation via OR-Tools CP-SAT (`us_substitution.py`)
- [x] Path B: GCC direct-identification linkage with statutory gates (`gcc_linkage.py`)
- [x] `MatchStrategy` interface and jurisdiction-driven routing in the API
- [x] Property-based tests: CP-SAT optimality, GCC day-185 rejection

### Explicitly deferred — glyph x-coordinate RTL table parsing

Robust RTL **table** parsing needs glyph x-coordinates: deciding whether an Arabic run is
stored in visual or logical order is undecidable from the character stream (week 2
`arabic.looks_visually_ordered` uses a label heuristic that works for `label: value` lines
and cannot work for table cells, which carry no separator).

That work is **isolated to `services/extraction/src/geometry.py`** and stubbed, so it does
not block the matching engine. The matcher consumes `EntryLine` / `ExportLine`, which are
already typed and already populated by the week 2 native path — nothing in weeks 3-6
depends on table extraction landing. Revisit once a real scanned *Bayan* corpus exists.

## Week 4 task breakdown

- [x] ZATCA valuation basis resolved — FX anchors to the duty-payment date
      (Valuation Art. 1(I)(6)); hardcoded 3.75 peg removed from `gcc_linkage.py`
- [x] `services/rules/src/fx.py` — dated `RateProvider`, rates held as exact ratios
- [x] Art. 16 §2 threshold enforced strictly; near misses rejected and flagged
- [x] Manufacturing drawback §1313(a)/(b): BOM explosion in the CP-SAT model
- [x] `review_queue` table + `services/rules/src/triage.py` HITL gate
- [x] Three declarative n8n workflows generated from `n8n/generate_workflows.py`

## Week 5 task breakdown

- [x] GCC open questions closed against primary source (`COMPLIANCE-GCC.md` §8)
- [x] `mcp-claims`: analyst tools over the review queue and claim state machine
- [x] `mcp-ledger`: append-only audit trail of analyst decisions
- [x] `mcp-hts`: pgvector classification search over USITC/CROSS and ZATCA tariff
- [x] Queue resolution notifies n8n to resume the suspended workflow
- [x] Integration tests: exception -> MCP resolve -> claim reaches `approved`

## Week 6 task breakdown

- [x] Multi-level BOM nesting: `BomComponent.sub_components`, route-keyed designation
      ceilings in CP-SAT, yield compounding down the tree
- [x] Tariff corpus ingestion: USITC HTS (indent hierarchy resolved), ZATCA Integrated
      Customs Tariff (bilingual), CBP CROSS rulings (revocation detected)
- [x] `services/packager`: CBP 7551/7552 as AcroForm PDFs, ZATCA refund-request JSON
- [x] Resolution 28624 mitigation: every ZATCA procedural citation is an
      `ANALYST_REVIEW` placeholder that blocks transmission (`COMPLIANCE-GCC.md` §8.4.1)
- [x] `scripts/fasah_sandbox_probe.py`: four payloads isolating the §8.2 unknown
- [x] Golden fixtures for the recursive multiplier and both packager lanes

### What week 6 deliberately did not do

**Transmit the Fasah probe.** The sandbox needs credentials issued to a registered
customs broker, and firing a speculative declaration at a customs platform on an
assumption is not a thing to do quietly. The script constructs and prints; a human runs it
and records the result in `scripts/fasah_probe_results.json`.

**Guess a Resolution 28624 article number.** A missing number invites a request for
information; a confidently wrong one is a misstatement to the authority. The placeholder
carries the reason and blocks the packet.

**Bundle the official CBP form templates.** They are CBP-published artefacts. The packager
fills a real template through `pdf.fill_template` where a tenant has one on file, and
otherwise renders a transcription that says on its face that it is not a CBP-issued form.

## Blocked on external acquisition — off the automated critical path

Two items cannot be closed by any amount of engineering. Both were carried as checklist
entries through weeks 5 and 6 on the assumption that another routing attempt might work;
that assumption is now retired. They move here, and the roadmap stops pretending a build
step will reach them.

### B1. Fasah sandbox credentials

**Status: requires a commercial relationship, not a request.** The Tabadul/Fasah sandbox
issues credentials to registered customs brokers against a Saudi commercial registration.
There is no self-service developer signup, and no public endpoint answers without one.

Acquisition routes, in the order worth trying:

1. **Engage a licensed broker in Jeddah or Riyadh** and probe under their credentials.
   This is the intended route regardless — Drawbridge never files, so a broker
   relationship is a prerequisite for the KSA lane going live, not an extra cost.
2. Apply to Tabadul directly as an integrating party once a Saudi entity exists.
3. A logistics provider already integrated with Fasah, as a sponsoring partner.

`scripts/fasah_sandbox_probe.py` is finished and waiting: four payloads, four questions,
`--curl` prints the commands. It needs an hour of a credentialed operator's time and
nothing else.

**Until then:** `PartialConsignmentBehaviour.UNDETERMINED` holds, partial-shipment claims
route to `ANALYST_REVIEW`, and no code depends on the answer.

### B2. ZATCA Resolution 28624 article numbers

**Status: requires a document, not a URL.** Every published route failed
(`COMPLIANCE-GCC.md` §8.4): ZATCA's own PDF 404s, the Umm Al-Qura issue page 500s, its
decisions page carries the operative text as an unlinked attachment, the istitlaa
consultation copy resets, two aggregators 503.

Acquisition routes:

1. **Academic legal databases** carrying the Umm Al-Qura gazette — a Saudi or Gulf
   university law library, or a subscription service indexing the gazette in Arabic.
2. **A local customs broker or Saudi trade-law practice**, which will hold the operative
   text as working material.
3. **The Umm Al-Qura print archive** by direct request, or ZATCA by written enquiry.

**Until then:** every ZATCA procedural citation is an `ANALYST_REVIEW` placeholder that
blocks transmission, and the packet says why (`COMPLIANCE-GCC.md` §8.4.1). This is a
stable resting state, not a temporary patch — the operative constants all come from the
GCC Common Customs Law, which is transcribed and authoritative. Closing B2 is one edit to
`services/packager/src/citations.py`.

## Week 7 task breakdown

- [x] Embedding pass: batched, resumable, pluggable backend; `pgvector` columns populated
      for USITC, ZATCA and CROSS
- [x] OCR confidence gating: per-token floor, per-field aggregate, hard rejection before
      the matcher sees a figure
- [x] `services/ingest/erp_mock.py`: deeply nested BOM source feeding the week 6
      recursive CP-SAT matcher
- [x] PSC (19 CFR §141.11) and §1520(d) renderers in `services/packager`

## Week 8 task breakdown

- [x] **Semantic embeddings.** `fastembed` replaces the trigram placeholder — a quantised
      ONNX sentence-transformer running in-process on CPU. No API, no key, no per-token
      cost; one model download, then offline. `is_semantic` is now True and earns it.
- [x] **The agent layer** (`services/agent/`): interchangeability narratives for US
      §1313(j)(2) pairings, and pre-analysis memos for `review_queue` rows.
- [x] **Queue integration.** Memos attach to the row and come back from
      `mcp-claims.inspect_exception`, so an analyst opening a claim already has the
      drafter's reading and its list of what it could not settle.
- [x] Golden fixtures for both.

### Why the model is multilingual

`paraphrase-multilingual-MiniLM-L12-v2` rather than `bge-small-en-v1.5`, which is smaller
and slightly sharper on English. Half this corpus is Arabic, and an English-only encoder
does not merely score Arabic badly — it has no useful geometry for it, so every ZATCA line
would sit at an arbitrary point. It also retrieves *across* the language boundary, which is
the property the dual-jurisdiction design needs and did not previously have: an Arabic
*Bayan* description now reaches an English USITC line.

### Two things week 8 found by running it

**The embedding width was wrong, not adjustable.** The column was provisioned at 1536; the
model emits 384. Migration `a7c31f9d4e60` moves it and nulls the existing vectors — there
is no conversion between embedding spaces, and any backend change already required a full
re-embed. `embed_corpus.py` selects on `embedding IS NULL`, so the re-embed is the normal
pass with nothing new to run.

**`VECTOR_CEILING` was calibrated for the old model and silently suppressed every semantic
hit.** 0.55 was measured against trigram distances, which collapse fast. This model's
distances are compressed into a narrow band — a good match near 0.6, an unrelated one near
0.86 — so every correct hit fell outside the threshold and search reported `method=lexical`
with nothing found. Indistinguishable from a corpus that was never embedded, and nothing
raised. The ceiling now lives on the backend (`Embedder.vector_ceiling`) because it is
meaningless outside one embedding space, and `tests/golden/test_semantic_embeddings.py`
keeps the regression.

The new value (0.75) was measured against nine tariff lines. That is enough to establish
the old one was wrong and not enough to call the new one right — see week 9.

### What week 8 deliberately did not do

- **Let the agent originate a figure.** `services/agent/src/grounding.py` scans every
  generated memo and rejects any number not traceable to the facts it was given. This is
  `CLAUDE.md`'s rule made mechanical rather than requested in a prompt: the failure it
  catches is not the model lying but the model being fluent — "approximately 4,800 units"
  where the record says 4,812.50.
- **Let the agent originate a citation.** Authorities are selected from a closed set the
  rules engine resolved. A fabricated ruling number in an audit-facing memo is the
  highest-cost error available to this system: confident, specific, checkable, wrong.
- **Let the agent resolve anything.** `recommendation` is a field on a row. A human
  calling `resolve_review_exception` is still the only thing that moves a claim.
- **Draft inside `POST /review/suspend`.** n8n waits on that call; coupling workflow
  suspension to a model round trip would make it fail for an unrelated reason.

## Week 9 task breakdown

- [x] **The loop closed.** `/documents/batch`, `/extraction/run`, `/classification/run`,
      `/claims/persist`, `/claims/transition`, `/packaging/build` — the six endpoints n8n
      was already calling in a workflow that referenced four that did not exist.
- [x] **The n8n pipeline rewritten** around them: intake, extraction, classification,
      matching, triage, persistence, then either the automated lane or the HITL branch,
      then packaging. Twenty-two nodes, generated from `n8n/generate_workflows.py`.
- [x] **The automated lane.** A clean claim now goes `quantified -> approved -> packaged`
      with no human in it.
- [x] **`vector_ceiling` calibrated** against `tests/fixtures/tariff_benchmark.json` —
      twenty labelled queries, ten the corpus answers and ten it cannot.
- [x] **Adversarial guard fixtures** (`tests/golden/test_agent_adversarial.py`): forty-odd
      deliberate hallucinations — invented duty amounts, fabricated entry numbers,
      non-existent CFR sections, unverified ZATCA articles — each asserted to fail closed
      with a named error type.
- [x] **`scripts/e2e_pipeline_test.py`**: both jurisdictions through the deployed stack.

### The ceiling could not be calibrated, and that was the finding

Week 8 set it to 0.75 from nine tariff lines and said nine was not enough. It was not, but
not in the direction expected: 0.75 admitted **eight of the ten hard negatives**. The
useful result is the one no value fixes. The worst true positive sits at 0.624 and the
nearest unanswerable query at 0.508, so the two ranges *overlap* — "ruggedised field laptop
computer" and "portable cordless electric hand drill" are not separable by distance against
this corpus, at any threshold.

So the ceiling stopped being a precision mechanism and became what it can actually be: a
rubbish filter calibrated for recall. 0.68 is the smallest value that still retrieves the
correct code for all ten positives, and it keeps "live breeding cattle" and "marine cargo
insurance brokerage" out. Everything admitted below it is a candidate, not an answer.

`tests/golden/test_tariff_benchmark.py::test_the_two_distance_ranges_overlap` asserts the
overlap, so if a future model does separate the classes the suite says so rather than
silently continuing to carry a mechanism that is no longer needed.

### The defect the benchmark found

`needs_analyst_confirmation` was "the vector path alone found this". That treated agreement
between the two paths as corroboration regardless of how weak either was, and two of the
twenty queries produced exactly that: "wooden lead pencils" reached wooden office furniture
on a trigram coincidence over the word *wooden* — 0.157, comfortably over the retrieval
floor — with a mediocre vector distance agreeing. It came back as an answer needing no
analyst. Every *correct* corroborated answer in the set scored 0.216 or better.

The rule is now: the lexical path confirms, alone, and only above
`search.CONFIRMATION_LEXICAL_FLOOR` (0.20). The vector path finds and ranks and never
confirms — a strong trigram match is the published schedule text saying so, where a near
vector neighbour is a model saying so, and those are different claims. Precision over
confirmed answers is 1.00 on the benchmark; it was 0.67 before.

The floor sits in a gap of 0.06 measured on twenty queries. It will move. The shape of the
rule is the finding, not the constant.

### Approval without a human, and the guard that replaced the old one

Until week 9 `ClaimState.QUANTIFIED` could only reach `ANALYST_REVIEW`, so every claim —
including the ones triage had nothing to say about — occupied a human's queue to be waved
through. A queue of non-decisions is a queue people stop reading, and the exceptions that
matter go with it.

`QUANTIFIED -> APPROVED` is now permitted. What stops that being a hole is that the
guarantee moved rather than went away: `analyst.transition_claim` refuses **any** move into
`APPROVED` while the claim carries an unresolved `review_queue` row, whoever is asking. The
check had to move out of `approve_claim` because the pipeline is now a caller, and it is
the caller that runs unattended.

### Two things week 9 found by running it

**`entry_lines.port_of_entry` was 16 characters.** Wide enough for a 4-digit CBP port code,
too narrow for "Jeddah Islamic Port". Every KSA claim was unpersistable and no test caught
it, because until week 9 nothing persisted a claim at all — the GCC matcher was exercised
entirely in memory. Migration `e5c48b71d90a` widens it to 64.

**A five-field test document scores 0.87 and fails the extraction floor.** The first e2e
run routed a clean US claim to review. The gate was right: character density is how the
native path scores its confidence, and a page with five lines on it has almost nothing to
be confident about. The fixture now carries the forty-odd fields a real 7501 and *Bayan*
carry. Worth recording because the tempting fix was to lower the floor.

### What week 9 deliberately did not do

- **Build a document-to-typed-lines extractor.** `/extraction/run` reports what can be read
  and how confidently; the typed lines still arrive from the structured source. Assembling
  them from a scanned *Bayan* table needs the glyph x-coordinate work deferred since week
  3, and faking it would put a fabricated provenance span on a filing.
- **Let the pipeline change a declared tariff code.** `/classification/run` corroborates and
  reports; it never rewrites. Reclassifying merchandise is a customs matter, not a
  data-cleaning one.
- **Run the agent live.** The e2e reports `unavailable` with no API key and the row keeps a
  NULL memo, which is exactly the state analysts worked in before the service existed. The
  question week 8 raised — how often a *real* memo trips the numeric guard — is still open
  and still needs an API key and real queue rows, not a stub.
- **Send anything to a customs authority.** Case B ends untransmittable by design: the ZATCA
  packet carries five open Resolution 28624 citations and the packager blocks it.

## Week 10 entry checklist

1. **Full-volume corpus load** — the ~19,000-line USITC schedule and the CROSS body.
   Carried from week 9. It gates (2), and the twenty-four-line benchmark corpus is the
   reason the current thresholds are provisional rather than wrong.
2. **Re-run the benchmark against that corpus.** Both constants — `vector_ceiling` 0.68 and
   `CONFIRMATION_LEXICAL_FLOOR` 0.20 — were measured against twenty-four lines. The second
   sits in a 0.06 gap and is the one likeliest to move.
3. **A live agent run against real queue rows.** Unchanged from week 8, and now cheaper to
   do: the pipeline produces real rows with real claims attached.
4. **Import the workflows into n8n and run one for real.** The JSON is generated and the
   endpoints it calls are exercised, but nothing has executed inside n8n yet — the Wait
   node's resume path in particular is asserted by `services/api/src/resume.py` and
   `mcp-claims`, not observed.
5. **Real scanned *Bayan* corpus** to calibrate the OCR floor against measured error.
6. **A real ERP connector** to replace `erp_mock.py`.
7. **Tenant profiles.** `/packaging/build` takes the claimant per request because filing
   identity — EIN, CR number, broker code, IBAN — is not modelled. It has to be before a
   pilot, and it is a small schema change, not a design question.
8. Blocked externally: **B1** Fasah credentials, **B2** Resolution 28624 text.

## Sequencing rationale

Extraction (wk 2–3) precedes matching (wk 4–6) because the matcher's input contract is
defined by what extraction can actually guarantee — building the matcher first would fix
a contract against imagined data.

The GCC lane entered at week 2 rather than later because it changes the *shape* of the
matcher, not just its constants (`ARCHITECTURE.md` §3.5). Discovering that substitution
does not exist in GCC law after building a substitution-shaped matcher would have meant
rewriting weeks 4–6, not extending them.

The agent layer (wk 7–8) lands *after* the deterministic core, so the LLM is scoped to
exactly the residue the rules engine cannot decide. Building it earlier invites the model
to absorb work that belongs in testable code.

That scoping is enforced rather than intended. By week 8 the matcher, the quantifier and
the rules engine already produce every figure a memo can contain, so `grounding.py` can
reject anything else as invented. Had the agent been built first there would have been no
fact set to check against, and "the LLM never originates a number" would have been a
convention — which is to say, a thing that holds until someone writes a prompt that
happens to break it.

The pilot (wk 12–13) targets a backward-looking claim rather than live flow: a 5-year
lookback has a known answer set and no operational dependency on the customer's current
quarter.

Orchestration (wk 8–9) lands last among the mechanisms rather than first, and week 9 is why
that ordering was right. Wiring the loop is what forced the first *write* of a GCC claim,
and the write is what found a column too narrow to hold a Saudi port name. A pipeline built
before the pieces it connects would have been rewritten around each of them in turn; a
pipeline built after them is where their contracts get tested against each other for the
first time. Two of the three defects week 9 found were of exactly that kind — not bugs in a
component, but disagreements between components that only a run can surface.

# DRAWBRIDGE — 14-Week Roadmap

> Persistent context. Read with `ARCHITECTURE.md` and `COMPLIANCE-GCC.md` at the start
> of any session.

## Status

| | |
|---|---|
| Current week | 17 |
| Scope | **Dual-jurisdiction: US (CBP) + GCC/KSA (ZATCA)** as of week 2 |
| Current milestone | **A set big enough to argue with** — fifty labelled queries, and an ingest repair measured rather than assumed |
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
| 11–12 | Multi-tenant hardening: RLS (wk 11) **then** Authentik on top of it (wk 12), secrets, OTel | Second tenant onboarded with zero code change |
| 12–13 | Pilot: one US importer + one KSA re-exporter, backward-looking claims | Filed claim in each jurisdiction, refund in motion |
| 14 | Broker white-label packaging + on-prem compose | Reproducible `.onprem.yml` deploy |
| 15 | Debug, review, harden | Backups under object lock; every committed artefact executed at least once |
| 16 | Retrieve-then-rerank; restore drill | 9/10 at hs6 in the top ten; a backup restored and its ledger re-verified |
| 17 | Ingest hierarchy; fifty-query benchmark | 44/50 at hs6 in the top ten, 26/50 at rank one; the ancestor-chain theory refuted |

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

Three items cannot be closed by any amount of engineering. Each was carried as a checklist
entry for several weeks on the assumption that another routing attempt might work; that
assumption is now retired for all three. They live here, and the roadmap stops pretending a
build step will reach them.

All three resolve through the same door. B1 needs a licensed Saudi broker's credentials, B3
needs a licensed Saudi broker's documents, and B2 is working material that practice holds.
One commercial relationship closes the KSA lane's remaining unknowns, and Drawbridge needs
that relationship anyway — it never files, so a broker is a prerequisite rather than an
extra cost.

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

### B3. A real scanned *Bayan* corpus

**Status: requires unredacted client documents, not a dataset.** Moved here in week 11
from the entry checklist, where it had been carried since week 3 as though it were a
build step. It is not one. There is no public corpus of Saudi customs declarations, and
there will not be: a *Bayan* carries the importer's identity, commercial values and
consignment references, so every copy in existence belongs to a trader or their broker.
Synthetic pages cannot substitute — the whole point of the corpus is to find out what a
real form does that this repository did not imagine.

Acquisition routes, in the order worth trying:

1. **The same broker relationship as B1.** A licensed broker holds thousands of these as
   working material and is the only party who can share them lawfully, redacted or under
   an engagement. This is why it is the same route: one relationship closes both.
2. **A pilot client's own archive**, under the engagement letter that would exist anyway
   before Drawbridge touched their filings.
3. A logistics provider's document management system, as a sponsoring partner.

**Until then:** `tests/fixtures/bayan.py` builds a *Bayan* at the PDF object level and the
geometry and template suites run against it. What that establishes is real — the
reconstruction is coordinate-driven and survives both storage orders — and what it cannot
establish is equally real: `COLUMN_GAP_POINTS`, `ROW_OVERLAP_RATIO` and `WORD_GAP_WIDTHS`
were chosen against a fixture this repository wrote, and `BAYAN_LINE_TABLE` is a mock whose
column set comes from the declaration's published structure rather than from a measured
form. Neither is calibrated. The template refuses rather than guesses when a heading is
missing, which is the posture that makes an uncalibrated parser safe to ship, and the
`ANALYST_REVIEW` route is where anything it will not read goes.

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

## Week 10 task breakdown

- [x] **RTL table parsing, un-deferred.** `services/extraction/src/geometry.py` was a stub
      from week 2. It now reads per-glyph coordinates from `rawdict`, bands them into rows
      by vertical overlap, splits them into cells by horizontal proximity, and emits each
      cell in reading order — right to left for an Arabic cell, left to right for the
      Latin and numeric runs inside it.
- [x] **`audit_ledger`** — append-only by construction, not by convention. Triggers refuse
      UPDATE, DELETE and TRUNCATE; every row carries a SHA-256 chained to its predecessor
      within the tenant. Migration `f7a3c9d2e814`.
- [x] **`ProvenanceSpan`** in `packages/schemas`: document hash, page, x0/y0/x1/y1, all
      mandatory. `EntryLine` and `ExportLine` refuse to construct if a figure they state
      has no box behind it.
- [x] **`trace_figure`** on `mcp-ledger`, plus `ledger_chain` and `claim_ledger`.
- [x] **Golden fixtures**: a synthetic *Bayan* table parsed from geometry
      (`tests/golden/test_rtl_geometry.py`), and an approved claim's duty traced to a
      rectangle through the MCP tool (`tests/integration/test_ledger.py`).

### The corruption is in the numerals, not the words

The week 2 plan assumed the problem was reversed Arabic *text* and that glyph advance
direction would reveal which producers stored it visually. Measured, that turned out to be
the wrong half of the problem. MuPDF applies its own bidi pass to Arabic letter runs before
anything downstream sees them, so words usually arrive readable and `order_of` reports
LOGICAL whichever way the producer wrote them.

It does not do the same for Arabic-Indic numerals. A quantity of ١٢٠٠ comes out of the text
layer as ٠٠٢١, which normalises to 21 — a figure that parses, reconciles against nothing,
and is wrong by a factor of fifty-seven. That is strictly worse than a reversed word, which
whoever reads it notices.

So `order_of` stayed, downgraded to diagnostic, and the reconstruction stopped consulting
the stream at all. `extract_table` never reads the character sequence; it reads positions.
The suite renders the same table into a PDF twice — once stored visually, once logically —
and asserts identical output, because a test against one storage order would pass just as
well if the module were quietly reading the stream.

### What "traceable to a source-document span" meant before this week

`CLAUDE.md` has said since week 1 that every figure in a claim must trace to a span in a
source document. Until week 10 that was satisfiable by one span covering a page. A claim
could carry nine figures and a single rectangle, and the invariant held.

`ProvenanceSpan` is the strict form: hash, page, and a non-degenerate box, none of them
optional. `EntryLine._figures_carry_their_boxes` refuses a line that states a figure with
no box — but only when the provenance cites a *paginated* document. EDI and ERP feeds have
records rather than pages and address their figures by `field_path`, which is not a
loophole so much as the honest description of a source with no coordinates. The distinction
is `provenance.STRUCTURED_KINDS`, and a claim citing a 7501 no longer gets to use it.

The e2e run is where that bites. Week 9's script sent `field_path="lines[0]"` and said in
its docstring that claiming a rectangle it had not measured would be a fabricated record.
It was right, so week 10 changed the input rather than the standard: each case now ingests
two documents — the import declaration and the export evidence — and locates each figure on
the one that evidences it with `page.search_for`. `_boxes` raises when a label is not on the
page. Nothing approximates.

### Why the ledger is chained and not just append-only

Triggers stop the application from rewriting history. They do not stop someone with
database access from dropping the triggers, and a customs audit is the setting where "the
application could not have done it" is a weaker claim than "the record shows it was not
done".

Each row therefore hashes its own content together with its predecessor's hash, per tenant.
`verify_chain` recomputes the lot and reports the sequence number of the first break, and
distinguishes the two ways it can break: a `prev_hash` that does not match means a row was
removed, an `entry_hash` that does not match means a row was altered.

What the chain does *not* detect is the removal of an entire tenant's chain, because an
empty chain verifies. That is what the TRUNCATE guard is for, and beyond it, off-site
retention — which is a deployment question week 10 did not answer.

### Two consequences worth stating plainly

**A tenant can no longer be deleted.** `audit_ledger.tenant_id` is RESTRICT, so offboarding
becomes a deliberate manual act. That is the correct direction for a five-year retention
obligation to fail, and it made the integration teardown say so out loud: the fixtures
suspend the triggers, remove their own rows, and put the guards back.

**`claim_id` carries no foreign key at all.** Every other claim-scoped table cascades on
delete. A cascade here would mean deleting a claim silently deletes the evidence it
existed, so the column is allowed to outlive its claim instead.

### What week 10 deliberately did not do

- **Assemble typed lines from a scanned table.** `extract_table` returns cells and their
  boxes; which column is the duty and which is the VAT still comes from the caller. A
  Bayan template is a small piece of work and inferring the mapping from position is not —
  it would put a layout guess between an Arabic table and a duty figure.
- **Backfill provenance for the pre-week-10 fixtures.** The property suites now hand every
  line a full set of distinct boxes, but they are synthetic boxes on synthetic documents.
  Only the e2e measures against real bytes.
- **Verify the chain automatically.** `ledger_chain` is a tool an operator or an auditor
  calls. Nothing runs it on a schedule and nothing alerts on it, which is a deployment
  concern rather than a code one, and pretending otherwise with a background task nobody
  watches would be worse.

### One thing found by running the suite

`test_never_exceeds_lp_upper_bound` tolerated one cent of rounding regardless of how many
allocations the solver made, where its sibling test already scaled the tolerance per
allocation. Hypothesis found it with three matches against a duty of one cent — where the
quantization *is* the entire figure. The tolerance was the oversight; the solver was doing
what it says it does.

## Week 11 task breakdown

- [x] **Bayan template engine.** `services/extraction/src/templates.py` — a spatial
      mapping from geometry cells to named fields, with a mock ZATCA declaration
      (`BAYAN_LINE_TABLE`). Column semantics are supplied, not inferred: week 10 stopped
      exactly here and said why.
- [x] **Export-and-tombstone offboarding.** `scripts/tenant_offboard.py` signs a tenant's
      whole ledger chain with Ed25519, archives it to MinIO cold storage as JSON lines,
      and tombstones the tenant row. The ledger rows stay where the retention obligation
      requires them and stop being reachable.
- [x] **Row-level security.** Migration `b1d6f2c93a47` enables RLS on the ten tenant-scoped
      tables; `services/api/src/tenancy.py` scopes every session with `SET LOCAL tenant.id`;
      `scripts/rls_bootstrap.py` creates the unprivileged role that makes it bite.
- [x] **Golden fixtures**: a geometric cell typed as `duty_amount`
      (`tests/golden/test_bayan_template.py`), and a query for another tenant's claim id
      returning nothing (`tests/integration/test_rls.py`).

### The thing that would have made RLS decorative

`drawbridge` — the role in every DSN in the stack until this week — owns these tables and
is a Postgres superuser with `BYPASSRLS`. Policies do not apply to it. `FORCE ROW LEVEL
SECURITY` would not have helped either: FORCE binds a table's owner only where the owner is
not a superuser, which is precisely not this case.

So a migration that enabled RLS and stopped there would have installed ten policies, passed
every test written against the existing connection, and isolated nothing. That is a worse
outcome than no policies at all, because the next person to read the schema would conclude
the problem was solved.

The isolation therefore comes from `drawbridge_app`, an unprivileged role, and week 11
moved the stack onto it: `docker-compose.yml` points the services at `drawbridge_app` and
keeps the owner URL under its own name for the things that genuinely cross tenants —
migrations, the corpus loaders, `tenant_offboard.py`, and the two operator steps in the
e2e. `scripts/rls_bootstrap.py` refuses to finish if the role it just created can bypass a
policy, and `tests/integration/test_rls.py` asserts the same thing before any other test in
the file relies on it.

The e2e was then run end to end with every service connected as the app role. Both cases
passed, which is the only evidence that matters here: a policy set that has never had a
real pipeline run through it is a hypothesis.

### Fail-closed, and what that cost

`app_current_tenant()` returns NULL when nothing has set `tenant.id`, so an unscoped
connection reads no tenant rows at all. The alternative — falling open when the variable is
missing — would be satisfied by exactly the set of code paths this control exists to catch.

The cost is that every entry point had to be given a tenant, and several never see one. A
`GET /claims/{id}`, a packet build, `trace_figure`, an analyst clicking a resolve link:
each has an identifier and nothing else. Those resolve the owner through a `SECURITY
DEFINER` lookup — `app_tenant_of_claim`, `app_tenant_of_review`,
`app_tenant_of_resume_token` — and then scope the rest of the transaction. Each returns one
uuid and pins its `search_path`, so what someone learns by guessing a claim UUID is which
tenant owns it, and the row itself stays invisible until the scope is set. That is a real
hole, it is the smallest one that lets the routes work, and it is pinned by a test rather
than described in a comment.

`SET LOCAL` rather than `SET`, through `set_config(..., is_local => true)` rather than
literal SQL. Connections are pooled: a scope that outlived its transaction would be
inherited by whichever request checked the connection out next, which is a cross-tenant
read with no bug anywhere in any query.

### The tombstone is what makes RESTRICT survivable

Week 10 left `audit_ledger.tenant_id` as RESTRICT and stated the consequence — a tenant with
ledger rows cannot be deleted — without giving offboarding a procedure. The procedure is
export, sign, tombstone, in that order, and the order is the interesting part: a failed
upload leaves a live tenant and no artifact, which is fixed by running the script again,
whereas the other order leaves an invisible tenant whose ledger was never exported. That
second state is the one the whole mechanism exists to prevent, so the database refuses it
too — `ck_tenant_offboard_is_evidenced` rejects a tombstone with no artifact key, signature
and public key on the row.

Ed25519 over the manifest, and the manifest commits to the SHA-256 of the body. Years
later a customs authority is being shown records produced by the party it is auditing, and
"this is what our database said" is a weaker statement than "this is what our database
said, and here is a signature made before the relationship ended". The private key is read
from the environment and the script refuses to invent one, because a signature made with a
key that existed for the duration of a single process proves nothing.

One predicate does the hiding. `app_current_tenant()` resolves the GUC *through* the
`tenants` table and returns NULL once `offboarded_at` is set, so a tombstone takes the
tenant's documents, claims, ledger and queue out of every scoped query at once rather than
requiring ten policies to remember.

### The heading is the authority, not the index

The template declares both a column index and the headings that column carries. When a
header row is present the heading wins and the binding actually used is reported back.

This is not defensive programming. A form revision that inserts a column shifts every index
after it, and a template trusting its own indices would keep parsing — reading the quantity
column as the value, producing a claim that is arithmetically consistent, internally
reconciled, and wrong. `test_a_reordered_form_binds_by_heading_rather_than_by_index` swaps
value and duty on the page and pins the outcome. A field whose heading is nowhere on the
page raises rather than falling back to position.

Coercion failures behave differently: they are collected on `TemplateResult.issues` rather
than raised, because a dash struck through one duty cell should not discard the four good
columns beside it. That is what the review queue is for.

### What week 11 deliberately did not do

- **Assemble `EntryLine` objects from a template.** `spans_for` emits the provenance a
  line needs and the typing is done, but nothing yet builds a declaration out of a *Bayan*
  end to end. That wants a real form to build against (**B3**), and a header block —
  declaration number, importer, dates — which is a different extraction problem from a
  line table and gets its own week.
- **Move `tariff_lines` and `tariff_rulings` under RLS.** They are the same schedule for
  every tenant and carry nothing that identifies one. A policy there would cost a join per
  classification query and protect nothing.
- **Verify the chain on a schedule.** Still an operator action. Carried from week 10 and
  now carried again, because it is a deployment question — where the ledger is replicated
  and what alerts on a break — and answering it with a background task nobody watches
  would be worse than leaving it open.
- **Rotate the offboarding key, or verify an archived artifact from the CLI.**
  `verify_artifact` exists and is tested; nothing exposes it to an auditor yet.

### One thing found by wiring it up

`ALTER ROLE ... PASSWORD` is a utility statement and takes no bind parameter, so the value
has to reach Postgres as a literal. That is the only statement in `tenancy.py` where a
placeholder is unavailable, and it is a password — so the character set is restricted to
printable ASCII without backslashes first and the quote doubled second. Worth recording
because the obvious code passes a bind parameter, and it fails at runtime rather than at
type-check time.

## Week 12 task breakdown

- [x] `services/api/src/auth.py` — bearer verification, Authentik JWKS (RS256) or a local
      shared secret (HS256), and the tenant claim that feeds week 11's `SET LOCAL tenant.id`
- [x] `AuthMiddleware` on every route but `/health`, `/ready` and the schema endpoints
- [x] `authorise_tenant` / `expected_tenant` — the token overrules the request body
- [x] Authentik server and worker in compose behind an `identity` profile; Jaeger always on
- [x] `services/api/src/telemetry.py` — one provider per process, exporter optional
- [x] `audit_ledger.trace_id`, stamped and deliberately not hashed (migration c8e41b7f52d9)
- [x] `tests/integration/test_tenant_isolation.py` — two users, two tenants, through HTTP
- [x] `scripts/pilot_us.py`, `scripts/pilot_ksa.py`, `scripts/pilot_common.py`
- [x] `scripts/mint_token.py`; n8n's HTTP nodes now send a bearer token

### How this attaches to week 11, precisely

Week 11 installed row-level policies comparing every row against `app_current_tenant()`,
which reads a GUC the application sets per transaction. It ended by saying the control
answers *which rows may this connection see* and that nothing answered *who is this caller*.

That gap had a specific shape rather than a general one. The value in `tenant.id` came from
the request body — `POST /claims/persist` scoped the transaction to `body.tenant_id` — so
the policies were comparing each row against a number the caller supplied. Against an honest
client that is isolation. Against a client that edits one field it is nothing, and Postgres
cannot tell the difference: from its side both requests look like a correctly scoped
connection.

Authentik closes it at exactly one join. The `tenant_id` claim on a verified token becomes
the argument to `tenancy.set_tenant`, and `auth.authorise_tenant` refuses a body that
disagrees with it. Everything else about the two weeks is unchanged — same policies, same
`drawbridge_app` role, same fail-closed GUC. What changed is the provenance of one value.

The layering is what makes the milestone's "second tenant with zero code change" true.
Authentik decides *who*, the token carries *which tenant*, and Postgres enforces *which
rows*. Three mechanisms, and none of them is a filter in a query a developer has to remember
to write.

### The four identifier-addressed routes, closed

Week 11 enumerated the entry points that hold an identifier and no tenant — `GET
/claims/{id}`, `POST /packaging/build`, `POST /review/{id}/resolve`, `GET
/review/pending/{token}` — gave each a `SECURITY DEFINER` owner lookup, and recorded the
consequence: a caller holding a claim id could read that claim, because the lookup resolved
the owner and scoped to it.

`auth.expected_tenant()` is the constraint that was missing. The owner the lookup returns
must equal the token's tenant or `tenancy._require` raises, and the route reports the same
404 a nonexistent id gets. Same status, same body — distinguishing "not yours" from "does
not exist" turns a guessed uuid into a membership oracle, and the caller cannot act on the
difference anyway.

It returns None for a service principal, which is a stated exemption rather than an absent
check.

### The service token is a cross-tenant credential, and it is not pretended otherwise

n8n runs one workflow against whichever tenant its trigger names, and is addressed by claim
id four times in a single run. Minting it a token per tenant would put tenant credentials in
a workflow file; giving it none would put the API back where it started. So there are two
principal shapes: a *user* bound to one tenant by its `tenant_id` claim, and a *service*
carrying `drawbridge:service` and no tenant, which may act for any of them provided it names
one.

A token that is both is refused at `decode`, because "may this caller act for tenant X" would
otherwise depend on which field the reader looked at first.

This is the largest hole in the week, so it is written down rather than mitigated: anyone
holding `DRAWBRIDGE_SERVICE_TOKEN` reads every tenant. It is minted by a separate command, it
is the one credential in `.env.example` annotated as a cross-tenant breach, and
`TestTheServiceTokenIsDeliberatelyDifferent` pins what it can do, so a change to that surface
is visible rather than incidental.

### The trace id is recorded and not hashed

`audit_ledger.trace_id` ties a row an auditor reads in 2030 to the run that wrote it. It is
not part of `entry_hash`, and the reason is what the digest is for: it covers what the row
*asserts* — the event, its subject, its actor, the document behind it — and a trace id says
where to look for how it happened.

Hashing it would have cost two things and bought nothing. Artifacts exported by
`tenant_offboard.py` before this migration would stop verifying against a chain recomputed
after it, which is precisely the false positive that teaches people to ignore a
tamper-evidence mechanism. And the same logical event replayed after a failure would hash
differently, so a retry would read as a rewrite.

The append-only triggers refuse `UPDATE` on this table, so the column can only ever be
written by the `INSERT` that creates the row. A trace id cannot be attached after the fact,
which is the property that keeps it honest.

### Two things found by wiring it up

**`FastAPIInstrumentor.instrument_app` in the lifespan does nothing.** Starlette builds its
middleware stack once, so a middleware added at startup goes into a list nothing reads again.
The first run produced spans for the single hand-written span in `/matching/run` and for no
request at all — which looked like working instrumentation right up until the operation list
was read and had one entry in it. Instrumentation now happens at module scope, last, which
also makes the OpenTelemetry middleware outermost: a request rejected by auth still gets a
span, and a burst of 401s is visible as one.

**`/documents/batch` authorised after it had already written the object.** The handler puts
each document into MinIO under the tenant's prefix and only then calls `in_thread`, so the
first version of the tenant check ran after the side effect — a forged request was refused
and left an object behind under someone else's key. The integration test caught it by failing
on a MinIO connection error rather than on the assertion it was written for. Both document
routes now authorise before touching the store. Worth recording because the check was in the
right function and still in the wrong place.

### What week 12 deliberately did not do

- **Configure Authentik.** The containers are in compose behind an `identity` profile and the
  API verifies RS256 against a JWKS URL, but no provider, application or property mapping is
  scripted, and nothing has issued a real RS256 token to this API. The local HS256 path is
  what the suite and the e2e exercise. Until a blueprint exists and one round trip has
  actually happened, "Authentik integration" means the API is shaped to accept it.
- **Put secrets anywhere but the environment.** `DRAWBRIDGE_JWT_SECRET`,
  `DRAWBRIDGE_SERVICE_TOKEN` and `DRAWBRIDGE_APP_DB_PASSWORD` are environment variables with
  development defaults. That was the third item in the week 11–12 milestone and it is the
  one that did not land.
- **Trace the MCP servers in the same trace as the API.** Each configures a provider and each
  produces its own spans, but nothing propagates `traceparent` across the MCP transport, so
  an analyst tool call is a separate trace from the request that prompted it.
- **Rotate or expire anything.** Tokens carry an `exp` and nothing refreshes them; there is
  no revocation list and no key rotation.
- **Seed a pilot from real documents.** Both corpora are fiction and every figure in them
  carries a `pilot-fixture` box that traces to no PDF. `pilot_common.assert_not_evidence`
  exists so that nothing can quietly file one.

### The KSA pilot cannot be five years old, and that is the statute

The instruction was a five-year-old corpus in both jurisdictions. It works in the US lane:
§1313(j) allows five years from import to export and §1313(r) three years from export to
file, so 2021 entries exported in 2024 are still filable this year — which is the shape of a
real backward-looking engagement, old imports and live money.

It does not work in KSA. Common Customs Law Art. 174 bars any claim for duties paid more than
three years ago, with no discretion, and `KSA_PROFILE` encodes it as an absolute bar. A 2021
Saudi corpus does not produce a small refund; it produces zero, because every line is dead
before the matcher sees it.

So `pilot_ksa.py` defaults to the oldest duty payments still inside Art. 174, and keeps the
literal five-year corpus behind `--time-barred` — because week 13 will need to show a
customer *why* their older entries are gone, and a rejection nobody can reproduce is an
assertion. Both corpora print their caveats rather than carrying them in a comment.

## Week 13 task breakdown

- [x] `packages/schemas/tenant.py` + `tenant_profiles` — EIN, CR number, broker code, IBAN
- [x] `services/api/src/profiles.py` — the packager addresses a filing from the database
- [x] `services/api/src/secrets.py` — secrets out of `.env`, with a provider seam
- [x] `scripts/manage_secrets.py`, `make secrets-init` / `secrets-show` / `secrets-check`
- [x] Full USITC load: 28,899 published lines, ingested and embedded
- [x] `scripts/pilot_run.py` — both corpora through the deployed API, both asserted
- [x] `search_text` (migration f2b90d47ac13) — embed the leaf, not the chapter heading
- [x] `scripts/calibrate_thresholds.py` — what the thresholds look like at volume
- [x] `tests/integration/test_trace_propagation.py` — the MCP trace join, pinned

### The pilot ran, and the KSA corpus could not have

`make pilot-run` pushes three corpora through the deployed API with a real bearer token.
The US lane reaches a rendered CBP 7551, transmittable, with no open citations, addressed
entirely from the database. The time-barred KSA lane is refused with GCC Art. 174 named in
the rejection. Both refunds reproduce **to the cent** — USD 128,597.24 and SAR 54,687.50 —
which is the assertion that matters, because a pilot that only checks for a non-zero
refund passes just as happily on a wrong number.

Running it found that week 12's KSA corpus asserted a refund no filing date could produce.
Its dates were literals; by the time anything ran them, the clean re-export sat 461 days
behind the filing date and Art. 16 §3(b)'s six-month window had closed on it. The GCC lane
cannot hold fixed dates — six months from re-export to filing means a literal corpus
expires within two quarters of being written, and expires silently. Every KSA date is now
an offset from the filing date, with the three gates and the room each offset leaves
written down beside them.

The US corpus was wrong in the other direction: `expected_base` counted the Section 301
duty and omitted the merchandise processing fee, which is refundable on a drawback claim.
The matcher had been right all along and the corpus had been under-stating the claim by
the one component a client is least likely to have counted themselves. Neither error was
detectable in week 12, because week 12 wrote both numbers and ran neither.

### The filing identity is a row now

`POST /packaging/build` used to take the claimant in the request body — the caller told
the packager what to print on a form addressed to CBP. `claimant` is now optional and the
default comes from `tenant_profiles`, which is what makes "a second tenant with zero code
change" true rather than aspirational: the pilot's request body names a claim id and
nothing else, and the EIN, filer code, name and city land on the 7551.

The table is separate from `tenants` because the two rows have opposite lifetimes. A
`tenants` row is a tombstone that outlives the relationship by years, because
`audit_ledger.tenant_id` is RESTRICT and §163 says so. This row holds precisely what a
departing tenant is entitled to have erased, and `ON DELETE CASCADE` erases it without
disturbing the tombstone.

The IBAN is the only field validated by checksum rather than by shape, because it is the
only one whose error moves money to a stranger. A transposed pair of digits is still a
perfectly well-formed IBAN; ISO 13616 mod-97 is what catches it. That validation proves
the number was not mistyped and nothing more — not that the account exists, not that it
belongs to the tenant — and the field docs say so, because a caller who reads "validated"
as "verified" will skip the confirmation that actually matters.

### Secrets, and what moving them actually buys

`DRAWBRIDGE_JWT_SECRET`, the service token and the app role's password are no longer in
`.env`. They resolve from a gitignored `.secrets.json` through
`services/api/src/secrets.py`, with the environment as a deliberate override for an
orchestrator that injects.

This does not make them secret from anyone who can read the file, and the module says so
in its first paragraph. What it removes is specific: they are no longer printed by
`docker inspect` or `docker compose config`, no longer inherited by every child process
this container spawns, no longer in `/proc/<pid>/environ`, and no longer in the
environment blocks that crash reporters collect. What it adds is one place to rotate and a
`SecretProvider` seam — `AwsSecretsManagerProvider` is written as a working shape whose
`load` refuses and names what it would need, rather than a stub that returns empty and
lets a deployment start with nothing configured.

Two things the generator deliberately will not mint. An Anthropic key is issued by
Anthropic. A service token is a JWT signed with `jwt_secret`, so it cannot exist before
that key does and is not random in any case. Generating either would report a configured
credential and move the failure from startup to the first request that used it.

Rolling it out found the ordering bug the design predicts: `.env` still carried the old
values, compose loads `.env` into the container environment, and environment beats the
file by design — so the API resolved `dev-only-change-me` from a file it was reading
correctly. They are removed from `.env` rather than blanked, because an empty variable is
still a variable and would win the same way.

### Trace propagation was already there

Week 12 recorded that nothing propagated `traceparent` across the MCP transport. Going to
write it found the SDK does both halves: the client dispatcher injects W3C context into
the JSON-RPC `_meta` under SEP-414, and `OpenTelemetryMiddleware` is installed by default,
outermost, on every server. Verified against the running stack — a tool call made under a
client span produced `drawbridge-mcp-hts` spans in Jaeger carrying the client's trace id.

So there is no propagation code in this repository, and the deliverable is
`tests/integration/test_trace_propagation.py` instead. A property nobody wrote is a
property nobody notices losing: an SDK upgrade that drops the default middleware, or one
server built with `middleware=[]`, would return the codebase to week 12's position
silently.

### The corpus is loaded, and loading it broke classification

28,899 published lines, up from nine. Two things surfaced immediately.

The duty rate columns did not fit. `varchar(64)` was sized against a fixture where every
rate read "Free" or "2.5%"; the published schedule carries rates up to 439 characters —
the sugar lines reciting general note 15, the tobacco lines reciting an entire in-lieu-of
formula — and 105 rows overflowed. They are `Text` now, because the column was not
truncating for want of a bigger number but for want of any right one.

The serious one: **semantic retrieval collapsed.** Against the full schedule the benchmark
retrieved 1 of 10 correct subheadings in its top 10 — against the nine-line corpus it had
retrieved everything — and the nearest neighbours for "ruggedised field laptop computer"
were five machine-tool subheadings. The cause is
`description_en`, which is the ancestor chain root-first because a line whose own text
reads "Other" is meaningless without it. For 8471.30.01.00 that chain is 240 characters of
which the first 190 are the chapter heading, shared verbatim by every line under heading
8471. The model mean-pools over tokens, so the twenty characters that distinguish a laptop
from a mainframe are averaged into nothing and every sibling embeds to almost the same
point. Twenty-four flat hand-written descriptions could never have shown this.

`search_text` (migration f2b90d47ac13) holds the same chain leaf-first and capped at 200
characters, and is what gets embedded; `description_en` stays the readable one and is
still what an analyst sees and what a citation prints. The cap is a measurement, not a
taste: at 120 the immediate parent of 8471.41.01.50 is five characters too long to fit
beside a leaf reading "Other", so that line embeds as the word "Other" and sits 0.868 from
a plain-language query; at 200 the parent fits and the distance is 0.592.

Re-embedding all 28,899 lines against it moved recall at hs6 from **1 of 10 to 5 of 10**
in the top ten, and from 0 to 4 at rank 1. Median top-hit distance fell from 0.361 to
0.292 and the worst from 0.720 to 0.475. That is a large improvement and it is not a
working classifier: five of ten is not a number to put in front of a customer, and the
next suspect is the representation rather than the text — 384 dimensions of multilingual
MiniLM over 29,000 near-identical legal phrases is thin. Week 14 starts there.

The same run produced the sharpest evidence yet that the ceiling is not the control people
assume it is. Correct answers now reach out to 0.475; the one query that is not a good at
all — marine cargo insurance brokerage — sits at 0.492. Seventeen thousandths separate the
worst correct answer from pure noise, so **no value of `vector_ceiling` separates them**.
The fixture said as much in week 8 as a prediction; this is the measurement. Precision is
decided by `needs_analyst_confirmation`, and the ceiling only decides how much noise a
human has to look at.

### Recalibration measured the fixture, so the fixture was the thing that changed

The instruction was to re-run the benchmark and recalibrate `vector_ceiling` and
`CONFIRMATION_LEXICAL_FLOOR` against production volume. Neither constant changed, and the
reason is that the benchmark cannot support the measurement in the form it was in.

Nine of its ten negatives are negatives only relative to its own 24-line corpus. Live
cattle, cut roses, raw sugar, printed books and wooden pencils are all real headings in
the published HTSA — against the full schedule they are positives whose answer the fixture
never recorded. A threshold tuned by counting how many of them cross it would be measuring
the fixture going stale and reporting it as precision. One negative survives at any
volume: marine cargo insurance brokerage, which is not a good.

Five of its ten positives named ten-digit codes that do not exist in the published
schedule at all. The real HTSA splits 0901.21 eight ways on organic certification, Arabica
versus Robusta and container size — none of which appears in "roasted cofee beans, not
decafinated". So each positive now carries `expects_hs6`, and
`scripts/calibrate_thresholds.py` scores at six digits: the level a goods description
actually supports, and the level that is harmonised internationally. `expects` stays for
`tests/integration/test_tariff_benchmark.py`, which measures against the fixture's own
corpus where those exact codes do exist.

Changing a threshold on top of a broken embedding text would have been fixing the
instrument to flatter the reading.

### Running the suite logs you out of your own stack

`tests/integration/conftest.py` calls `ensure_app_role` with the suite's password, so a
full `pytest` against the same Postgres the compose stack uses rotates `drawbridge_app`
out from under the running API. Every request then fails with `password authentication
failed`, several minutes after the thing that caused it, and the API looks broken rather
than locked out.

Recorded rather than fixed, because both plausible fixes are worse than the note. Giving
the suite its own role would mean the tests no longer exercise the role the deployment
uses, which is the entire point of testing against a real role rather than a mock. Having
the suite restore the previous password would need it to know one it is deliberately not
given. The recovery is one command:

    make rls-bootstrap   # reads the password from .secrets.json, then restart the API

### What week 13 deliberately did not do

- **Configure Authentik.** Unchanged from week 12: the containers are in compose behind an
  `identity` profile, the API verifies RS256 against a JWKS URL, and no provider,
  application or property mapping is scripted. No RS256 token has ever reached this API.
- **Load CROSS.** The tariff schedule loaded; the ruling corpus did not. CBP publishes
  CROSS through a search interface with no bulk export, and `tariff_rulings` is still
  empty, so `find_rulings` has nothing to cite.
- **Encrypt a secret.** `.secrets.json` is plaintext on disk. The seam for a real manager
  exists and is unimplemented, deliberately and loudly.
- **Run n8n for real.** The HTTP nodes carry `DRAWBRIDGE_SERVICE_TOKEN` and `make up`
  bridges it from the secrets file into the container environment, which is asserted and
  still not observed.
- **File anything.** Both corpora are fiction; every figure carries a `pilot-fixture`
  box that traces to no document, and `assert_not_evidence` runs before the seeder and
  again before the pilot.

## Week 14 task breakdown

- [x] `infra/authentik_bootstrap.py` — provider, application, `tenant_id` mapping, RS256
      round trip against the running API
- [x] `VaultSecretProvider` and a real `AwsSecretsManagerProvider`; `.secrets.json` is now
      one backend of three and no longer the only one
- [x] `infra/vault_bootstrap.py` — KV v2 mount, one-path policy, AppRole, verified read
- [x] `scripts/ingest_cross.py` — CROSS is loaded; `tariff_rulings` is not empty
- [x] `services/classifier/src/search.py` — the ruling scorer, which had never returned
      a row against a real body
- [x] `services/packager/src/branding.py` — white label as a compliance surface
- [x] `services/agent/src/worker.py` — the drafting loop finally has a process
- [x] `docker-compose.onprem.yml`, `infra/caddy/Caddyfile`, `.env.onprem.example`
- [x] `README.md` — the dual-jurisdiction summary

### The debt cleared first, and what each one turned out to be

**Authentik.** Weeks 12 and 13 both recorded the same sentence: no RS256 token has ever
reached this API. `infra/authentik_bootstrap.py` creates the signing keypair, the scope
property mapping that emits `tenant_id`, the OAuth2 provider, the application and a service
account, then mints a token and verifies it *through `services.api.src.auth.decode`* — the
function every request runs, rather than a second `jwt.decode` that would only prove PyJWT
works. With `--api-url` it also sends the token to the deployed API over HTTP.

Two things came out of doing it rather than describing it. The provider needs
`redirect_uris` present even though the client-credentials grant has nowhere to redirect
to; it is set empty, because a permissive redirect URI on a provider that never uses one is
an open redirect waiting for somebody to enable the code flow. And `DRAWBRIDGE_JWT_ISSUER`
was hard-coded to `drawbridge` in the compose file, which was fine while `make token` was
the only issuer and wrong the moment a real one appeared: `iss` is the provider's own URL
and the API has to be told what it will be.

The negative half of the round trip is the half worth keeping. The same claims, re-signed
HS256 with the shared secret this service used until an OIDC URL was configured, must come
back 401. If they do not, the JWKS path was added *beside* the shared secret rather than in
place of it — and every value in `.secrets.json` is still a token-minting key. Observed:
`RS256 -> HTTP 200`, `HS256 -> HTTP 401 no verification key`.

**Secrets.** `AwsSecretsManagerProvider` was written in week 13 as a seam whose `load`
refused. It is implemented now, and so is `VaultSecretProvider` — KV v2 over AppRole,
through `httpx`, because the read is one GET and the login is one POST and a client library
for two endpoints is a supply-chain edge bought for nothing.

The design decision that matters is not which manager: it is that `default_providers`
returns the environment and **exactly one** backend. Naming Vault removes the file from the
chain entirely. A chain that fell back to disk when Vault was unreachable would start,
work, and be running on whatever stale plaintext was last checked out — which is the
failure the whole exercise exists to remove, reintroduced as a convenience.

Proved end to end rather than asserted: the API container was restarted with
`DRAWBRIDGE_SECRETS_PROVIDER=vault`, logged in with the AppRole, and resolved every secret
from `secret/drawbridge`. The `.secrets.json` mount was still present in the container and
still not consulted — a value written into Vault and absent from the file came back from
`resolve()`.

**CROSS.** Carried since week 9 on the grounds that CBP publishes no bulk export, which is
true. The search interface is a single-page application backed by a public JSON API, and
120 rulings drawn round-robin across twelve terms now sit in `tariff_rulings`, embedded.
Round-robin and not term-by-term: draining the first term before starting the second
produced a sample from four chapters under a docstring claiming twelve, which is how a
corpus quietly becomes a corpus about laptops. `BeautifulSoup` is not used, because the
endpoint returns plain text — thirty bodies sampled across six chapters carried no markup
at all, and a parser for tags that do not exist is dead weight with a supply chain.

### Loading the rulings found that ruling search had never worked

`search_rulings` scored with `similarity(subject || ' ' || body, :q)`. `similarity` is
set-symmetric — shared trigrams over the union of both sides — so a four-word query against
a four-thousand-word ruling is dominated by the denominator. Against the loaded corpus the
best score any query achieved was **0.127**, and `LEXICAL_FLOOR` is 0.15. The function
returned zero rows for every query, and had done since week 3. Three hand-written fixture
rulings with two-sentence bodies hid it completely.

`word_similarity(query, document)` is the operator for this shape: it scores the query
against the best-matching extent rather than against the whole. Over subjects it separates
cleanly — twelve goods queries each retrieved their own ruling at rank 1, and four
non-goods queries topped out at 0.314. The body is scored too, at 0.6 weight, because a
CROSS subject is not always descriptive: a protest ruling is titled "Application for
further review of protest number 1601-..." and the goods appear only in the text. The
discount is there because a long document can always find *some* matching extent — the
non-goods queries score 0.425 against bodies and 0.056 against subjects.

`LEXICAL_FLOOR` did not move, for the same reason `vector_ceiling` did not move last week.
Paraphrased goods queries — what an analyst types, as against the term CBP indexed on —
land between 0.20 and 0.25, and the non-goods queries reach 0.314. The ranges overlap and
no floor separates them. `find_rulings` returns a list for a person to read against the
article in front of them, and tuning the floor until the overlap disappeared would tune it
until the real matches disappeared too.

This is the second week running that real data broke something a fixture had been
certifying. Week 13 it was the embedding text; this week it is the lexical scorer. Both
were sized against corpora small enough that the defect could not express itself.

### White label is a compliance surface, not a logo

Every 7551 since week 4 has carried: *"Prepared by Drawbridge for filing by a licensed
customs broker. Drawbridge is not a customs broker and does not transmit to CBP."* Both
halves are true of us and load-bearing — §5 of the architecture turns on the second.
Neither is true of a licensed broker preparing their own client's claim.

So the notice is **composed** rather than substituted. `Preparer.is_licensed_broker`
selects which second sentence is true, and a deployment that sets it must supply the filer
code that makes the claim checkable — `Preparer` refuses to be constructed otherwise,
because an unverifiable claim of licensure on a customs filing is worse than no claim. A
deployment that renamed the preparer and kept our disclaimer would be worse than one that
changed nothing, because it would read as deliberate.

The certifications, the statutory citations, the form titles and the "not a CBP-issued
form" line are not brandable. They are the authority's words or facts about the document,
and a deployment able to edit them could quietly weaken a declaration somebody signs.

`preparer` rides on `PacketRequest` rather than being read from configuration inside the
renderer, so a packet regenerated in four years reproduces the notice that was on it rather
than the notice the deployment happens to carry that day.

### The on-prem stack, and why it is standalone

`docker-compose.onprem.yml` is not an overlay. An overlay would be shorter and would be a
trap: `-f a.yml -f b.yml` merges rather than replaces, so every bind mount, published port
and `--reload` in the development file survives into the deployment, and forgetting one
`-f` deploys development under a production name.

What actually changes:

- **One container binds a port.** Development publishes Postgres, MinIO, Jaeger and five
  MCP servers — convenient on a laptop, and nine unauthenticated services on a broker's
  LAN. Here Caddy terminates TLS and everything else is reachable only inside the network.
- **The data plane has no default gateway.** `drawbridge-data` is `internal: true`, so
  Postgres, Redis, MinIO, Jaeger and the five MCP servers cannot originate outbound traffic
  at all. It is the cheapest meaningful control in the file: the database cannot phone home.
- **Secrets from Vault with nothing behind them**, identity from Authentik with no
  shared-secret fallback, and `DRAWBRIDGE_ENVIRONMENT=production` so `check_secret_posture`
  refuses a placeholder rather than warning about it.
- **Migrations are a job the API waits on**, not something four uvicorn workers race.
- `cap_drop: [ALL]`, `no-new-privileges`, `read_only` with a tmpfs `/tmp` on every image we
  build, resource limits, and log rotation in the daemon rather than in a cron job nobody
  wrote.

Six credentials still reach containers as files under `./secrets`, because Postgres, MinIO
and n8n read a `_FILE` variant and cannot be taught to call a manager. That is two copies
of each secret and it is stated in `secrets/README.md` rather than glossed: the fix is a
`vault agent` sidecar templating them at start, and it is not built.

### What week 14 deliberately did not do

- **Back anything up.** `postgres-data` is a volume on one host. The retention obligation
  runs for years and a volume is not a backup. Carried, and now the most conspicuous gap.
- **Fix classification.** Still 5 of 10 at hs6 against the full schedule. Nothing this week
  touched it; the ruling scorer is a different code path.
- **Pin digests.** The on-prem file pins tags. A deployment that has been through change
  control should carry digests, and the file says so.
- **Run n8n for real.** Carried from weeks 9–13. The service token reaches the container;
  no workflow has been imported and executed.
- **File anything.** Both corpora are fiction and `assert_not_evidence` refuses to act on
  a `pilot-fixture` provenance box.

## Week 15 entry checklist

1. **Retrieval.** 5 of 10 at hs6 is the oldest unfixed number in this file. The text is no
   longer the suspect — the model is. A 384-dimension multilingual MiniLM over 29,000
   near-identical legal phrases is a thin representation; try an English-specialised model
   or retrieve-then-rerank, and measure with `scripts/calibrate_thresholds.py`.
2. **Then the thresholds.** `vector_ceiling` and `CONFIRMATION_LEXICAL_FLOOR` are still
   week 8's numbers, measured on twenty-four lines. `LEXICAL_FLOOR` is now also known to
   sit inside an overlap for rulings. None of the three moves until retrieval does.
3. **Backups and retention.** `verify_chain` on a schedule, where `postgres-data` is
   replicated, and what an offboarded tenant's archive bucket lifecycle is. Carried from
   weeks 10–14 and now the largest hole in a stack that claims a four-year obligation.
4. **A `vault agent` sidecar** for Postgres, MinIO and n8n, removing the six files under
   `./secrets` and the second copy of each secret.
5. **Import the workflows into n8n and run one for real.** Carried from weeks 9–14.
6. **A live agent run against real queue rows.** The worker exists now; it has never
   drafted a memo in a deployment.
7. **More of CROSS.** 120 rulings is a sample. Decide whether the answer is a larger
   sample, a purchased corpus, or narrowing what `find_rulings` claims to cover.
8. **A *Bayan* header-block template.** Partially blocked on **B3**.
9. **Digest-pinned images** and a signed release, so "what is deployed" has one answer.
10. Blocked externally: **B1** Fasah credentials, **B2** Resolution 28624 text, **B3** a
    real scanned *Bayan* corpus. **Parked permanently — see below.**

## External blockers — parked, permanently

**B1, B2 and B3 are commercial dependencies and will not be automated in this codebase.**
They have been carried as open items since week 2; that was right while there was a chance
a technical route existed, and it is now just a list that makes the roadmap look shorter
than it is. Each is closed here with what unblocks it and what stands in for it meanwhile.

| | What it is | Why no code closes it | What stands in |
|---|---|---|---|
| **B1** | Fasah (Saudi single-window) credentials | Issued to a licensed customs broker against a commercial registration. There is no sandbox, no self-service, and no public API to build against. `scripts/fasah_sandbox_probe.py` exists to record that, not to obtain them. | The KSA lane produces a ZATCA refund payload and stops. Nothing in this repository transmits to Fasah, and `assert_not_evidence` refuses to act on fixture provenance. |
| **B2** | The authoritative text of Ministerial Resolution 28624 | Not published in machine-readable form. Obtaining it means a Saudi counsel engagement or a paid legal database licence. | `COMPLIANCE-GCC.md` cites the GCC Common Customs Law directly and the rules engine implements Art. 174 from it. Where 28624 would refine a rule, the rule is absent rather than guessed. |
| **B3** | A real scanned *Bayan* corpus | Real declarations are a customer's commercial records. There is no public corpus, and a synthetic one cannot establish OCR accuracy on real scans. | `tests/fixtures/bayan.py` renders a synthetic RTL table. It proves the bilingual extraction path runs; it does not establish field accuracy on scanned Arabic, and `ARCHITECTURE.md` says so. |

The practical consequence: **the GCC lane is complete as far as a repository can take it.**
Further GCC work needs a customer, not a commit. Anything that appears to close one of
these without the underlying access is a fixture wearing a deployment's name, and the
correct engineering response is to leave the gap visible.

## Week 15 task breakdown

- [x] **Retrieval.** Root cause found and fixed: the corpus was embedded from
      `f"{code} {body}"`, so every document vector carried a ten-digit token no query
      contains. `search_tariff` now looks a code up instead of hoping for it.
- [x] **Embedding provenance.** `embedding_model_id` on both corpora (`a3f81c22d907`),
      stamping the text convention as well as the model. `--reembed` converges the corpus
      in place.
- [x] **The benchmark had the same defect** — `tests/golden/test_tariff_benchmark.py`
      embedded its fixture corpus as `f"{code} {description}"` too, which is why nothing
      caught this. Fixed and re-measured: the worst true positive moved 0.625 → 0.635 and
      the nearest hard negative 0.508 → 0.497, so removing the prefix *widened* the
      overlap. Fixing the text made the measurements sound; it did not make the classifier
      work.
- [x] Reranking evaluated and **rejected on measurement** — an English cross-encoder makes
      the Arabic queries worse; the multilingual one buys one position in ten for four
      seconds a query and 1.1 GB.
- [x] **Vault Agent sidecar.** `./secrets` deleted; six credentials rendered to tmpfs from
      a separate Vault path under a separate AppRole, proven live.
- [x] **Backups.** `scripts/retention.py` — `pg_dump` under S3 object lock in COMPLIANCE
      mode, plus `verify_chain` across every tenant on its own interval.
- [x] **Digest pinning.** All nine third-party images; `make pin-check` fails on drift.
- [x] **n8n, for real.** Three workflows imported, activated, and one webhook run to a
      packaged claim with a CBP 7551. Seven defects fixed to get there.
- [x] **The agent worker against live rows** — connects and selects; drafting proven with
      the model stubbed. Still blocked on an API key.
- [x] **Re-embed converged** — 28,908 lines and 117 rulings on the `.../desc` convention,
      `stale: 0`, and a second run embeds nothing.
- [x] **Re-calibrated against the full corpus.** 6/10 in the top ten (was 5/10) and 3/10 at
      rank one (was 4/10): **no measurable improvement.** The two distance ranges have
      crossed — the worst true positive is now at 0.529 and the nearest non-good at 0.467,
      so the noise is closer than the answer and `vector_ceiling` at 0.68 admits both.
      Correcting the embedding text made every future measurement sound and did not move
      the classifier. That is the finding, not a disappointment: the prefix had to go
      before anything measured over it could be believed.
- [ ] **Thresholds.** Deliberately not moved. See below.

### What week 15 deliberately did not do

- **Move a threshold.** `vector_ceiling` and `CONFIRMATION_LEXICAL_FLOOR` are still week
  8's numbers and `LEXICAL_FLOOR` is still 0.15. The re-measurement is now in hand and it
  says no single value works: the worst true positive (0.529) sits *further* than the
  nearest thing the corpus cannot answer (0.467), so any ceiling that keeps recall admits
  the noise. That is not a number to tune, it is a retrieval problem to fix, and moving a
  constant would only decide which of the two failures to have.
- **Fix the pipeline's inability to fail.** Every n8n HTTP node sets `neverError: true`, so
  a 500 from `/claims/persist` became `{data: "Internal Server Error"}`, the run continued
  through packaging, returned 200 and recorded `success`. Real, understood, and a
  structural change to a 22-node graph that wants doing deliberately rather than at the end
  of a long week.
- **Sign anything.** Digests say the bytes have not changed since somebody wrote them down,
  not that they were trustworthy then.
- **Load more of CROSS.** Still 120 rulings.

## Week 16 entry checklist

1. **A pipeline that can fail.** Remove `neverError` or gate on status after every HTTP
   node. Today a run that 500s reports success and packages a claim built on nothing.
   Largest correctness hole in the repository.
2. ~~**The model, on evidence that now means something.**~~ *Done in week 16 — see
   §21. The claim was half right: the encoder finds the answer and cannot order it, so the
   fix was a second stage rather than a bigger first one.* Retrieval is 6/10 at ten and 3/10
   at rank one over a corpus the queries can finally reach, and the distance ranges have
   crossed — noise nearer than the answer. No threshold fixes that. Re-run the reranker
   comparison in `ARCHITECTURE.md` §20.1 against the corrected space, and test the standing
   claim directly: that a 384-dimension multilingual encoder is too thin for 29,000
   near-identical legal phrases. Two typo queries and one of two Arabic queries retrieve
   nothing at all, which is the shape of an encoder problem rather than a threshold one.
3. ~~**Thresholds last, and only if retrieval moves.**~~ *Retrieval moved; the
   thresholds still did not, and week 16 recorded the measured bound instead.* `vector_ceiling`,
   `CONFIRMATION_LEXICAL_FLOOR` and `LEXICAL_FLOOR` together, in one commit, with the
   measurement in the message.
4. ~~**A restore drill.**~~ *Done in week 16 — `retention.py restore`, weekly on the
   schedule loop, 15 seconds end to end.* A backup nobody has restored is a file. `pg_restore` into a scratch
   database, run the golden fixtures against it, and record how long it took.
5. **An Anthropic key in a deployment**, so the agent worker's drafting runs live rather
   than to a stub.
6. **Test the deployment file the way the workflows were tested.** `docker compose config`
   proves the on-prem YAML parses; nothing has ever started it. Everything week 15 found
   came from executing an artefact that had only been validated.
7. **More of CROSS**, and a decision about whether the answer is a larger sample, a
   purchased corpus, or narrowing what `find_rulings` claims to cover.
8. **Signed images**, so "what is deployed" has a provenance answer and not just an
   identity one.
9. **A *Bayan* header-block template** — as far as B3 allows, which is not far.

## Week 16 task breakdown

- [x] **Retrieve-then-rerank.** `Reranker` and `FastEmbedReranker` in `embeddings.py`,
      a `_rerank` stage in `search.py`, opt-in on `/classification/run` and
      `classify_with_embedding`. 50 candidates from pgvector, re-scored by
      `jinaai/jina-reranker-v2-base-multilingual`, blended `0.70 / 0.30` against retrieval.
- [x] **The depth and the weight are measured, not chosen.** Recall stops improving at
      depth 50 (6/10 at 25, 8/10 from 50 to 500). The weight has a plateau from 0.50 to
      0.85, all scoring 4/10 at rank one.
- [x] **`BAAI/bge-reranker-base` measured and rejected** — multilingual, a third the
      latency, and rank-1 falls from 3 to 2. Being multilingual is necessary and not
      sufficient.
- [x] **The number moved.** 9/10 at hs6 in the top ten and 5/10 at rank one, against 6/10
      and 3/10 one-stage. First movement since week 13, and the first that was measured
      before the change rather than after it.
- [x] **Restore drill.** `retention.py restore` — newest object, digest checked against
      what was recorded at write time, `pg_restore` into a scratch database, every tenant's
      hash chain re-verified **inside the restored copy**, then dropped. Weekly on the
      `schedule` loop. Run for real: 28,908 lines and 280 ledger entries back in 15 seconds.
- [x] **A real defect found by running it**: `str(sqlalchemy.URL)` masks the password, so
      the first drill could not connect using a URL that read correctly in the traceback.
- [ ] **Thresholds.** Deliberately not moved, again. See below.

### What week 16 deliberately did not do

- **Move a threshold.** `vector_ceiling` stays 0.68, `CONFIRMATION_LEXICAL_FLOOR` 0.20,
  `LEXICAL_FLOOR` 0.15. What changed is that the ceiling now has a measured lower bound:
  under reranking it gates the *candidate pool* rather than the display, the worst correct
  candidate in the depth-50 pool sits at 0.601, and anything below about 0.61 is
  demonstrably wrong. The obvious new idea — a floor on reranker confidence — does not
  survive the data: the one true non-good scores 0.0735 and a true positive scores 0.0888.
- **Fix the pipeline's inability to fail.** Still every n8n HTTP node with
  `neverError: true`. Carried from week 15 and now the oldest item on this list, which is
  the argument for doing it first rather than a reason it keeps being deferred.
- **Fix `search_text`.** 596 ten-digit US lines have no ancestor chain, and the two
  benchmark queries that miss at any depth are that defect. It is a re-ingest, not a
  constant.
- **Sign anything, or load more of CROSS.** Unchanged from week 15.

## Week 17 entry checklist

1. **A pipeline that can fail.** Third week on this list. Remove `neverError` or gate on
   status after every HTTP node; today a run that 500s reports success and packages a claim
   built on nothing.
2. ~~**Repair `search_text`.**~~ *Done in week 17 — and the claim below is wrong. It
   was 1,821 lines, not 596, and repairing them changed retrieval by one query in fifty.
   See §22.3: hs6 scoring counts a subheading found if any descendant ranks, and every
   repaired line had a sibling carrying the full text.* The ancestor chain is missing on 596 leaf lines, which is
   exactly where the two unreachable benchmark queries land — 8471.41 is what a desktop PC
   classifies under and its text never says it is a computer. Re-ingest with the chain
   intact, re-embed, re-measure. This is the ceiling on retrieval and it is a data fix.
3. ~~**Then re-measure the reranker over the repaired corpus.**~~ *Done, and it did not
   move for that reason. It moved because the query set got five times bigger and more
   honest.* The 8/10 pool ceiling is a property of the corpus, not of the model, so it
   should move.
4. ~~**A bigger labelled set.**~~ *Done in week 17 — fifty positives, fifteen
   negatives, every label resolved out of the published schedule.* Every number in §21 rests on ten queries. 4/10 versus 3/10 is
   one query, and the honest reading of a one-query difference is that it is not a
   difference. Fifty labelled queries would make the next threshold decision defensible.
5. **Reranker latency.** 2.2 s median and 8.5 s worst case is fine for an analyst looking
   at one line and impossible for a 200-line entry. Either batch the forward passes, cache
   per (query, code), or accept that it is an interactive-only feature and say so in the
   API.
6. **An Anthropic key in a deployment**, so the agent worker's drafting runs live.
7. **Start the on-prem file.** Still only ever `docker compose config`-ed.
8. **More of CROSS**, and **signed images**.

## Week 17 task breakdown

- [x] **Repaired the ingest hierarchy.** `retrieval_text` capped the whole string
      including the leaf, so 1,821 lines (not the 596 estimated — that count was
      ten-digit rows only) embedded with no ancestor at all. 0301.93.02.90 embedded as
      the word "Other".
- [x] **And found the obvious fix half wrong.** Exempting the parent unconditionally
      re-adds the shared heading to 1,079 lines whose leaf already identifies the good,
      which is the week 13 dilution defect coming back through the exemption. The shipped
      rule exempts the parent only when it is not the root of the chain: 742 lines gain a
      discriminator, 1,079 keep their leaf undiluted.
- [x] **Measured it, and it does not move retrieval.** Unrepaired 42/50 top-ten,
      unconditional 41/50, proximate-only 41/50. The week 16 roadmap called this defect
      "the ceiling on retrieval" and it is not the ceiling on anything the benchmark
      measures — hs6 scoring counts a subheading found if any descendant ranks, and every
      repaired line had a sibling carrying the full text.
- [x] **A real defect found on the way**: the upsert rewrote `search_text` and left
      `embedding` alone, and `--reembed` keys on the model id, which had not changed. A
      corpus could be updated and its index silently not. Now nulled on text change, which
      is also what made this week's re-embed 742 rows instead of 28,899.
- [x] **Fifty positives, fifteen negatives.** Every new label resolved out of the
      published schedule rather than written from memory. Four rewritten mid-week for
      colliding with a line the fixture already held. Five service negatives added — the
      only kind that survives a change of corpus. `survives_full_schedule` is a field now,
      not a query string hardcoded in a script.
- [x] **Reranker re-measured at five times the evidence**: +10 in the top ten and +9 at
      rank one, against week 16's +3 and +2 on ten queries.
- [ ] **Rank-1 above 85%.** Not met, and not close: **52%**. See below.
- [ ] **Thresholds.** Not moved, fourth week running. See below.

### What week 17 deliberately did not do

- **Hit the 85% rank-1 target.** Measured 52%, with top-ten recall at 88%. Nothing in the
  previous weeks predicted 85% — the ten-query set said 50%, so 52% on fifty confirms the
  earlier number rather than falling short of it. The gap is not a ranking problem:
  fourteen of the eighteen queries that land in the top ten but not first are outranked by
  a **sibling subheading differing on a stated qualifier** — 750 ml against 2 litres, 2 mm
  against 10 mm, grey cement against white. Neither a bi-encoder nor a cross-encoder does
  arithmetic or resolves a negation, and a third stage of the same kind will not fix it.
- **Move a threshold.** `vector_ceiling` stays 0.68, `CONFIRMATION_LEXICAL_FLOOR` 0.20,
  `LEXICAL_FLOOR` 0.15. The worst true positive is still 0.6348 with 0.0452 of headroom —
  but it is now the worst of fifty rather than the worst of ten, so the same value rests
  on five times the evidence.
- **Fix the pipeline's inability to fail.** Still every n8n HTTP node with
  `neverError: true`. Fourth week on this list. It is now unambiguously the oldest
  outstanding defect in the repository and it should go first in week 18.
- **Anything about auto-confirmation.** See below — it turns out there is nothing to
  measure yet.

### What the bigger corpus exposed

1. **Nothing is auto-confirmed in production.** All fifty calibration queries return
   vector-only against the 28,899-line schedule; the lexical path contributes to none of
   them, so `needs_analyst_confirmation` is true for every classification the system
   currently produces. That is the safe direction to be wrong in, and it means the
   confirmation rule — three weeks of design, its own benchmark, its own tests — has never
   been exercised at volume.
2. **One integration test was measuring the fixture rather than the rule.** Growing the
   corpus from 24 lines to 64 made the week 9 "wooden lead pencils" case vanish: ten hits
   out of twenty-four is most of a corpus and ten out of sixty-four is not, so the lexical
   and vector paths stopped overlapping at all. No query in the fixture now produces a
   `both` hit. The test constructs the hit now and asserts the rule.

## Week 18 entry checklist

1. **A pipeline that can fail.** Fourth week on this list. Remove `neverError` or gate on
   status after every HTTP node; today a run that 500s reports success and packages a
   claim built on nothing. Nothing else on this list is as old or as cheap.
2. **Find out why the lexical path never fires at volume.** Item 1 above is either a
   tuning problem, a query-preprocessing problem, or evidence that trigram similarity over
   28,899 lines cannot reach the top ten — and it decides whether the confirmation rule is
   a working safety property or a decoration.
3. **Sibling disambiguation, if anything is to be done about rank-1.** The failure is
   numeric and negation-shaped, not semantic. A rule that extracts stated quantities from
   the query and filters candidates whose text contradicts them would address fourteen of
   the eighteen near misses; a bigger model would address none of them.
4. **Latency.** 2.8 s median and 7.5 s worst case, unchanged. Batch the forward passes,
   cache per (query, code), or declare reranking interactive-only in the API.
5. **An Anthropic key in a deployment**, so the agent worker's drafting runs live.
6. **Start the on-prem file.** Still only ever `docker compose config`-ed.
7. **More of CROSS**, and **signed images**.

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

Recordkeeping (wk 10–11) after the pipeline rather than alongside the schemas, and week 10
is the argument for that too. A ledger built in week 2 would have recorded the events week 2
could imagine. Built after a run exists, it records the events the run actually produces —
and the same ordering is what made the provenance requirement enforceable: it is easy to
require a bounding box on every figure when nothing yet produces figures, and the
requirement means something only once there is a pipeline that has to satisfy it. The
week 9 e2e had to grow a second document per case to comply, which is a real cost the
week 2 version of this decision would have hidden.

Multi-tenant hardening (wk 11–12) after the ledger rather than at the schema, and the same
argument holds a third time. Every table has carried `tenant_id` since week 2 and every
query has filtered on it, so RLS in week 2 would have looked like a formality — ten policies
over a schema nobody had written a wrong query against yet. Built after ten weeks of query
paths exist, it is a claim with something to check: the entry points that never see a tenant
are enumerable, they turned out to be four, and each needed a decision rather than a policy.
The one thing week 2 would have got right and week 11 nearly got wrong is the role: the
control is worth nothing while the services connect as the owner, and that is not visible
from the schema — only from the DSN.

Identity (wk 12) after row-level security (wk 11) rather than before it, which is the reverse
of the usual order and was the right way round here. Authentication first would have produced
a verified caller with nowhere to put the answer: every query would still have filtered on a
tenant id in application code, and the token would have been one more input to a filter
somebody has to remember to write. Built second, it has exactly one job and one join —
supply the value that goes into `tenant.id` — and the enforcement underneath it had already
been proven against a second tenant. The evidence that the ordering was right is how small
the week 12 diff is at the point where the two meet: one function, called at seven route
entry points, and nothing in the policies changed at all.

The pilot (wk 13) after the corpus load rather than before it, which cost a week and was
worth it. A pilot run against nine tariff lines would have passed: the matcher does not
consult the schedule to allocate duty, so both corpora reach a packet either way. What the
full corpus produced was the discovery that classification does not work at volume — which
a green pilot on a toy corpus would have hidden behind a green pilot. The ordering that
matters here is not pilot-then-corpus or corpus-then-pilot; it is that both ran in the same
week, so the one that passes could be checked against the one that does not.

Debugging as a milestone (wk 15) rather than as maintenance, and it paid for itself in the
first hour. Week 15 was scheduled to *improve* five things — retrieval, secrets, backups,
digests, orchestration. What it actually did was execute five artefacts that had only ever
been validated, and every one of them was broken in a way its validation could not see: the
embedding text was valid text, the workflow files were valid JSON, the image tags were
valid tags, the generated secret was a valid secret. Seven weeks of "5 of 10 at hs6" turned
out to be a string concatenation, and six weeks of "import the workflows" turned out to be
seven defects deep.

The ordering lesson is not "test more". Each of these had a test that passed. It is that a
check on an artefact's *form* certifies nothing about its *use*, and the gap between the
two is exactly where a roadmap item that says "run it for real" gets deferred to next week.
Weeks 13 and 14 each found one defect of this kind and named it in passing; week 15 found
five and is the week that should have been scheduled after week 9.

The debt before the milestone (wk 14), which is the reverse of every other week here and
was right. The three carried items — Authentik, a real secrets manager, CROSS — are exactly
the three things an on-prem deployment cannot be honest without: a stack that shipped with
a shared-secret fallback, a plaintext file and an empty ruling table would have been a
hardened deployment of a development configuration. Clearing them first also produced the
week's most useful finding, because loading CROSS is what showed that ruling search had
never returned a row. A milestone built on top of unpaid debt tends to certify the debt.

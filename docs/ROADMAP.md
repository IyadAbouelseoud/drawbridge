# DRAWBRIDGE — 14-Week Roadmap

> Persistent context. Read with `ARCHITECTURE.md` and `COMPLIANCE-GCC.md` at the start
> of any session.

## Status

| | |
|---|---|
| Current week | 12 |
| Scope | **Dual-jurisdiction: US (CBP) + GCC/KSA (ZATCA)** as of week 2 |
| Current milestone | **The caller** — Authentik-shaped identity, traced writes, pilot corpora |
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

## Week 13 entry checklist

1. **Run a pilot corpus end to end.** `make pilot-seed` writes both trigger payloads and
   nothing has yet pushed one through the pipeline. This is the week's own scaffolding, and
   the first thing to spend.
2. **An Authentik blueprint** — provider, application, and a property mapping that emits
   `tenant_id` — plus one real RS256 round trip against the API. The API side is done; the
   identity provider side is not configured at all.
3. **Secrets out of the environment.** The last third of the week 11–12 milestone.
4. **Full-volume corpus load** — the ~19,000-line USITC schedule and the CROSS body. Carried
   from weeks 9, 10, 11 and 12; the oldest open item, and it gates (5).
5. **Re-run the tariff benchmark against that corpus.** `vector_ceiling` 0.68 and
   `CONFIRMATION_LEXICAL_FLOOR` 0.20 were both measured against twenty-four lines.
6. **Tenant profiles** — EIN, CR number, broker code, IBAN. Carried from weeks 9–12, and
   now concrete: two pilot tenants exist and neither has any of these, so the packager still
   cannot address a second claimant.
7. **A *Bayan* header-block template.** Partially blocked on **B3**.
8. **Import the workflows into n8n and run one for real.** Carried from weeks 9, 10 and 11.
   The HTTP nodes now send `DRAWBRIDGE_SERVICE_TOKEN`, which is asserted and not observed.
9. **A live agent run against real queue rows.** Carried from week 8.
10. **A retention job**: `verify_chain` on a schedule, and an answer to where the ledger is
    replicated. Carried from weeks 10 and 11.
11. **Propagate `traceparent` across the MCP transport**, so an analyst tool call joins the
    trace of the request that prompted it.
12. Blocked externally: **B1** Fasah credentials, **B2** Resolution 28624 text, **B3** a real
    scanned *Bayan* corpus.

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

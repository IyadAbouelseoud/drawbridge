# DRAWBRIDGE — 14-Week Roadmap

> Persistent context. Read with `ARCHITECTURE.md` and `COMPLIANCE-GCC.md` at the start
> of any session.

## Status

| | |
|---|---|
| Current week | 7 |
| Scope | **Dual-jurisdiction: US (CBP) + GCC/KSA (ZATCA)** as of week 2 |
| Current milestone | Embedding pass, OCR gating, ERP BOM source, PSC + §1520(d) |
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

## Week 8 entry checklist

1. **Full-volume corpus load.** The embedding pass is proven; what remains is downloading
   the ~19,000-line USITC schedule and the CROSS body and running it. Bounded by download
   and compute, not by design.
2. **A real embedding backend.** `services/classifier/src/embeddings.py` ships a
   deterministic hashing vectorizer and an Ollama client. The hashing backend is honest
   about being lexical-only; a semantic backend needs a model endpoint decision.
3. **Real scanned *Bayan* corpus** to calibrate the OCR floor against measured error
   rather than a defensible default.
4. **A real ERP connector.** `erp_mock.py` fixes the contract — SAP/Oracle field shapes,
   multi-level explosion, yield and scrap. Swapping in a live connector changes the
   source, not the shape.
5. **The agent layer** (weeks 9-10): Claude scoped to exactly the residue the rules
   engine cannot decide.
6. Blocked externally: **B1** Fasah credentials, **B2** Resolution 28624 text.

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

The pilot (wk 12–13) targets a backward-looking claim rather than live flow: a 5-year
lookback has a known answer set and no operational dependency on the customer's current
quarter.

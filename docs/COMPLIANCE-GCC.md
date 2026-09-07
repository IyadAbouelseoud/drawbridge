# GCC / Saudi Arabia — Compliance Rule Reference

> Persistent context. Primary-source rules for the KSA/GCC lane of the compliance engine.
> Every constant in `services/rules/` traces to a citation in this file. Read with
> `ARCHITECTURE.md` before touching jurisdiction logic.

**Verification status.** Sections 1–2 are transcribed from the GCC Secretariat's published
Common Customs Law PDF and are authoritative. Sections 3–5 are drawn from ZATCA's own
pages plus Big-4 and law-firm advisories; they are directionally reliable but the article
numbers of ZATCA Resolution 28624 were not obtainable in English. Treat section 4 numbers
as needing confirmation against the Arabic Umm Al-Qura text before a live filing; the
packager enforces this automatically by emitting every 28624 citation as an
`ANALYST_REVIEW` placeholder (§8.4.1).

---

## 1. Statutory basis

| Instrument | Provision | Effect |
|---|---|---|
| GCC Common Customs Law | **Article 97** | Customs duties collected on foreign goods are wholly or partially refunded on re-export, per the Rules of Implementation |
| GCC Rules of Implementation | **Article 16** | The nine operative drawback controls (below) |
| GCC Rules of Implementation | **Article 15(c)** | The import declaration number **must be affixed to the re-export declaration** |
| GCC Common Customs Law | **Article 174** | Absolute bar: no refund claim accepted for duties paid more than **three years** ago |
| GCC Common Customs Law | **Article 175** | Administration may destroy customs records after **five years** |
| KSA Ministerial Decision | **No. 3852 (2 July 2021)** | National Rules of Origin for GCC preferential treatment |
| ZATCA Resolution | **No. 28624 (eff. 29 Dec 2023)** | Rules of Customs Procedures |

> Article 97 text: *"Customs duties 'taxes' collected on the foreign goods shall be totally
> or partially refunded at re-exportation according to the practices and conditions set
> forth in the Rules of Implementation."*

---

## 2. GCC drawback controls — Rules of Implementation, Article 16

Nine cumulative conditions. **All must hold**; failing any one voids the claim.

| # | Control | Encoded as |
|---|---|---|
| 1 | Re-exporter must be the person in whose name the goods were imported, **or** a person who can definitively prove purchase | `claimant_is_importer_of_record` \| `proof_of_purchase` |
| 2 | Value of re-exported goods **≥ USD 5,000** (or local-currency equivalent) | `GCC_MIN_REEXPORT_VALUE_USD = 5_000` |
| 3a | Goods re-exported within **one Gregorian year of the date of duty payment** | `GCC_MAX_PAYMENT_TO_REEXPORT` |
| 3b | Claim filed within **six Gregorian months of the date of re-export** | `GCC_MAX_REEXPORT_TO_CLAIM` |
| 4 | Single consignment; part shipments allowed only where proven to belong to the same consignment | `consignment_id` + `is_partial_shipment` |
| 5 | Goods not locally used after import; same condition as imported | `unused_and_unaltered` |
| 6 | Refund limited to duties **actually paid** | refund base = paid duty, not assessed |
| 7 | Refund issued after re-export and verification of all re-export documents | state `HANDED_OFF → FILED` gate |
| 8 | The approved unified (single) customs declaration must be used for the re-export | `declaration_kind = UNIFIED_GCC` |
| 9 | Controls apply on implementation of single point of entry and common collection | — |

### 2.1 Three corrections to prior assumptions

These were carried into the brief as approximations and are wrong in ways that change the
arithmetic. The engine uses the primary-source form.

| Assumed | Actual | Why it matters |
|---|---|---|
| 365-day re-export window from import date | **One Gregorian year from the date of duty payment** | The anchor is the payment date, not the import or entry date — these differ whenever duty is deferred (ZATCA permits up to 30 days' postponement against guarantee). A leap year also makes "one Gregorian year" 366 days |
| 180-day claim filing deadline | **Six Gregorian months from the date of re-export** | Calendar months, not a fixed day count: 181–184 days depending on the months spanned. A 180-day constant files late in most of the year |
| *(not stated)* | **USD 5,000 minimum re-export value** | A hard eligibility gate with no US analogue. Claims below it are void, so it must be screened before any extraction spend |

### 2.2 The decisive structural difference: no substitution

Controls 4, 5 and Article 15(c) together require the re-exported goods to be **matched to
their own import declaration** as a single identified consignment in unaltered condition.

**The GCC has no substitution drawback.** There is no equivalent of TFTEA's 8-digit HTS
substitution — `HTSCode.substitution_key` is meaningless in this jurisdiction and must
never be reachable from the GCC matching path.

Consequence: the matcher is not one engine with a config flag. It is two strategies behind
one interface —

- **US**: combinatorial allocation over a substitution-eligible pool (CP-SAT earns its keep).
- **GCC**: a declaration-linkage walk. Each re-export declaration resolves to exactly one
  import declaration. Cheaper, stricter, and unforgiving of a wrong link.

### 2.3 Refund rate

US drawback refunds **99%** of duties and fees. GCC Article 16 §6 limits the refund to
"the customs taxes 'duties' that were actually paid" and prescribes no percentage haircut,
so the GCC rate is **100%** of duty actually paid.

`DRAWBACK_REFUND_RATE` therefore cannot remain a module constant — it is a property of the
jurisdiction. See `packages/schemas/src/drawbridge_schemas/jurisdiction.py`.

### 2.4 What is *not* recoverable through this lane

KSA import **VAT (15%)** is not customs duty. It is recovered through the taxpayer's VAT
return as input tax, not through an Article 97 drawback claim. Excise tax has its own
refund procedure. Mixing either into a drawback claim is a filing error — the schema keeps
`vat_paid` and `excise_paid` on the entry line for reconciliation, and excludes both from
`total_recoverable_base` under the GCC profile.

---

## 3. KSA National Rules of Origin — Ministerial Decision 3852

A distinct recovery lane. Where GCC-preferential treatment was available but not claimed
at import (or was denied pending proof), duty is paid or guaranteed and refunded once
origin is established.

**Qualifying conditions:**

| Requirement | Threshold |
|---|---|
| Valid Certificate of Origin | Required |
| Direct shipment from the GCC country of manufacture | Required — transit through a non-GCC country permitted only for shipping, handling, and preservation |
| Local value added | **≥ 40%** |
| Workforce localization at the manufacturing entity | **≥ 25%** |
| Offset flexibility | The two may trade off, provided local value added **≥ 20%** *and* localization **≥ 10%** |
| GCC free-zone manufacture | **Excluded** from preferential treatment |

**Documentary set, due within 90 days of clearance:**

1. Certificate of Origin
2. Copy of the customs declaration (*Bayan*)
3. National certificate from the competent authority in the GCC country of origin
4. Valuation certificate from the country of origin
5. Proof of payment of the goods' value
6. Bill of lading
7. IBAN (for refund disbursement, paid in SAR)
8. Copies of invoices
9. Copy of the bank guarantee

The value-added percentage must be certified by a **licensed public accountant operating
in Saudi Arabia**. Verification may include physical inspection of the manufacturing
facility.

**Mechanics:** post a bank guarantee covering duties and taxes at import → file the refund
request through ZATCA e-Services → ZATCA and the competent authority verify → refund in SAR.

---

## 4. ZATCA Rules of Customs Procedures — Resolution 28624

*Article numbers unconfirmed in English; verify against the Arabic Umm Al-Qura text.*

- A re-export declaration **must be linked to the original import declaration** to claim a
  refund of the corresponding duties. This is the operative Saudi implementation of GCC
  Article 15(c) and the single most important field in the whole GCC lane.
- Moving goods from KSA into a **Special Economic Zone** is treated as a re-export: a
  re-export declaration linked to the initial import declaration supports the refund.
  SEZ-to-SEZ transfers need prior ZATCA approval.
- Duty payment may be **postponed up to 30 days** against a bank or cash guarantee. This
  is what decouples the import date from the duty-payment date, and therefore from the
  Article 16 §3(a) re-export clock.
- Original documents must be **retained five years** even where filing is electronic.
- Re-export of harmful or non-conforming goods, where approved by ZATCA and duty was paid
  at import, supports a refund claim.

---

## 5. Fasah — document sourcing

**Fasah** (فسح) is the Saudi national single window, operated by Tabadul under ZATCA.
Manifests, customs declarations (*Bayan*), and permits flow through it, and it federates
SASO, SFDA, Ministry of Commerce, and port authorities.

The *Bayan* is the KSA customs declaration and the GCC-lane analogue of the CBP 7501: it
carries the declaration number, HS code, duty assessed and paid, importer identity, and
consignment references.

**Access posture — mirrors the ACE rule.** Drawbridge ingests **client-exported Fasah/Bayan
records and broker feeds**. It does not scrape the Fasah portal and does not hold client
portal credentials. Where a client's broker has host-to-host integration, that feed is the
preferred source. This is the same constraint that governs ACE (`ARCHITECTURE.md` §5.2) and
is non-negotiable for the same reason.

---

## 6. Output target — ZATCA e-Services

Path: **ZATCA e-Services → Customs Services → Customs Duty Refund Request**
(`zatca.gov.sa/en/eServices/Pages/eServices_252.aspx`, live since 2022-01-10, no fee).

Stated eligibility is thin: the applicant must be the importer, the customs declaration's
duties must be fulfilled, and "all documents related to the refund eligibility" must be
provided. ZATCA publishes no field-level schema, no SLA, and no submission deadline of its
own — the binding deadlines are the GCC Article 16 §3(b) six-month clock and the Article
174 three-year bar.

`services/packager` therefore emits, for the GCC lane:

1. A **refund request payload** — importer identity, IBAN, original import declaration
   number, linked re-export declaration number, duty paid, amount claimed, currency SAR.
2. An **evidence bundle** — the nine-document origin set where the lane is origin-based,
   or the import/re-export declaration pair plus proof of non-use where it is drawback.
3. A **derivation trail** — every figure to its source span, per the global provenance
   invariant.

Because no machine schema is published, the payload is modelled as a typed Pydantic object
rendered to both a human-completable form and JSON. When ZATCA publishes an API, only the
renderer changes.

---

## 7. Valuation and currency conversion — RESOLVED

Week 2 left the FX reference date open. Primary source settles it.

**GCC Common Customs Law, Rules of Implementation of Valuation, Article 1(I)(6):**

> *"The time of payment of the customs taxes 'duties' shall be the time approved for
> currency exchange rate."*

The conversion rate is fixed at the **duty-payment date**. Not the invoice date, not the
declaration date, not the date of re-export. This is the same anchor Art. 16 §3(a) uses
for the re-export window, so one date drives both the eligibility clock and the currency
conversion.

Related provisions from the same article:

| Provision | Effect |
|---|---|
| Art. 1(I)(5) | Freight, insurance and other charges are added to customs value **until arrival at the port of destination** — a CIF basis, not FOB |
| Art. 1(I)(7) | Discounts agreed after the date of importation are ignored; so are credit balances from earlier consignments |
| Art. 1(I)(8) | The WTO Valuation Agreement governs interpretation |
| Art. 1(I)(1) | Where final valuation is prolonged, goods clear against a cash deposit |

### 7.1 Why there is no SAMA rate fetch

The obvious implementation — call SAMA for the rate on the relevant date — is the wrong
one, for three reasons that compound:

1. **SAR/USD is a policy peg, not a market rate.** SAMA has held 1 USD = 3.75 SAR since
   1986. There is no daily rate to look up; a "live" fetch would return a constant while
   adding a network dependency.
2. **SAMA publishes no machine-readable feed.** It releases monthly period-average and
   end-of-period tables for twelve currencies. Every API offering "SAMA rates" is a
   third-party mirror, typically trailing the calendar by around two months. An
   unofficial mirror is weaker evidence for a filing than the documented peg.
3. **A filed figure must reproduce years later.** A network call at match time makes the
   claim non-reproducible: re-running a 2024 claim in 2029 would hit a different endpoint,
   a different mirror, or nothing at all. That breaks the §163 / Art. 175 recordkeeping
   posture the whole system is built to satisfy.

**What is implemented instead** (`services/rules/src/fx.py`): a date-aware
`RateProvider` interface with the peg as one documented, cited entry, and a table
provider for dated rates where a real conversion is needed. Rates resolve **as at the
duty-payment date** per Art. 1(I)(6). Where a declared value is in a third currency with
no rate on file, the claim routes to `ANALYST_REVIEW` rather than being converted on a
guess.

The hardcoded `SAR_PER_USD` constant in `gcc_linkage.py` is gone.

### 7.2 The threshold is now enforced strictly

Week 3 accepted claims within 10% of the USD 5,000 minimum on the theory that the
valuation basis was unresolved. Art. 1(I)(5) resolves enough of it — customs value on a
CIF basis, converted at the payment-date rate — that the band is no longer justified.
Art. 16 §2 says "shall not be less than five thousand US dollars", and a threshold with a
soft edge is not the threshold the article states.

Borderline claims are **rejected**, not silently accepted, and the rejection carries the
computed USD figure and the shortfall so an analyst reviewing the queue can see exactly
how close it came. Strict enforcement plus visible near-misses beats a quiet acceptance
that would surface as a ZATCA rejection months later.

---

## 8. Open questions — week 5 resolution

Three of the four were closed against primary source. The fourth could not be, and the
reason is recorded rather than papered over.

### 8.1 Threshold basis — RESOLVED

**Question:** does the USD 5,000 minimum apply to the CIF import value or the re-export
value?

**Answer: the re-export value, computed on the Article 28 basis.** Two provisions settle
it together.

> **Rules of Implementation Art. 16 §2** — *"The value of the **re-exported foreign
> goods** for which the customs taxes 'duties' are to be refunded shall not be less than
> five thousand US dollars (or its equivalent in the local currency)."*

> **GCC Common Customs Law Art. 28** — *"The value of the exported goods is that
> indicated in the customs declaration **plus all the costs until arrival of the goods at
> the customs office**."*

So the threshold is screened against the re-export declaration value, and that value is
declared value plus costs to the customs office.

**This is not CIF, and it is not bare FOB.** The two valuation bases in the law point in
different directions and must not be confused:

| | Provision | Basis | Includes |
|---|---|---|---|
| **Import** valuation | Valuation Art. 1(I)(5) | CIF | freight, insurance and charges **to the port of destination in the GCC** |
| **Export** valuation | Common Customs Law Art. 28 | FOB **plus inland costs** | costs only **as far as the customs office** — not onward international freight or insurance |

Using the import CIF figure would overstate the re-export value by the inbound freight
and insurance, passing claims that should fail. Using bare FOB would understate it by the
inland leg, failing claims that should pass. Both errors are silent.

**Implementation.** `ExportLine.declared_value` carries the Art. 28 figure and is
documented as such. `valuation_basis` records which basis the figure was captured on, so
a claim assembled from a source that only supplied an FOB or CIF number is visible rather
than being screened on the wrong footing.

Conversion to USD runs at the duty-payment date per Valuation Art. 1(I)(6) — §7 above.

### 8.2 Partial consignments — RESOLVED IN LAW, OPEN ON THE PLATFORM

**Question:** may a partial re-export be linked to a full-quantity import *Bayan*?

**In law: yes, with proof.** Two provisions govern.

> **Rules of Implementation Art. 16 §4** — *"The foreign goods to be re-exported shall be
> of a single consignment for ease of identification and matching with the importation
> documents; **however, a single consignment may be re-exported in part shipments once it
> is definitely proven for the customs administration that such shipments constitute a
> part of the same consignment**."*

> **Common Customs Law Art. 44(b)** — *"A single consignment may not be split. However,
> for acceptable reasons, **the director general may allow such splitting**, provided
> that such splitting shall not result in a loss to the treasury."*

Part shipments are therefore permitted but not automatic: they need evidence tying the
shipment to the consignment, and Art. 44(b) reserves discretion to the director general.
The engine's `consignment_id` requirement on partial shipments is the Art. 16 §4 proof
obligation, and the same-consignment check across re-exports against one declaration is
the Art. 44(b) no-loss-to-treasury guard.

**On the platform: undetermined, and it will stay that way until tested.** Whether Fasah
*technically* accepts a re-export declaration whose quantity is less than the linked
import *Bayan* line is a platform behaviour, not a legal question. It is not documented
in any public ZATCA or Tabadul material, and guessing at it from the statute would be
inferring an implementation from a permission.

> **Phase 6 action — live Fasah sandbox test.** Submit a re-export declaration for a
> partial quantity against a full-quantity import *Bayan* and record: whether the link is
> accepted at all; whether the residual quantity remains available for a second
> re-export; whether ZATCA requires a prior ruling under Art. 44(b); and what the
> platform returns when the cumulative re-exported quantity would exceed the import line.
> Until that test runs, partial-shipment claims route to `ANALYST_REVIEW` rather than
> being filed on an assumption.

### 8.3 Six-month clock start — UNCHANGED

Whether the Art. 16 §3(b) clock runs from the re-export declaration date or from physical
departure is still unstated in the law. **The engine uses the earlier of the two**, which
can only shorten the window and therefore cannot cause a late filing.

### 8.4 ZATCA Resolution 28624 article numbers — NOT OBTAINABLE

**Status: unresolved. Every published route to the text failed.**

| Source | Result |
|---|---|
| `zatca.gov.sa/.../Rules_of_Customs_Procedures.pdf` | HTTP 404 |
| Umm Al-Qura issue page (`uqn.gov.sa/details?p=24297`) | HTTP 500 |
| Umm Al-Qura decisions page (`uqn.gov.sa/decisions-and-regulations/4001240`) | Announcement only; the operative text is an unlinked attachment |
| `istitlaa.ncc.gov.sa` consultation copy | Connection reset |
| Two Saudi legal aggregators | HTTP 503 / navigation shell only |

What **is** established from ZATCA's own announcement and Big-4 advisories, without
article numbers attached:

- Administrative Decision **28624** dated **23/05/1445 AH**, published in Umm Al-Qura,
  effective 30 days after publication (**29 December 2023**).
- Amended by Administrative Decision **1446-99-485** dated **05/04/1446 AH**, and a
  further decision **28918** appears in the same amendment series.
- A re-export *Bayan* is created and **linked to the import *Bayan*** in order to move
  goods to a free or duty-free zone, and the owner then submits a refund request for the
  customs duties.
- ZATCA may photograph incoming goods or require descriptive literature so they can be
  **matched on re-export** — the practical counterpart of the Art. 16 §4 identification
  requirement.

**Why this does not block week 5.** The operative constants all come from the GCC Common
Customs Law and its Rules of Implementation, which are transcribed from the GCC
Secretariat's own publication and are authoritative (§§1–2). Resolution 28624 is the
Saudi *procedural* implementation: it governs how a filing is made, not what makes a
claim eligible. Nothing in `services/rules/` or `services/matcher/` depends on an article
number from it.

**What it does block.** The citation strings on the KSA filing packet, which currently
name the GCC provisions rather than their Saudi procedural counterparts. Before the first
live KSA filing, obtain the Arabic text — by request to ZATCA directly, or from the Umm
Al-Qura print archive — and map the article numbers into `services/packager/`.

Until then, §4 of this document remains marked as needing confirmation, and no claim
citation asserts a Resolution 28624 article number it cannot support.

### 8.4.1 Mitigation — the `ANALYST_REVIEW` citation default (week 6)

The packager does not guess. Every ZATCA **procedural** citation in a generated output
packet defaults to an `ANALYST_REVIEW` placeholder rather than an article number, and the
placeholder is a first-class value in the payload, not an empty string.

| Rule | Behaviour |
|---|---|
| A citation whose authority is the GCC Common Customs Law or its Rules of Implementation | Emitted verbatim. These are transcribed from the GCC Secretariat's own publication (§§1–2) and are authoritative. |
| A citation whose authority is **ZATCA Resolution 28624** | Emitted as `CitationStatus.ANALYST_REVIEW` with `authority = "ZATCA Administrative Decision 28624 (23/05/1445 AH)"`, `article = null`, and the reason recorded. |
| Any packet containing one or more `ANALYST_REVIEW` citations | Carries `requires_analyst_review = true` at the top level and **must not** be transmitted to ZATCA e-Services until an analyst supplies the article number. |

The placeholder carries the reason string verbatim, so the person clearing it sees why it
is open rather than an unexplained blank:

> `28624 article number not obtainable from any published source; see COMPLIANCE-GCC.md §8.4`

**Why a placeholder and not an inferred number.** A wrong article number on a refund
request is worse than a missing one. A missing number invites a request for information; a
confidently wrong number is a misstatement to the authority, and the whole design
principle of this system is that the LLM never originates a figure or a citation. The same
rule now applies to the packager: it never originates an article number either.

**Closing it.** When the Arabic text is obtained, the article numbers are entered once in
`services/packager/src/citations.py` and every placeholder resolves. No claim logic
changes, because no claim logic ever depended on them.

---

## 9. Fasah partial-consignment probe — payload specification (week 6)

`scripts/fasah_sandbox_probe.py` constructs, and prints for manual execution, the exact
payloads that answer §8.2. It does **not** transmit: the Fasah sandbox requires credentials
issued to a registered broker, which are not available from this environment, and firing a
speculative declaration at a customs platform is not something to do on an assumption.

The probe emits four requests in sequence, each isolating one unknown:

| # | Probe | Answers |
|---|---|---|
| A | Re-export declaration for the **full** quantity of the linked import line | Baseline — establishes that the link mechanism works at all for this Bayan |
| B | Re-export declaration for a **partial** quantity (40% of the import line) | Whether a below-quantity link is accepted |
| C | Second re-export declaration for the **residual** 60% against the same Bayan | Whether residual quantity remains available after a partial draw |
| D | Third re-export declaration **exceeding** the residual | What the platform returns on over-draw — the error code is the answer |

Probe A is submitted against a distinct Bayan from B/C/D so the baseline is not consumed
by the partial series.

**Recording the result.** Each response is written to `scripts/fasah_probe_results.json`
in a fixed shape, and §8.2 is updated from that file rather than from recollection. Until
it exists, `PartialConsignmentBehaviour.UNDETERMINED` is what the engine assumes, and
partial-shipment claims continue to route to `ANALYST_REVIEW`.

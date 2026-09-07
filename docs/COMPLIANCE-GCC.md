# GCC / Saudi Arabia — Compliance Rule Reference

> Persistent context. Primary-source rules for the KSA/GCC lane of the compliance engine.
> Every constant in `services/rules/` traces to a citation in this file. Read with
> `ARCHITECTURE.md` before touching jurisdiction logic.

**Verification status.** Sections 1–2 are transcribed from the GCC Secretariat's published
Common Customs Law PDF and are authoritative. Sections 3–5 are drawn from ZATCA's own
pages plus Big-4 and law-firm advisories; they are directionally reliable but the article
numbers of ZATCA Resolution 28624 were not obtainable in English. Treat section 4 numbers
as needing confirmation against the Arabic Umm Al-Qura text before a live filing.

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

## 8. Remaining open questions

1. Exact article numbers in ZATCA Resolution 28624 (needs Arabic Umm Al-Qura text).
2. Whether ZATCA screens the threshold against the **customs value** of the re-exported
   goods or the original import customs value where they differ. Art. 16 §2 says "the
   value of the re-exported foreign goods", which reads as the former. **The engine uses
   the re-export declared value** and records which basis was used on the claim.
3. Whether partial-consignment re-exports are accepted through Fasah without a prior ruling.
4. Whether the six-month claim clock runs from the re-export declaration date or from
   physical departure. **The engine uses the earlier of the two.**

Each is a claim-voiding risk if guessed wrong, so each routes to `ANALYST_REVIEW` rather
than being resolved by the model.

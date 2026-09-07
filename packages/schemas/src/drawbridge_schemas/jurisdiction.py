"""Jurisdiction profiles.

Week 1 treated the US as the only jurisdiction and made its constants module-level. That
was valid then and is wrong now: the GCC differs from the US in refund rate, clock anchor,
deadline arithmetic, minimum claim value, and — decisively — in whether substitution
matching exists at all. See docs/ARCHITECTURE.md §3.5 and docs/COMPLIANCE-GCC.md.

Every constant here carries its citation. Nothing in this module is a guess; where the
primary source is silent, the field is None and the rules engine routes to ANALYST_REVIEW
rather than inventing a value.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Jurisdiction(StrEnum):
    """Customs authority a claim is filed with."""

    US = "us"
    """United States — CBP."""

    KSA = "ksa"
    """Kingdom of Saudi Arabia — ZATCA. GCC Common Customs Law applies."""


class Currency(StrEnum):
    USD = "USD"
    SAR = "SAR"


class MatchTheory(StrEnum):
    """How an import line may be paired with an export line."""

    DIRECT_IDENTITY = "direct_identity"
    """The same physical article, traced to its own import declaration."""

    HTS_SUBSTITUTION = "hts_substitution"
    """A different article sharing the first 8 HTS digits. US only (TFTEA)."""

    DECLARATION_LINKAGE = "declaration_linkage"
    """Re-export declaration carrying the import declaration number.
    GCC Rules of Implementation Art. 15(c). The only GCC theory."""


class ClockAnchor(StrEnum):
    """What event starts the re-export eligibility window.

    Not cosmetic. ZATCA permits duty payment to be postponed up to 30 days against a
    guarantee, so import date and duty-payment date genuinely differ, and the GCC window
    runs from payment.
    """

    IMPORT_DATE = "import_date"
    DUTY_PAYMENT_DATE = "duty_payment_date"


class DeadlineUnit(StrEnum):
    """Calendar months and fixed day counts are not interchangeable.

    "Six Gregorian months" is 181-184 days depending on which months are spanned. A
    180-day constant files late for most of the year.
    """

    DAYS = "days"
    GREGORIAN_MONTHS = "gregorian_months"
    GREGORIAN_YEARS = "gregorian_years"


class Deadline(BaseModel):
    """A statutory period, in the unit the statute actually uses."""

    model_config = ConfigDict(frozen=True)

    amount: int = Field(gt=0)
    unit: DeadlineUnit
    citation: str


class JurisdictionProfile(BaseModel):
    """The complete rule set for one customs authority.

    Selected at claim creation and threaded through matching, quantification and
    packaging. Nothing downstream reads a jurisdiction constant from anywhere else.
    """

    model_config = ConfigDict(frozen=True)

    jurisdiction: Jurisdiction
    currency: Currency

    refund_rate: Annotated[Decimal, Field(gt=0, le=1)]
    """Fraction of recoverable base actually refunded."""

    refund_rate_citation: str

    permitted_theories: frozenset[MatchTheory]
    """Which matching theories the statute allows. Enforced, not advisory."""

    clock_anchor: ClockAnchor
    reexport_window: Deadline
    """Import/payment -> re-export must fall inside this."""

    claim_filing_deadline: Deadline
    """Re-export -> claim filing must fall inside this."""

    absolute_bar: Deadline | None
    """Hard prescription: no claim accepted beyond this from duty payment, regardless."""

    record_retention: Deadline

    min_claim_value: Decimal | None
    """Minimum value of re-exported goods for eligibility. None where no threshold."""

    min_claim_value_currency: Currency | None

    includes_fees_in_base: bool
    """Whether merchandise/harbour-type fees join duty in the recoverable base."""

    includes_consumption_tax_in_base: bool
    """VAT/excise. Always False in GCC — recovered via the VAT return, not drawback."""

    def permits(self, theory: MatchTheory) -> bool:
        return theory in self.permitted_theories


# --------------------------------------------------------------------------------------
# United States — 19 U.S.C. §1313
# --------------------------------------------------------------------------------------

US_PROFILE = JurisdictionProfile(
    jurisdiction=Jurisdiction.US,
    currency=Currency.USD,
    refund_rate=Decimal("0.99"),
    refund_rate_citation="19 U.S.C. §1313 — 99% of duties, taxes and fees",
    permitted_theories=frozenset({MatchTheory.DIRECT_IDENTITY, MatchTheory.HTS_SUBSTITUTION}),
    clock_anchor=ClockAnchor.IMPORT_DATE,
    reexport_window=Deadline(
        amount=5, unit=DeadlineUnit.GREGORIAN_YEARS, citation="19 U.S.C. §1313(j)"
    ),
    claim_filing_deadline=Deadline(
        amount=3, unit=DeadlineUnit.GREGORIAN_YEARS, citation="19 U.S.C. §1313(r)"
    ),
    absolute_bar=None,
    record_retention=Deadline(amount=5, unit=DeadlineUnit.GREGORIAN_YEARS, citation="19 CFR §163"),
    min_claim_value=None,
    min_claim_value_currency=None,
    includes_fees_in_base=True,
    includes_consumption_tax_in_base=False,
)


# --------------------------------------------------------------------------------------
# Saudi Arabia / GCC — Common Customs Law Art. 97, Rules of Implementation Art. 16
#
# Transcribed from the GCC Secretariat's published Common Customs Law. Three values here
# correct approximations that were carried into the project brief; see
# docs/COMPLIANCE-GCC.md §2.1.
# --------------------------------------------------------------------------------------

GCC_MIN_REEXPORT_VALUE_USD = Decimal("5000.00")
"""Rules of Implementation Art. 16 §2 — "shall not be less than five thousand US dollars
(or its equivalent in the local currency)". No US analogue; a hard eligibility gate."""

KSA_PROFILE = JurisdictionProfile(
    jurisdiction=Jurisdiction.KSA,
    currency=Currency.SAR,
    # Art. 16 §6 limits the refund to duties "actually paid" and prescribes no haircut.
    refund_rate=Decimal("1.00"),
    refund_rate_citation="GCC Rules of Implementation Art. 16 §6 — duties actually paid",
    # No substitution exists in GCC law. Art. 16 §4-5 require a single identified
    # consignment in unaltered condition; Art. 15(c) requires the import declaration
    # number on the re-export declaration.
    permitted_theories=frozenset({MatchTheory.DIRECT_IDENTITY, MatchTheory.DECLARATION_LINKAGE}),
    clock_anchor=ClockAnchor.DUTY_PAYMENT_DATE,
    reexport_window=Deadline(
        amount=1,
        unit=DeadlineUnit.GREGORIAN_YEARS,
        citation="GCC Rules of Implementation Art. 16 §3(a) — one Gregorian year from "
        "the date of payment of the customs duties",
    ),
    claim_filing_deadline=Deadline(
        amount=6,
        unit=DeadlineUnit.GREGORIAN_MONTHS,
        citation="GCC Rules of Implementation Art. 16 §3(b) — six Gregorian months from "
        "the date of re-exportation",
    ),
    absolute_bar=Deadline(
        amount=3,
        unit=DeadlineUnit.GREGORIAN_YEARS,
        citation="GCC Common Customs Law Art. 174 — no claim accepted for duties paid "
        "more than three years ago",
    ),
    record_retention=Deadline(
        amount=5,
        unit=DeadlineUnit.GREGORIAN_YEARS,
        citation="GCC Common Customs Law Art. 175; ZATCA Res. 28624 five-year originals",
    ),
    min_claim_value=GCC_MIN_REEXPORT_VALUE_USD,
    min_claim_value_currency=Currency.USD,
    includes_fees_in_base=False,
    # KSA import VAT (15%) is recovered through the VAT return as input tax, and excise
    # has its own procedure. Including either in a drawback claim is a filing error.
    includes_consumption_tax_in_base=False,
)


PROFILES: dict[Jurisdiction, JurisdictionProfile] = {
    Jurisdiction.US: US_PROFILE,
    Jurisdiction.KSA: KSA_PROFILE,
}


def profile_for(jurisdiction: Jurisdiction) -> JurisdictionProfile:
    return PROFILES[jurisdiction]

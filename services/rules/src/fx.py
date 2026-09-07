"""Currency conversion for statutory thresholds.

The reference date is settled by primary source. GCC Common Customs Law, Rules of
Implementation of Valuation, **Article 1(I)(6)**:

    "The time of payment of the customs taxes 'duties' shall be the time approved for
     currency exchange rate."

So a rate resolves **as at the duty-payment date** — the same anchor Art. 16 §3(a) uses
for the re-export window. Not the invoice date, not the declaration date.

**Why there is no live SAMA call.** SAMA holds SAR at a policy peg rather than quoting a
market rate, publishes only monthly tables with no machine-readable feed, and every
"SAMA API" is a third-party mirror trailing the calendar by weeks. More decisively, a
network call at match time makes a filed figure non-reproducible: re-running a 2024 claim
in 2029 would hit a different endpoint or none at all, which breaks the recordkeeping
posture the system exists to satisfy. See docs/COMPLIANCE-GCC.md §7.1.

What replaces it is a date-aware provider interface with the peg as one cited entry, and
a table provider for dated rates where a real conversion is needed. Where no rate is on
file the conversion **fails loudly** rather than guessing.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from drawbridge_schemas.jurisdiction import Currency


class RateUnavailableError(LookupError):
    """No rate on file for this pair as at this date.

    Deliberately fatal at the conversion boundary. A threshold decided on an invented
    rate is worse than a claim routed to review: the first is silently wrong, the second
    is visibly unfinished.
    """

    def __init__(self, base: Currency, quote: Currency, as_of: date) -> None:
        self.base = base
        self.quote = quote
        self.as_of = as_of
        super().__init__(
            f"no {base}/{quote} rate on file as at {as_of} "
            "(GCC Rules of Implementation of Valuation Art. 1(I)(6) — rate is fixed at "
            "the duty-payment date)"
        )


@dataclass(frozen=True, slots=True)
class Rate:
    """One quoted rate, with the authority behind it.

    Held as a ratio rather than a single decimal factor, because pre-computing a
    reciprocal loses exactness. `1 / 3.75` at Decimal's default precision is
    0.2666...667, and 18,750 SAR through it converts to
    5000.000000000000000000000001 rather than 5000 — which decides a claim sitting
    exactly on the Art. 16 §2 threshold the wrong way. Multiplying then dividing keeps
    the division exact wherever it terminates.

    `citation` is not decoration. A converted threshold figure appears in a filing, so
    the derivation trail has to name where the rate came from.
    """

    base: Currency
    quote: Currency
    numerator: Decimal
    denominator: Decimal
    effective_from: date
    citation: str
    is_peg: bool = False

    @property
    def units_of_quote_per_base(self) -> Decimal:
        """The rate as a single figure, for display and comparison."""
        return self.numerator / self.denominator

    def convert(self, amount: Decimal) -> Decimal:
        return amount * self.numerator / self.denominator

    def inverse(self, citation_suffix: str = " (inverted)") -> Rate:
        return Rate(
            base=self.quote,
            quote=self.base,
            numerator=self.denominator,
            denominator=self.numerator,
            effective_from=self.effective_from,
            citation=self.citation + citation_suffix,
            is_peg=self.is_peg,
        )


def quoted_rate(
    base: Currency,
    quote: Currency,
    factor: Decimal,
    effective_from: date,
    citation: str,
    *,
    is_peg: bool = False,
) -> Rate:
    """A rate quoted as a single multiplier."""
    return Rate(base, quote, factor, Decimal("1"), effective_from, citation, is_peg)


class RateProvider(Protocol):
    """Resolves a rate as at a date."""

    def rate(self, base: Currency, quote: Currency, as_of: date) -> Rate:
        """Rate to convert `base` into `quote`, effective on `as_of`.

        Raises RateUnavailableError when nothing is on file.
        """
        ...


# --------------------------------------------------------------------------------------
# The SAR peg
# --------------------------------------------------------------------------------------

# SAMA has held the riyal at 3.75 to the dollar since 1986. This is monetary policy, not
# a market quote: there is no daily series to fetch, and the "rate on the payment date"
# for any date in the system's range is this number.
SAR_PEG_ESTABLISHED = date(1986, 6, 1)
SAR_PER_USD_PEG = Decimal("3.75")

_PEG_CITATION = (
    "Saudi Central Bank (SAMA) fixed peg, 1 USD = 3.75 SAR, in force since June 1986; "
    "rate date per GCC Rules of Implementation of Valuation Art. 1(I)(6)"
)


class PeggedRateProvider:
    """Serves pegged pairs and nothing else.

    Refuses dates before the peg was established rather than extrapolating backwards —
    the three-year absolute bar makes such a claim unfileable anyway, so a wrong answer
    here would only ever mislead.
    """

    def rate(self, base: Currency, quote: Currency, as_of: date) -> Rate:
        if base is quote:
            return quoted_rate(base, quote, Decimal("1"), as_of, "identity", is_peg=True)

        if {base, quote} == {Currency.USD, Currency.SAR}:
            if as_of < SAR_PEG_ESTABLISHED:
                raise RateUnavailableError(base, quote, as_of)
            usd_to_sar = quoted_rate(
                Currency.USD,
                Currency.SAR,
                SAR_PER_USD_PEG,
                SAR_PEG_ESTABLISHED,
                _PEG_CITATION,
                is_peg=True,
            )
            # Inverting the ratio rather than the decimal keeps SAR -> USD exact.
            return usd_to_sar if base is Currency.USD else usd_to_sar.inverse("")

        raise RateUnavailableError(base, quote, as_of)


# --------------------------------------------------------------------------------------
# Dated table
# --------------------------------------------------------------------------------------


@dataclass
class TableRateProvider:
    """Dated rates loaded from stored records.

    For third-currency invoices — a Bayan valued in CNY or EUR — where a real conversion
    is needed and the peg says nothing. Rates are stored with an effective date and
    resolved by taking the latest rate at or before the payment date, which is how a
    published daily series behaves.

    Deliberately not backed by an HTTP client: whatever populates this table is an ingest
    concern with its own provenance, and the matcher must stay a pure function of its
    input.
    """

    rates: dict[tuple[Currency, Currency], list[Rate]] = field(default_factory=dict)

    def add(self, rate: Rate) -> None:
        key = (rate.base, rate.quote)
        series = self.rates.setdefault(key, [])
        series.append(rate)
        series.sort(key=lambda r: r.effective_from)

    def rate(self, base: Currency, quote: Currency, as_of: date) -> Rate:
        if base is quote:
            return quoted_rate(base, quote, Decimal("1"), as_of, "identity")

        series = self.rates.get((base, quote))
        if series:
            return _latest_at_or_before(series, as_of, base, quote)

        # A stored CNY/USD series also answers USD/CNY.
        inverse = self.rates.get((quote, base))
        if inverse:
            return _latest_at_or_before(inverse, as_of, quote, base).inverse()

        raise RateUnavailableError(base, quote, as_of)


def _latest_at_or_before(series: list[Rate], as_of: date, base: Currency, quote: Currency) -> Rate:
    dates = [r.effective_from for r in series]
    index = bisect.bisect_right(dates, as_of) - 1
    if index < 0:
        raise RateUnavailableError(base, quote, as_of)
    return series[index]


@dataclass
class ChainedRateProvider:
    """Tries each provider in order, first hit wins.

    The peg answers SAR/USD without a table; the table answers everything else. Ordering
    matters: a mirrored SAMA figure that drifted off 3.75 must never override the peg.
    """

    providers: tuple[RateProvider, ...]

    def rate(self, base: Currency, quote: Currency, as_of: date) -> Rate:
        for provider in self.providers:
            try:
                return provider.rate(base, quote, as_of)
            except RateUnavailableError:
                continue
        raise RateUnavailableError(base, quote, as_of)


def default_provider() -> RateProvider:
    """Peg first, then any dated table supplied by ingest."""
    return ChainedRateProvider((PeggedRateProvider(), TableRateProvider()))


# --------------------------------------------------------------------------------------
# Threshold evaluation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThresholdCheck:
    """Result of screening a value against a statutory minimum.

    Carries the converted figure and the rate that produced it so the decision is
    reconstructable from the record alone.
    """

    passed: bool
    declared_amount: Decimal
    declared_currency: Currency
    converted_amount: Decimal
    threshold: Decimal
    threshold_currency: Currency
    rate: Rate
    rate_date: date

    @property
    def shortfall(self) -> Decimal:
        """How far under the minimum, in threshold currency. Zero when it passed."""
        return max(self.threshold - self.converted_amount, Decimal("0"))

    @property
    def shortfall_ratio(self) -> Decimal:
        if self.threshold == 0:
            return Decimal("0")
        return self.shortfall / self.threshold

    @property
    def is_near_miss(self) -> bool:
        """Within 10% of clearing.

        Not a pass — Art. 16 §2 says "shall not be less than", and a threshold with a
        soft edge is not the threshold the article states. This flags the rejection for
        analyst attention so a near miss is visible rather than quietly discarded.
        """
        return not self.passed and self.shortfall_ratio <= Decimal("0.10")

    def explain(self) -> str:
        return (
            f"{self.declared_amount} {self.declared_currency} = "
            f"{self.converted_amount.quantize(Decimal('0.01'))} "
            f"{self.threshold_currency} at {self.rate.units_of_quote_per_base} "
            f"({self.rate_date}); minimum is {self.threshold} {self.threshold_currency}"
        )


def check_minimum(
    *,
    declared_amount: Decimal,
    declared_currency: Currency,
    threshold: Decimal,
    threshold_currency: Currency,
    rate_date: date,
    provider: RateProvider,
) -> ThresholdCheck:
    """Screen a declared value against a statutory minimum.

    `rate_date` must be the duty-payment date (Art. 1(I)(6)). Passing any other date here
    produces a defensible-looking figure computed on the wrong basis.
    """
    rate = provider.rate(declared_currency, threshold_currency, rate_date)
    converted = rate.convert(declared_amount)
    return ThresholdCheck(
        passed=converted >= threshold,
        declared_amount=declared_amount,
        declared_currency=declared_currency,
        converted_amount=converted,
        threshold=threshold,
        threshold_currency=threshold_currency,
        rate=rate,
        rate_date=rate_date,
    )

"""Currency conversion at the statutory rate date, and human-in-the-loop triage."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from drawbridge_schemas.jurisdiction import KSA_PROFILE, Currency, Jurisdiction
from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Language,
    Provenance,
    Span,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode
from services.matcher.src.base import (
    MatchRequest,
    MatchResult,
    Rejection,
    RejectionCode,
    SolverStatus,
)
from services.matcher.src.gcc_linkage import GccLinkageMatcher
from services.rules.src.fx import (
    SAR_PEG_ESTABLISHED,
    ChainedRateProvider,
    PeggedRateProvider,
    RateUnavailableError,
    TableRateProvider,
    check_minimum,
    default_provider,
    quoted_rate,
)
from services.rules.src.triage import ReviewReason, Severity, triage

TENANT = UUID("00000000-0000-0000-0000-0000000000a1")
DECLARATION = "20240115447821"
HS = "84713000"


class TestPeggedProvider:
    provider = PeggedRateProvider()

    def test_usd_to_sar_is_the_peg(self) -> None:
        rate = self.provider.rate(Currency.USD, Currency.SAR, date(2024, 2, 8))
        assert rate.units_of_quote_per_base == Decimal("3.75")
        assert rate.is_peg
        assert "SAMA" in rate.citation

    def test_sar_to_usd_inverts_the_peg(self) -> None:
        rate = self.provider.rate(Currency.SAR, Currency.USD, date(2024, 2, 8))
        assert rate.convert(Decimal("375")) == Decimal("100")

    def test_identity_conversion(self) -> None:
        rate = self.provider.rate(Currency.USD, Currency.USD, date(2024, 2, 8))
        assert rate.convert(Decimal("42")) == Decimal("42")

    def test_refuses_dates_before_the_peg(self) -> None:
        """Extrapolating backwards would only ever mislead — Art. 174 bars such a claim
        anyway."""
        with pytest.raises(RateUnavailableError):
            self.provider.rate(Currency.USD, Currency.SAR, SAR_PEG_ESTABLISHED - timedelta(days=1))

    def test_carries_a_citation_for_the_derivation_trail(self) -> None:
        rate = self.provider.rate(Currency.SAR, Currency.USD, date(2024, 2, 8))
        assert "Art. 1(I)(6)" in rate.citation


class TestTableProvider:
    def _table(self) -> TableRateProvider:
        table = TableRateProvider()
        for day, value in (
            (date(2024, 1, 1), "0.1380"),
            (date(2024, 2, 1), "0.1390"),
            (date(2024, 3, 1), "0.1400"),
        ):
            table.add(quoted_rate(Currency.CNY, Currency.USD, Decimal(value), day, "test series"))
        return table

    def test_resolves_the_latest_rate_at_or_before_the_date(self) -> None:
        """A published daily series behaves this way: the rate in force on the date."""
        table = self._table()
        for day, expected in (
            (date(2024, 2, 15), "0.1390"),
            (date(2024, 2, 1), "0.1390"),
            (date(2024, 1, 31), "0.1380"),
        ):
            rate = table.rate(Currency.CNY, Currency.USD, day)
            assert rate.units_of_quote_per_base == Decimal(expected)

    def test_refuses_dates_before_the_series_starts(self) -> None:
        with pytest.raises(RateUnavailableError):
            self._table().rate(Currency.CNY, Currency.USD, date(2023, 12, 31))

    def test_inverts_a_stored_series(self) -> None:
        table = self._table()
        rate = table.rate(Currency.USD, Currency.CNY, date(2024, 2, 15))
        assert rate.units_of_quote_per_base == Decimal("1") / Decimal("0.1390")
        assert "inverted" in rate.citation

    def test_unknown_pair_raises(self) -> None:
        with pytest.raises(RateUnavailableError, match=re.escape("Art. 1(I)(6)")):
            TableRateProvider().rate(Currency.CNY, Currency.SAR, date(2024, 2, 8))


class TestChainedProvider:
    def test_peg_wins_over_a_drifted_table_entry(self) -> None:
        """A mirrored figure that drifted off 3.75 must never override the peg."""
        table = TableRateProvider()
        table.add(
            quoted_rate(Currency.SAR, Currency.USD, Decimal("0.9"), date(2020, 1, 1), "bad mirror")
        )
        chained = ChainedRateProvider((PeggedRateProvider(), table))

        rate = chained.rate(Currency.SAR, Currency.USD, date(2024, 2, 8))
        assert rate.is_peg
        assert rate.convert(Decimal("375")) == Decimal("100")

    def test_falls_through_to_the_table_for_other_pairs(self) -> None:
        table = TableRateProvider()
        table.add(quoted_rate(Currency.CNY, Currency.USD, Decimal("0.14"), date(2024, 1, 1), "s"))
        chained = ChainedRateProvider((PeggedRateProvider(), table))
        assert chained.rate(Currency.CNY, Currency.USD, date(2024, 2, 8)).convert(
            Decimal("100")
        ) == Decimal("14.00")

    def test_default_provider_answers_the_peg(self) -> None:
        assert default_provider().rate(Currency.SAR, Currency.USD, date(2024, 2, 8)).is_peg


class TestThresholdCheck:
    def test_passing_value(self) -> None:
        check = check_minimum(
            declared_amount=Decimal("390625.00"),
            declared_currency=Currency.SAR,
            threshold=Decimal("5000.00"),
            threshold_currency=Currency.USD,
            rate_date=date(2024, 2, 8),
            provider=default_provider(),
        )
        assert check.passed
        # 390,625 / 3.75 = 104,166.67
        assert check.converted_amount.quantize(Decimal("0.01")) == Decimal("104166.67")
        assert check.shortfall == Decimal("0")

    def test_exactly_at_the_threshold_passes(self) -> None:
        """ "Shall not be less than" — equal is not less."""
        check = check_minimum(
            declared_amount=Decimal("18750.00"),  # exactly 5,000 USD
            declared_currency=Currency.SAR,
            threshold=Decimal("5000.00"),
            threshold_currency=Currency.USD,
            rate_date=date(2024, 2, 8),
            provider=default_provider(),
        )
        assert check.passed
        assert check.converted_amount == Decimal("5000.00")

    def test_one_riyal_short_fails(self) -> None:
        check = check_minimum(
            declared_amount=Decimal("18746.00"),
            declared_currency=Currency.SAR,
            threshold=Decimal("5000.00"),
            threshold_currency=Currency.USD,
            rate_date=date(2024, 2, 8),
            provider=default_provider(),
        )
        assert not check.passed
        assert check.is_near_miss

    def test_far_below_is_not_a_near_miss(self) -> None:
        check = check_minimum(
            declared_amount=Decimal("3000.00"),
            declared_currency=Currency.SAR,
            threshold=Decimal("5000.00"),
            threshold_currency=Currency.USD,
            rate_date=date(2024, 2, 8),
            provider=default_provider(),
        )
        assert not check.passed
        assert not check.is_near_miss

    def test_explain_names_the_rate_and_date(self) -> None:
        check = check_minimum(
            declared_amount=Decimal("3000.00"),
            declared_currency=Currency.SAR,
            threshold=Decimal("5000.00"),
            threshold_currency=Currency.USD,
            rate_date=date(2024, 2, 8),
            provider=default_provider(),
        )
        text = check.explain()
        assert "2024-02-08" in text
        assert "SAR" in text and "USD" in text


# --------------------------------------------------------------------------------------
# The gate, end to end through the matcher
# --------------------------------------------------------------------------------------


def _provenance() -> Provenance:
    ref = DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.ZATCA_BAYAN,
        sha256="d" * 64,
        object_key="tenants/a1/zatca_bayan/v.pdf",
        language=Language.MIXED,
    )
    return Provenance(
        spans=(Span(document=ref, page=1, bbox=(0.0, 0.0, 10.0, 10.0)),),
        confidence=Confidence(score=0.97, method="pymupdf-native"),
    )


def _entry(payment: date) -> EntryLine:
    return EntryLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.KSA,
        currency=Currency.SAR,
        declaration_number=DECLARATION,
        line_number=1,
        import_date=payment - timedelta(days=24),
        declaration_date=payment - timedelta(days=24),
        duty_payment_date=payment,
        port_of_entry="Jeddah Islamic Port",
        country_of_origin="CN",
        hts=HTSCode(code=HS),
        description="machines",
        quantity=Decimal("1200"),
        unit_of_measure="PCE",
        entered_value=Decimal("937500.00"),
        duty_paid=Decimal("46875.00"),
        provenance=_provenance(),
    )


def _export(export_date: date, value: Decimal) -> ExportLine:
    return ExportLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.KSA,
        reference="RE-VAL-1",
        line_number=1,
        export_date=export_date,
        destination_country="AE",
        hts=HTSCode(code=HS),
        description="re-exported",
        quantity=Decimal("500"),
        unit_of_measure="PCE",
        declared_value=value,
        linked_import_declaration=DECLARATION,
        consignment_id="CNS-A",
        provenance=_provenance(),
    )


def _run(value: Decimal, provider=None):
    payment = date(2024, 2, 8)
    export_date = payment + timedelta(days=90)
    return GccLinkageMatcher(provider).match(
        MatchRequest(
            imports=[_entry(payment)],
            exports=[_export(export_date, value)],
            profile=KSA_PROFILE,
            as_of=export_date + timedelta(days=30),
        )
    )


class TestGccValuationGate:
    def test_value_above_threshold_links(self) -> None:
        assert len(_run(Decimal("390625.00")).matches) == 1

    def test_value_exactly_at_threshold_links(self) -> None:
        # 18,750 SAR / 3.75 = exactly 5,000 USD.
        assert len(_run(Decimal("18750.00")).matches) == 1

    def test_near_miss_is_now_rejected_strictly(self) -> None:
        """Week 3 accepted this. Art. 16 §2 says "shall not be less than"."""
        result = _run(Decimal("18000.00"))  # 4,800 USD
        assert result.matches == ()
        rejection = result.rejections[0]
        assert rejection.code is RejectionCode.BELOW_MINIMUM_VALUE
        assert "near_miss" in rejection.detail

    def test_near_miss_rejection_shows_the_arithmetic(self) -> None:
        rejection = _run(Decimal("18000.00")).rejections[0]
        assert "4800.00 USD" in rejection.detail
        assert "2024-02-08" in rejection.detail
        assert "short by 200.00" in rejection.detail

    def test_far_below_carries_no_near_miss_marker(self) -> None:
        rejection = _run(Decimal("3000.00")).rejections[0]
        assert "near_miss" not in rejection.detail

    def test_missing_rate_routes_to_review_not_a_guess(self) -> None:
        """An empty provider cannot convert, so the threshold is unevaluable."""
        result = _run(Decimal("390625.00"), provider=TableRateProvider())
        assert result.matches == ()
        assert result.rejections[0].code is RejectionCode.RATE_UNAVAILABLE
        assert "Art. 1(I)(6)" in result.rejections[0].citation


class TestTriage:
    def _result(self, status: SolverStatus, rejections=()) -> MatchResult:
        return MatchResult(
            jurisdiction=Jurisdiction.KSA,
            status=status,
            rejections=tuple(rejections),
            candidate_pairs=3,
            detail="test",
        )

    def test_optimal_and_confident_proceeds(self) -> None:
        verdict = triage(
            self._result(SolverStatus.OPTIMAL),
            confidences=[Confidence(score=0.99, method="native")],
        )
        assert not verdict.requires_review

    def test_feasible_is_not_optimal(self) -> None:
        """A feasible allocation may under-claim, and nothing downstream could tell."""
        verdict = triage(self._result(SolverStatus.FEASIBLE))
        assert verdict.requires_review
        assert verdict.items[0].reason is ReviewReason.SOLVER_NOT_OPTIMAL
        assert verdict.items[0].severity is Severity.HIGH

    def test_infeasible_blocks(self) -> None:
        verdict = triage(self._result(SolverStatus.INFEASIBLE))
        assert verdict.is_blocking

    def test_low_confidence_queues(self) -> None:
        verdict = triage(
            self._result(SolverStatus.OPTIMAL),
            confidences=[
                Confidence(score=0.99, method="native"),
                Confidence(score=0.82, method="paddleocr"),
            ],
        )
        assert verdict.requires_review
        assert verdict.items[0].reason is ReviewReason.LOW_EXTRACTION_CONFIDENCE
        assert verdict.items[0].payload["lowest_method"] == "paddleocr"

    def test_needs_review_flag_queues_even_at_high_score(self) -> None:
        verdict = triage(
            self._result(SolverStatus.OPTIMAL),
            confidences=[Confidence(score=0.99, method="native", needs_review=True)],
        )
        assert verdict.requires_review

    def test_threshold_near_miss_queues(self) -> None:
        rejection = Rejection(
            export_line_id="x",
            code=RejectionCode.BELOW_MINIMUM_VALUE,
            citation="Art. 16 §2",
            detail="short by 200.00 USD [near_miss]",
        )
        verdict = triage(self._result(SolverStatus.OPTIMAL, [rejection]))
        assert verdict.items[0].reason is ReviewReason.THRESHOLD_NEAR_MISS
        assert verdict.items[0].severity is Severity.NORMAL

    def test_plain_below_threshold_does_not_queue(self) -> None:
        rejection = Rejection(
            export_line_id="x",
            code=RejectionCode.BELOW_MINIMUM_VALUE,
            citation="Art. 16 §2",
            detail="short by 4000.00 USD",
        )
        verdict = triage(self._result(SolverStatus.OPTIMAL, [rejection]))
        assert not verdict.requires_review

    def test_missing_rate_blocks(self) -> None:
        rejection = Rejection(
            export_line_id="x",
            code=RejectionCode.RATE_UNAVAILABLE,
            citation="Art. 1(I)(6)",
            detail="no rate",
        )
        verdict = triage(self._result(SolverStatus.OPTIMAL, [rejection]))
        assert verdict.is_blocking
        assert verdict.items[0].reason is ReviewReason.RATE_UNAVAILABLE

    def test_imminent_deadline_queues(self) -> None:
        verdict = triage(
            self._result(SolverStatus.OPTIMAL),
            filing_deadline=date(2025, 3, 12),
            as_of=date(2025, 3, 1),
        )
        assert verdict.items[0].reason is ReviewReason.DEADLINE_IMMINENT
        assert verdict.items[0].severity is Severity.HIGH

    def test_expired_deadline_blocks(self) -> None:
        verdict = triage(
            self._result(SolverStatus.OPTIMAL),
            filing_deadline=date(2025, 3, 12),
            as_of=date(2025, 3, 20),
        )
        assert verdict.is_blocking

    def test_distant_deadline_is_silent(self) -> None:
        verdict = triage(
            self._result(SolverStatus.OPTIMAL),
            filing_deadline=date(2026, 1, 1),
            as_of=date(2025, 3, 1),
        )
        assert not verdict.requires_review

    def test_several_reasons_produce_several_items(self) -> None:
        """One row per reason: resolving a near miss does not resolve a bad extraction."""
        rejection = Rejection(
            export_line_id="x",
            code=RejectionCode.BELOW_MINIMUM_VALUE,
            citation="Art. 16 §2",
            detail="[near_miss]",
        )
        verdict = triage(
            self._result(SolverStatus.FEASIBLE, [rejection]),
            confidences=[Confidence(score=0.5, method="ocr")],
            filing_deadline=date(2025, 3, 12),
            as_of=date(2025, 3, 1),
        )
        reasons = {i.reason for i in verdict.items}
        assert reasons == {
            ReviewReason.SOLVER_NOT_OPTIMAL,
            ReviewReason.LOW_EXTRACTION_CONFIDENCE,
            ReviewReason.THRESHOLD_NEAR_MISS,
            ReviewReason.DEADLINE_IMMINENT,
        }

"""Property-based proof that the GCC linker enforces its statutory deadlines.

The headline case: a claim filed on day 185 after re-export must be rejected. Six
Gregorian months is 181-184 days depending on which months are spanned, so day 185 is
past the deadline for *every* re-export date in the calendar — which is exactly what
makes it a good universal property, and exactly what a 180-day constant would get wrong
in the other direction.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from drawbridge_schemas.jurisdiction import KSA_PROFILE, Currency, Jurisdiction, MatchTheory
from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Language,
    Provenance,
    Span,
)
from drawbridge_schemas.trade import EntryLine, ExportLine, HTSCode
from services.matcher.src.base import MatchRequest, RejectionCode, SolverStatus
from services.matcher.src.gcc_linkage import GccLinkageMatcher
from services.rules.src.deadlines import add_gregorian_months, window_for

TENANT = UUID("00000000-0000-0000-0000-0000000000a1")
DECLARATION = "20240115447821"
HS_CODE = "84713000"

# Comfortably above the Art. 16 §2 USD 5,000 minimum: 390,625 SAR / 3.75 = 104,166 USD.
ABOVE_THRESHOLD = Decimal("390625.00")


def _provenance() -> Provenance:
    ref = DocumentRef(
        document_id=uuid4(),
        kind=DocumentKind.ZATCA_BAYAN,
        sha256="b" * 64,
        object_key="tenants/a1/zatca_bayan/p.pdf",
        language=Language.MIXED,
    )
    return Provenance(
        spans=(Span(document=ref, page=1, bbox=(0.0, 0.0, 10.0, 10.0)),),
        confidence=Confidence(score=0.96, method="pymupdf-native"),
    )


def _entry(
    payment_date: date, quantity: int = 1200, duty: Decimal = Decimal("46875.00")
) -> EntryLine:
    return EntryLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.KSA,
        currency=Currency.SAR,
        declaration_number=DECLARATION,
        line_number=1,
        import_date=payment_date - timedelta(days=24),
        declaration_date=payment_date - timedelta(days=24),
        duty_payment_date=payment_date,
        port_of_entry="Jeddah Islamic Port",
        country_of_origin="CN",
        hts=HTSCode(code=HS_CODE),
        description="Portable data processing machines",
        quantity=Decimal(quantity),
        unit_of_measure="PCE",
        entered_value=Decimal("937500.00"),
        duty_paid=duty,
        vat_paid=Decimal("147656.25"),
        provenance=_provenance(),
    )


def _export(
    export_date: date,
    *,
    quantity: int = 500,
    value: Decimal = ABOVE_THRESHOLD,
    declaration: str | None = DECLARATION,
    unused: bool = True,
    consignment: str | None = "CNS-2024-0115-A",
) -> ExportLine:
    return ExportLine(
        line_id=uuid4(),
        tenant_id=TENANT,
        jurisdiction=Jurisdiction.KSA,
        reference=f"RE-{uuid4().hex[:8].upper()}",
        line_number=1,
        export_date=export_date,
        destination_country="AE",
        hts=HTSCode(code=HS_CODE),
        description="Re-exported unused",
        quantity=Decimal(quantity),
        unit_of_measure="PCE",
        declared_value=value,
        linked_import_declaration=declaration,
        consignment_id=consignment,
        unused_and_unaltered=unused,
        provenance=_provenance(),
    )


def _run(imports, exports, as_of):
    return GccLinkageMatcher().match(
        MatchRequest(imports=imports, exports=exports, profile=KSA_PROFILE, as_of=as_of)
    )


# Payment dates across four years, so every month-length combination is exercised.
payment_dates = st.dates(min_value=date(2022, 1, 1), max_value=date(2025, 12, 31))
# Re-export well inside the one-Gregorian-year window.
window_offsets = st.integers(min_value=1, max_value=300)


class TestSixGregorianMonthDeadline:
    """Art. 16 §3(b) — six Gregorian months from the date of re-exportation."""

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(payment_dates, window_offsets)
    def test_day_185_is_always_rejected(self, payment: date, offset: int) -> None:
        """Day 185 is past six Gregorian months for every re-export date.

        The longest six-month span in the Gregorian calendar is 184 days (a run
        containing four 31-day months plus a leap February). Day 185 therefore cannot be
        inside the window regardless of when the goods left.
        """
        export_date = payment + timedelta(days=offset)
        result = _run([_entry(payment)], [_export(export_date)], export_date + timedelta(days=185))

        assert result.matches == ()
        assert result.status is SolverStatus.NO_CANDIDATES
        codes = {r.code for r in result.rejections}
        assert codes & {
            RejectionCode.FILING_DEADLINE_PASSED,
            RejectionCode.ABSOLUTE_BAR_PASSED,
        }, codes

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(payment_dates, window_offsets)
    def test_filing_on_the_deadline_itself_is_accepted(self, payment: date, offset: int) -> None:
        """The deadline is inclusive — filing on the last permitted day still counts."""
        export_date = payment + timedelta(days=offset)
        deadline = add_gregorian_months(export_date, 6)
        window = window_for(KSA_PROFILE, payment, export_date)

        # Skip cases where Art. 174's three-year bar bites first; those are covered
        # separately and are a different rejection.
        if window.absolute_bar is not None and deadline > window.absolute_bar:
            return

        result = _run([_entry(payment)], [_export(export_date)], deadline)
        assert len(result.matches) == 1, [str(r) for r in result.rejections]

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(payment_dates, window_offsets)
    def test_one_day_past_the_deadline_is_rejected(self, payment: date, offset: int) -> None:
        export_date = payment + timedelta(days=offset)
        deadline = add_gregorian_months(export_date, 6)

        result = _run([_entry(payment)], [_export(export_date)], deadline + timedelta(days=1))
        assert result.matches == ()

    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(payment_dates, window_offsets)
    def test_a_180_day_constant_would_disagree_with_the_statute(
        self, payment: date, offset: int
    ) -> None:
        """Why the Gregorian-month arithmetic exists at all.

        Six Gregorian months is 181-184 days. A 180-day constant expires early on every
        re-export date, so a claim filed between day 181 and the true deadline is valid
        under the statute and would have been rejected by the approximation.
        """
        export_date = payment + timedelta(days=offset)
        deadline = add_gregorian_months(export_date, 6)
        naive = export_date + timedelta(days=180)

        assert deadline > naive
        assert 181 <= (deadline - export_date).days <= 184


class TestOneGregorianYearWindow:
    """Art. 16 §3(a) — re-export within one Gregorian year of duty payment."""

    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(payment_dates)
    def test_reexport_beyond_one_year_is_rejected(self, payment: date) -> None:
        export_date = payment + timedelta(days=400)
        result = _run([_entry(payment)], [_export(export_date)], export_date + timedelta(days=30))

        assert result.matches == ()
        codes = {r.code for r in result.rejections}
        assert RejectionCode.REEXPORT_WINDOW_EXPIRED in codes, codes

    def test_clock_runs_from_payment_not_import(self) -> None:
        """The distinction ZATCA's 30-day postponement makes real.

        Import 2024-01-15, duty paid 2024-02-08. A re-export on 2025-01-30 is 381 days
        after import — outside a year from import — but 357 days after payment, which is
        inside the window the statute actually grants.
        """
        entry = _entry(date(2024, 2, 8))
        assert entry.import_date == date(2024, 1, 15)
        export_date = date(2025, 1, 30)

        assert (export_date - entry.import_date).days == 381
        assert (export_date - entry.duty_payment_date).days == 357

        result = _run([entry], [_export(export_date)], export_date + timedelta(days=30))
        assert len(result.matches) == 1, [str(r) for r in result.rejections]


class TestMinimumValueThreshold:
    """Art. 16 §2 — USD 5,000 minimum re-export value."""

    def test_clearly_below_threshold_is_rejected(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        # 3,000 SAR = 800 USD, far under the minimum.
        result = _run(
            [_entry(payment)],
            [_export(export_date, value=Decimal("3000.00"))],
            export_date + timedelta(days=30),
        )

        assert result.matches == ()
        assert result.rejections[0].code is RejectionCode.BELOW_MINIMUM_VALUE
        assert "16 §2" in result.rejections[0].citation

    def test_missing_declared_value_cannot_evidence_the_minimum(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        result = _run(
            [_entry(payment)],
            [_export(export_date, value=None)],
            export_date + timedelta(days=30),
        )
        assert result.rejections[0].code is RejectionCode.BELOW_MINIMUM_VALUE

    def test_borderline_value_routes_to_review_rather_than_rejection(self) -> None:
        """The valuation basis ZATCA applies is an open question.

        18,000 SAR = 4,800 USD, four percent under the threshold. Rejecting on our own
        arithmetic would forfeit a possibly-valid claim silently, so borderline values
        are carried forward and flagged instead.
        """
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        result = _run(
            [_entry(payment)],
            [_export(export_date, value=Decimal("18000.00"))],
            export_date + timedelta(days=30),
        )
        assert len(result.matches) == 1


class TestLinkageGates:
    """Art. 15(c), Art. 16 §§1, 4, 5."""

    def test_missing_declaration_link_is_rejected(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        # jurisdiction=KSA forces the link at the schema level, so build a US-shaped
        # line and re-tag it to reach the matcher's own gate.
        export = _export(export_date).model_copy(update={"linked_import_declaration": None})
        result = _run([_entry(payment)], [export], export_date + timedelta(days=30))

        assert result.rejections[0].code is RejectionCode.NO_DECLARATION_LINK
        assert "15(c)" in result.rejections[0].citation

    def test_unknown_declaration_is_rejected(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        result = _run(
            [_entry(payment)],
            [_export(export_date, declaration="99999999999999")],
            export_date + timedelta(days=30),
        )
        assert result.rejections[0].code is RejectionCode.UNKNOWN_IMPORT_DECLARATION

    def test_used_or_altered_goods_are_rejected(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        result = _run(
            [_entry(payment)],
            [_export(export_date, unused=False)],
            export_date + timedelta(days=30),
        )
        assert result.rejections[0].code is RejectionCode.GOODS_USED_OR_ALTERED
        assert "16 §5" in result.rejections[0].citation

    def test_claimant_without_standing_is_rejected(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        result = GccLinkageMatcher().match(
            MatchRequest(
                imports=[_entry(payment)],
                exports=[_export(export_date)],
                profile=KSA_PROFILE,
                as_of=export_date + timedelta(days=30),
                claimant_is_importer_of_record=False,
                proof_of_purchase=False,
            )
        )
        assert result.matches == ()
        assert "16 §1" in result.rejections[0].citation

    def test_proof_of_purchase_restores_standing(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        result = GccLinkageMatcher().match(
            MatchRequest(
                imports=[_entry(payment)],
                exports=[_export(export_date)],
                profile=KSA_PROFILE,
                as_of=export_date + timedelta(days=30),
                claimant_is_importer_of_record=False,
                proof_of_purchase=True,
            )
        )
        assert len(result.matches) == 1


class TestGccNeverSubstitutes:
    """The failure that would look plausible all the way to a filing."""

    def test_different_hts_under_same_substitution_key_does_not_link(self) -> None:
        """Same 8-digit key, different full code. The US would pair these; GCC must not."""
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        export = _export(export_date).model_copy(update={"hts": HTSCode(code="84713050")})
        result = _run([_entry(payment)], [export], export_date + timedelta(days=30))

        assert result.matches == ()
        assert result.rejections[0].code is RejectionCode.QUANTITY_UNAVAILABLE

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(payment_dates, window_offsets)
    def test_emitted_theory_is_always_declaration_linkage(self, payment: date, offset: int) -> None:
        export_date = payment + timedelta(days=offset)
        result = _run([_entry(payment)], [_export(export_date)], export_date + timedelta(days=30))
        for match in result.matches:
            assert match.theory is MatchTheory.DECLARATION_LINKAGE
            assert match.substitution_key is None
            assert match.linked_import_declaration == DECLARATION
            assert KSA_PROFILE.permits(match.theory)


class TestGccQuantification:
    """VAT never reaches a drawback figure; the refund is duty in full."""

    def test_refund_excludes_vat_and_applies_no_haircut(self) -> None:
        payment = date(2024, 2, 8)
        export_date = payment + timedelta(days=90)
        # 500 of 1200 units: 46,875.00 x 500/1200 = 19,531.25
        result = _run(
            [_entry(payment)],
            [_export(export_date, quantity=500)],
            export_date + timedelta(days=30),
        )

        assert result.total_duty_allocated == Decimal("19531.25")
        # GCC refunds duty actually paid, in full.
        assert result.total_refund == Decimal("19531.25")

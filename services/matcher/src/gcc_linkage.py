"""Path B — GCC direct-identification linkage, Common Customs Law Art. 97.

This is not an optimisation. Rules of Implementation Art. 15(c) puts the import
declaration number *on* the re-export declaration: the link is a fact recorded on the
document, not a pairing to be selected. There is nothing to maximise.

What there is instead is a gate. Eight conditions, each traceable to an article, applied
cheapest-and-most-disqualifying first. Every failure returns the provision it failed and
the figures involved, because a claim that dies must be able to say why in the words of
the statute.

One import declaration may serve several re-export declarations (Art. 16 §4 permits part
shipments). A re-export may never draw on two import declarations — that would defeat the
identification the article requires.

See docs/ARCHITECTURE.md §3.7 and docs/COMPLIANCE-GCC.md §2.
"""

from __future__ import annotations

import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from drawbridge_schemas.jurisdiction import Currency, Jurisdiction, MatchTheory
from drawbridge_schemas.trade import EntryLine, ExportLine, LineMatch
from services.matcher.src.base import (
    MatchRequest,
    MatchResult,
    MatchStrategy,
    Rejection,
    RejectionCode,
    SolverStatus,
)
from services.rules.src.deadlines import window_for

if TYPE_CHECKING:
    from collections.abc import Sequence

# SAR is pegged to the USD at 3.75. The Art. 16 §2 threshold is written in US dollars
# "or its equivalent in the local currency", and the peg has held since 1986, so this is
# a fixed conversion rather than a rate lookup.
#
# COMPLIANCE-GCC.md §7 records the open question this does not resolve: whether ZATCA
# screens the threshold against customs value or invoice value. Claims within the review
# band below are flagged rather than decided.
SAR_PER_USD = Decimal("3.75")

# Claims whose value lands within this fraction of the threshold route to analyst review
# rather than being accepted or rejected on our own arithmetic.
THRESHOLD_REVIEW_BAND = Decimal("0.10")


class GccLinkageMatcher(MatchStrategy):
    """Deterministic declaration-linkage trace with statutory gates."""

    jurisdiction = Jurisdiction.KSA

    def match(self, request: MatchRequest) -> MatchResult:
        self._guard_jurisdiction(request)
        started = time.monotonic()

        by_declaration = _index_imports(request.imports)
        matches: list[LineMatch] = []
        rejections: list[Rejection] = []
        candidate_pairs = 0

        # Art. 16 §4 — part shipments must share a proven consignment. Track which
        # consignment each import declaration has already been claimed under.
        consignment_of: dict[str, str | None] = {}

        for export in sorted(request.exports, key=lambda e: (e.export_date, e.reference)):
            outcome = self._evaluate(request, export, by_declaration, consignment_of)
            if isinstance(outcome, Rejection):
                rejections.append(outcome)
                continue
            candidate_pairs += 1
            matches.append(outcome)

        elapsed = time.monotonic() - started
        status = SolverStatus.OPTIMAL if matches else SolverStatus.NO_CANDIDATES
        return MatchResult(
            jurisdiction=self.jurisdiction,
            status=status,
            matches=tuple(matches),
            rejections=tuple(rejections),
            candidate_pairs=candidate_pairs,
            wall_time_seconds=elapsed,
            detail=(
                f"{len(matches)} linked, {len(rejections)} rejected under "
                "GCC Rules of Implementation Art. 16"
            ),
        )

    # ------------------------------------------------------------------- the gate

    def _evaluate(
        self,
        request: MatchRequest,
        export: ExportLine,
        by_declaration: dict[str, list[EntryLine]],
        consignment_of: dict[str, str | None],
    ) -> LineMatch | Rejection:
        profile = request.profile

        # Gate 1 — Art. 15(c): the link must exist.
        declaration = export.linked_import_declaration
        if not declaration:
            return Rejection(
                export_line_id=str(export.line_id),
                code=RejectionCode.NO_DECLARATION_LINK,
                citation="GCC Rules of Implementation Art. 15(c)",
                detail=(
                    f"re-export {export.reference} line {export.line_number} carries no "
                    "import declaration number, so no identification is possible"
                ),
            )

        # Gate 1b — and must resolve.
        entries = by_declaration.get(declaration)
        if not entries:
            return Rejection(
                export_line_id=str(export.line_id),
                code=RejectionCode.UNKNOWN_IMPORT_DECLARATION,
                citation="GCC Rules of Implementation Art. 15(c)",
                detail=(
                    f"re-export {export.reference} references import declaration "
                    f"{declaration}, which is not among the {len(by_declaration)} "
                    "declarations ingested"
                ),
            )

        # Gate 2 — Art. 16 §2: USD 5,000 minimum. Screened before any date arithmetic
        # because it is the gate that disqualifies a claim before extraction spend is
        # worth making.
        value_check = _screen_minimum_value(export, profile)
        if value_check is not None:
            return value_check

        # Gate 6 — Art. 16 §5: unused and unaltered. Cheap, and absolute.
        if not export.unused_and_unaltered:
            return Rejection(
                export_line_id=str(export.line_id),
                code=RejectionCode.GOODS_USED_OR_ALTERED,
                citation="GCC Rules of Implementation Art. 16 §5",
                detail=(
                    f"re-export {export.reference} is flagged as locally used or altered "
                    "after import, which voids the claim"
                ),
            )

        # Gate 8 — Art. 16 §1: claimant must be the importer of record or prove purchase.
        if not (request.claimant_is_importer_of_record or request.proof_of_purchase):
            return Rejection(
                export_line_id=str(export.line_id),
                code=RejectionCode.NO_ELIGIBLE_IMPORT,
                citation="GCC Rules of Implementation Art. 16 §1",
                detail=(
                    "claimant is neither the importer of record nor has documented proof "
                    "of purchase of the foreign goods"
                ),
            )

        # Gate 7 — Art. 16 §4: part shipments must belong to one proven consignment.
        if export.is_partial_shipment and export.consignment_id is None:
            return Rejection(
                export_line_id=str(export.line_id),
                code=RejectionCode.CONSIGNMENT_MISMATCH,
                citation="GCC Rules of Implementation Art. 16 §4",
                detail=(
                    f"re-export {export.reference} is a part shipment but carries no "
                    "consignment identifier, so it cannot be proven to belong to the "
                    "same consignment as the import"
                ),
            )
        previous = consignment_of.get(declaration, _UNSET)
        if previous is not _UNSET and previous != export.consignment_id:
            return Rejection(
                export_line_id=str(export.line_id),
                code=RejectionCode.CONSIGNMENT_MISMATCH,
                citation="GCC Rules of Implementation Art. 16 §4",
                detail=(
                    f"import declaration {declaration} was already claimed under "
                    f"consignment {previous!r}; this re-export claims consignment "
                    f"{export.consignment_id!r}"
                ),
            )

        # Gates 3, 4, 5 — the dates. Walk the declaration's lines for one with quantity
        # available whose window is still open.
        window_failure: Rejection | None = None
        for entry in sorted(entries, key=lambda e: e.line_number):
            if entry.quantity_available <= 0:
                continue

            # GCC recognises no substitution: the linked declaration must actually cover
            # the article. Direct identity on the full tariff code.
            if entry.hts.code != export.hts.code:
                continue

            clock_start = entry.eligibility_clock_start
            window = window_for(profile, clock_start, export.export_date)

            # Gate 3 — Art. 16 §3(a): one Gregorian year from duty payment.
            if not window.export_in_window(export.export_date):
                window_failure = Rejection(
                    export_line_id=str(export.line_id),
                    import_line_id=str(entry.line_id),
                    code=RejectionCode.REEXPORT_WINDOW_EXPIRED,
                    citation=profile.reexport_window.citation,
                    detail=(
                        f"re-exported {export.export_date}, "
                        f"{(export.export_date - clock_start).days} days after duty "
                        f"payment {clock_start}; the permitted window closed "
                        f"{window.reexport_deadline}"
                    ),
                )
                continue

            # Gate 5 — Art. 174: three-year absolute bar, checked before the lane
            # deadline because it is the harder cut-off.
            if window.absolute_bar is not None and request.as_of > window.absolute_bar:
                window_failure = Rejection(
                    export_line_id=str(export.line_id),
                    import_line_id=str(entry.line_id),
                    code=RejectionCode.ABSOLUTE_BAR_PASSED,
                    citation=profile.absolute_bar.citation
                    if profile.absolute_bar
                    else "GCC Common Customs Law Art. 174",
                    detail=(
                        f"filing {request.as_of} is beyond the three-year bar "
                        f"{window.absolute_bar} running from duty payment {clock_start}"
                    ),
                )
                continue

            # Gate 4 — Art. 16 §3(b): six Gregorian months from re-export.
            if request.as_of > window.filing_deadline:
                window_failure = Rejection(
                    export_line_id=str(export.line_id),
                    import_line_id=str(entry.line_id),
                    code=RejectionCode.FILING_DEADLINE_PASSED,
                    citation=profile.claim_filing_deadline.citation,
                    detail=(
                        f"filing {request.as_of} is "
                        f"{(request.as_of - export.export_date).days} days after "
                        f"re-export {export.export_date}; six Gregorian months expired "
                        f"{window.filing_deadline}"
                    ),
                )
                continue

            quantity = min(entry.quantity_available, export.quantity_available)
            if quantity <= 0:
                continue

            duty_allocated = _apportion_duty(entry, quantity, profile)
            refund = (duty_allocated * profile.refund_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            consignment_of[declaration] = export.consignment_id

            return LineMatch(
                import_line_id=entry.line_id,
                export_line_id=export.line_id,
                quantity=quantity,
                theory=MatchTheory.DECLARATION_LINKAGE,
                linked_import_declaration=declaration,
                duty_allocated=duty_allocated,
                refund_amount=refund,
                days_clock_start_to_export=(export.export_date - clock_start).days,
            )

        if window_failure is not None:
            return window_failure

        return Rejection(
            export_line_id=str(export.line_id),
            code=RejectionCode.QUANTITY_UNAVAILABLE,
            citation="GCC Rules of Implementation Art. 16",
            detail=(
                f"import declaration {declaration} has no line matching HTS "
                f"{export.hts.code} with quantity available"
            ),
        )


# -------------------------------------------------------------------------- helpers

_UNSET = object()


def _index_imports(imports: Sequence[EntryLine]) -> dict[str, list[EntryLine]]:
    index: dict[str, list[EntryLine]] = defaultdict(list)
    for entry in imports:
        index[entry.declaration_number].append(entry)
    return dict(index)


def _screen_minimum_value(export: ExportLine, profile: object) -> Rejection | None:
    """Art. 16 §2 — value of re-exported goods must be at least USD 5,000.

    Returns a rejection, or None to continue. Claims within ten percent of the threshold
    are *accepted here* and flagged upstream rather than decided on our arithmetic: the
    valuation basis ZATCA applies is an open question (COMPLIANCE-GCC.md §7), and
    rejecting a borderline claim on an assumption forfeits it silently.
    """
    minimum = getattr(profile, "min_claim_value", None)
    if minimum is None:
        return None

    if export.declared_value is None:
        return Rejection(
            export_line_id=str(export.line_id),
            code=RejectionCode.BELOW_MINIMUM_VALUE,
            citation="GCC Rules of Implementation Art. 16 §2",
            detail=(
                f"re-export {export.reference} carries no declared value, so the "
                f"USD {minimum} minimum cannot be evidenced"
            ),
        )

    value_usd = _to_usd(export.declared_value, getattr(profile, "currency", Currency.SAR))
    if value_usd >= minimum:
        return None

    # Inside the review band the shortfall may be an artefact of the valuation basis.
    if value_usd >= minimum * (Decimal("1") - THRESHOLD_REVIEW_BAND):
        return None

    return Rejection(
        export_line_id=str(export.line_id),
        code=RejectionCode.BELOW_MINIMUM_VALUE,
        citation="GCC Rules of Implementation Art. 16 §2",
        detail=(
            f"re-export {export.reference} declared value {export.declared_value} "
            f"({value_usd.quantize(Decimal('0.01'))} USD) is below the "
            f"USD {minimum} minimum"
        ),
    )


def _to_usd(amount: Decimal, currency: Currency) -> Decimal:
    if currency is Currency.USD:
        return amount
    return amount / SAR_PER_USD


def _apportion_duty(entry: EntryLine, quantity: Decimal, profile: object) -> Decimal:
    """Pro-rata duty for the linked quantity, quantized to the cent.

    The GCC base excludes fees and consumption tax; `recoverable_base` applies the
    jurisdiction profile, so VAT on the Bayan never reaches a drawback figure.
    """
    if entry.quantity <= 0:
        return Decimal("0.00")
    base = entry.recoverable_base(profile)  # type: ignore[arg-type]
    share = (base * quantity) / entry.quantity
    return share.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

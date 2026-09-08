"""Turning a match result into rows.

This is where the pipeline stops being a computation and becomes state. Everything before
it is reproducible from its input; from here on the claim exists, has an identifier, and
is the thing an auditor will ask about.

Three properties the implementation is built around:

**Idempotent on the lines.** A pipeline run that fails after writing entry lines and is
retried must not duplicate them. Lines are keyed by the `line_id` extraction assigned, so
a re-run updates nothing and inserts nothing twice. The claim itself is *not* idempotent —
a second run produces a second claim, deliberately, because two claims over the same
period is a visible problem and a silently mutated claim is not.

**The figures are copied, never recomputed.** `duty_component` and `refund_amount` come
off the match verbatim. Recomputing them here would put a second implementation of the
refund arithmetic in the codebase, and the day the two disagree the database would be
authoritative for a number the matcher never produced.

**The deadline is derived, not supplied.** `filing_deadline` comes from
`rules.deadlines.window_for` over the matched pairs, taking the earliest — a claim is
bounded by its most urgent line, not its average one. Letting the caller pass a deadline
would let n8n's clock decide when a statutory window closes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from drawbridge_schemas.claim import ClaimState, DrawbackType, RecoveryLane
from drawbridge_schemas.jurisdiction import Jurisdiction, MatchTheory, profile_for
from services.api.src.ledger import record
from services.api.src.models import Claim, ClaimTransition, EntryLine, ExportLine, RefundLine
from services.rules.src.deadlines import window_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from drawbridge_schemas.trade import EntryLine as EntryLineSchema
    from drawbridge_schemas.trade import ExportLine as ExportLineSchema
    from drawbridge_schemas.trade import LineMatch


class PersistenceError(RuntimeError):
    """The claim could not be written. Never partially applied — see `sync_session`."""


def _figure_manifest(line: EntryLineSchema | ExportLineSchema) -> dict[str, Any]:
    """Every figure on a line, with the document and box it was read from.

    Written into the ledger at persistence time so a trace can be answered from an
    append-only row rather than from `entry_lines.provenance`, which is a live column an
    application bug or a well-meaning correction could rewrite. The line rows stay the
    working copy; the ledger is the copy an auditor is shown, and `trace_figure` compares
    the two rather than trusting either alone.
    """
    spans = line.provenance.figures
    return {
        name: {
            "document_id": str(span.document_id),
            "document_sha256": span.document_sha256,
            "page": span.page,
            "bbox": [span.x0, span.y0, span.x1, span.y1],
            "raw_text": span.raw_text,
            "extractor": span.extractor,
        }
        for name, span in spans.items()
    }


# Which lane a jurisdiction's drawback claims travel. Both are the ordinary case; the US
# alternates (PSC, §1520(d)) are separate lanes a claim is routed to deliberately, not
# something the pipeline falls into.
_LANE: dict[Jurisdiction, RecoveryLane] = {
    Jurisdiction.US: RecoveryLane.DRAWBACK,
    Jurisdiction.KSA: RecoveryLane.GCC_REEXPORT_DRAWBACK,
}

# The theory a claim is filed under, from the theories its lines actually rest on. A claim
# mixing direct identification with substitution is a substitution claim: the higher
# evidentiary burden governs the whole packet, because CBP reviews it as one.
_TYPE_BY_THEORY: dict[MatchTheory, DrawbackType] = {
    MatchTheory.DIRECT_IDENTITY: DrawbackType.UNUSED_DIRECT_IDENTITY,
    MatchTheory.HTS_SUBSTITUTION: DrawbackType.UNUSED_SUBSTITUTION,
    MatchTheory.MANUFACTURING_DIRECT_IDENTITY: DrawbackType.MANUFACTURING_DIRECT_IDENTITY,
    MatchTheory.MANUFACTURING_SUBSTITUTION: DrawbackType.MANUFACTURING_SUBSTITUTION,
    MatchTheory.DECLARATION_LINKAGE: DrawbackType.GCC_UNUSED_REEXPORT,
}

_BURDEN_ORDER: tuple[DrawbackType, ...] = (
    DrawbackType.MANUFACTURING_SUBSTITUTION,
    DrawbackType.UNUSED_SUBSTITUTION,
    DrawbackType.MANUFACTURING_DIRECT_IDENTITY,
    DrawbackType.UNUSED_DIRECT_IDENTITY,
    DrawbackType.GCC_UNUSED_REEXPORT,
)


def _drawback_type(matches: Sequence[LineMatch]) -> DrawbackType | None:
    present = {_TYPE_BY_THEORY[m.theory] for m in matches if m.theory in _TYPE_BY_THEORY}
    for candidate in _BURDEN_ORDER:
        if candidate in present:
            return candidate
    return None


def _upsert_entry_line(session: Session, line: EntryLineSchema, tenant_id: UUID) -> None:
    if session.get(EntryLine, line.line_id) is not None:
        return
    session.add(
        EntryLine(
            line_id=line.line_id,
            tenant_id=tenant_id,
            jurisdiction=line.jurisdiction.value,
            currency=line.currency.value,
            declaration_number=line.declaration_number,
            line_number=line.line_number,
            import_date=line.import_date,
            declaration_date=line.declaration_date,
            duty_payment_date=line.duty_payment_date,
            port_of_entry=line.port_of_entry,
            country_of_origin=line.country_of_origin,
            hts_code=line.hts.code,
            description=line.description,
            quantity=line.quantity,
            unit_of_measure=line.unit_of_measure,
            entered_value=line.entered_value,
            duty_paid=line.duty_paid,
            mpf_paid=line.mpf_paid,
            hmf_paid=line.hmf_paid,
            other_duty=line.other_duty,
            vat_paid=line.vat_paid,
            excise_paid=line.excise_paid,
            provenance=line.provenance.model_dump(mode="json"),
        )
    )


def _upsert_export_line(session: Session, line: ExportLineSchema, tenant_id: UUID) -> None:
    if session.get(ExportLine, line.line_id) is not None:
        return
    session.add(
        ExportLine(
            line_id=line.line_id,
            tenant_id=tenant_id,
            jurisdiction=line.jurisdiction.value,
            reference=line.reference,
            line_number=line.line_number,
            export_date=line.export_date,
            destination_country=line.destination_country,
            is_destruction=line.is_destruction,
            hts_code=line.hts.code,
            description=line.description,
            quantity=line.quantity,
            unit_of_measure=line.unit_of_measure,
            declared_value=line.declared_value,
            linked_import_declaration=line.linked_import_declaration,
            consignment_id=line.consignment_id,
            is_partial_shipment=line.is_partial_shipment,
            unused_and_unaltered=line.unused_and_unaltered,
            provenance=line.provenance.model_dump(mode="json"),
        )
    )


def _deadlines(
    jurisdiction: Jurisdiction,
    matches: Sequence[LineMatch],
    imports: dict[UUID, EntryLineSchema],
    exports: dict[UUID, ExportLineSchema],
) -> tuple[date, date | None]:
    """The earliest filing deadline across the matched pairs, and the absolute bar.

    Earliest rather than latest: one line running out of window puts the whole packet at
    risk, since the packet is filed as a unit.
    """
    profile = profile_for(jurisdiction)
    windows = [
        window_for(
            profile,
            clock_start=imports[m.import_line_id].eligibility_clock_start,
            export_date=exports[m.export_line_id].export_date,
        )
        for m in matches
    ]
    if not windows:
        msg = "cannot persist a claim with no matched lines"
        raise PersistenceError(msg)

    filing = min(w.effective_filing_deadline for w in windows)
    bars = [w.absolute_bar for w in windows if w.absolute_bar is not None]
    return filing, (min(bars) if bars else None)


def persist_claim(
    session: Session,
    *,
    tenant_id: UUID,
    jurisdiction: Jurisdiction,
    imports: Sequence[EntryLineSchema],
    exports: Sequence[ExportLineSchema],
    matches: Sequence[LineMatch],
    total_refund: Decimal,
    requires_review: bool,
    actor: str = "pipeline",
) -> dict[str, Any]:
    """Write the claim, its lines and its refund components as one transaction.

    `requires_review` decides the state the claim lands in, and nothing else. A claim
    routed to `analyst_review` is fully persisted — the figures, the derivation, the
    deadline — because an analyst who cannot see the numbers cannot review them.
    """
    if not matches:
        msg = "cannot persist a claim with no matched lines"
        raise PersistenceError(msg)

    by_import = {line.line_id: line for line in imports}
    by_export = {line.line_id: line for line in exports}
    missing = [str(m.import_line_id) for m in matches if m.import_line_id not in by_import] + [
        str(m.export_line_id) for m in matches if m.export_line_id not in by_export
    ]
    if missing:
        msg = f"match references {len(missing)} line(s) not in the request: {missing[:5]}"
        raise PersistenceError(msg)

    for entry in imports:
        _upsert_entry_line(session, entry, tenant_id)
    for export in exports:
        _upsert_export_line(session, export, tenant_id)
    session.flush()

    profile = profile_for(jurisdiction)
    filing_deadline, absolute_bar = _deadlines(jurisdiction, matches, by_import, by_export)
    import_dates = [by_import[m.import_line_id].import_date for m in matches]
    export_dates = [by_export[m.export_line_id].export_date for m in matches]

    state = ClaimState.ANALYST_REVIEW if requires_review else ClaimState.QUANTIFIED
    claim = Claim(
        claim_id=uuid4(),
        tenant_id=tenant_id,
        state=state.value,
        jurisdiction=jurisdiction.value,
        currency=profile.currency.value,
        lane=_LANE[jurisdiction].value,
        drawback_type=(t.value if (t := _drawback_type(matches)) else None),
        period_start=min(import_dates),
        period_end=max(export_dates),
        filing_deadline=filing_deadline,
        absolute_bar_date=absolute_bar,
        total_refund=total_refund,
    )
    session.add(claim)
    session.flush()

    for match in matches:
        session.add(
            RefundLine(
                refund_line_id=uuid4(),
                claim_id=claim.claim_id,
                import_line_id=match.import_line_id,
                export_line_id=match.export_line_id,
                theory=match.theory.value,
                substitution_key=match.substitution_key,
                linked_import_declaration=match.linked_import_declaration,
                quantity=match.quantity,
                duty_component=match.duty_allocated,
                refund_amount=match.refund_amount,
                days_clock_start_to_export=match.days_clock_start_to_export,
            )
        )

    # from_state is null: this is the claim coming into existence, not moving. A
    # transition row with a fabricated 'intake' origin would imply a state it never held.
    session.add(
        ClaimTransition(
            transition_id=uuid4(),
            claim_id=claim.claim_id,
            from_state=None,
            to_state=state.value,
            actor=actor,
            reason="pipeline persisted the matched claim",
            occurred_at=datetime.now(UTC),
        )
    )

    record(
        session,
        tenant_id=tenant_id,
        claim_id=claim.claim_id,
        event_type="claim_persisted",
        actor=actor,
        subject=state.value,
        payload={
            "jurisdiction": jurisdiction.value,
            "total_refund": str(total_refund),
            "refund_lines": len(matches),
            "filing_deadline": filing_deadline.isoformat(),
            "requires_review": requires_review,
        },
    )
    traceable: list[EntryLineSchema | ExportLineSchema] = [*imports, *exports]
    for line in traceable:
        manifest = _figure_manifest(line)
        if not manifest:
            continue
        record(
            session,
            tenant_id=tenant_id,
            claim_id=claim.claim_id,
            event_type="figure_traced",
            actor=actor,
            subject=str(line.line_id),
            document_sha256=next(iter(manifest.values()))["document_sha256"],
            payload={"line_id": str(line.line_id), "figures": manifest},
        )
    session.flush()

    return {
        "claim_id": str(claim.claim_id),
        "state": claim.state,
        "jurisdiction": claim.jurisdiction,
        "currency": claim.currency,
        "lane": claim.lane,
        "drawback_type": claim.drawback_type,
        "period_start": claim.period_start.isoformat(),
        "period_end": claim.period_end.isoformat(),
        "filing_deadline": claim.filing_deadline.isoformat(),
        "absolute_bar_date": (
            claim.absolute_bar_date.isoformat() if claim.absolute_bar_date else None
        ),
        "total_refund": str(claim.total_refund),
        "refund_lines": len(matches),
        "requires_review": requires_review,
    }

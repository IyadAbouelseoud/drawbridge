"""The analyst path, end to end against a real database.

The claim under test: an exception raised by week 4's triage can be fetched, resolved
through the same code the MCP tools call, and the claim reaches `approved` — with the
audit trail intact and the guards holding.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from services.api.src.analyst import (
    MIN_REASONING_CHARS,
    AnalystError,
    InsufficientReasoningError,
    approve_claim,
    claim_history,
    claim_summary,
    get_exception,
    list_queue,
    override_valuation,
    reopen_exception,
    resolve_exception,
    transition_claim,
)
from services.api.src.models import Claim, ReviewQueue, Tenant
from tests.integration.conftest import make_exception

REASON = "Verified re-export value 21,000 SAR against Bayan 20240115447821 line 1."


class TestQueueRead:
    def test_open_exception_appears_in_the_queue(
        self, session: Session, tenant: Tenant, exception_row: ReviewQueue
    ) -> None:
        items = list_queue(session, tenant_id=tenant.tenant_id)
        assert [i["review_id"] for i in items] == [str(exception_row.review_id)]
        assert items[0]["citation"] == "GCC Rules of Implementation Art. 16 §2"

    def test_queue_orders_blocking_before_older_low_severity(
        self, session: Session, tenant: Tenant, claim: Claim
    ) -> None:
        """Age alone is the wrong sort — a deadline kills a claim, a queue position does not."""
        make_exception(
            session,
            tenant_id=tenant.tenant_id,
            claim_id=claim.claim_id,
            reason="threshold_near_miss",
            severity="low",
        )
        blocking = make_exception(
            session,
            tenant_id=tenant.tenant_id,
            claim_id=claim.claim_id,
            reason="rate_unavailable",
            severity="blocking",
        )
        items = list_queue(session, tenant_id=tenant.tenant_id)
        assert items[0]["review_id"] == str(blocking.review_id)

    def test_inspect_returns_the_matcher_payload(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        """The provisions that failed must be visible without re-running the match."""
        detail = get_exception(session, exception_row.review_id)
        assert "near_miss" in detail["payload"]["details"][0]
        assert detail["workflow_run_id"] == "wf-integration-1"

    def test_filter_by_reason(self, session: Session, tenant: Tenant, claim: Claim) -> None:
        make_exception(
            session,
            tenant_id=tenant.tenant_id,
            claim_id=claim.claim_id,
            reason="rate_unavailable",
            severity="blocking",
        )
        items = list_queue(session, tenant_id=tenant.tenant_id, reason="rate_unavailable")
        assert len(items) == 1
        assert items[0]["reason"] == "rate_unavailable"


class TestReasoningIsMandatory:
    """A decision without a recorded reason is indistinguishable from an unexamined one."""

    def test_empty_reasoning_is_refused(self, session: Session, exception_row: ReviewQueue) -> None:
        with pytest.raises(InsufficientReasoningError, match="audit reasoning"):
            resolve_exception(
                session,
                review_id=exception_row.review_id,
                resolution="approved",
                reasoning="",
                analyst="iyad",
            )

    def test_token_reasoning_is_refused(self, session: Session, exception_row: ReviewQueue) -> None:
        with pytest.raises(InsufficientReasoningError):
            resolve_exception(
                session,
                review_id=exception_row.review_id,
                resolution="approved",
                reasoning="ok",
                analyst="iyad",
            )

    def test_the_floor_is_where_it_says_it_is(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        just_enough = "x" * MIN_REASONING_CHARS
        outcome = resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=just_enough,
            analyst="iyad",
        )
        assert outcome["resolution"] == "approved"

    def test_approval_also_requires_reasoning(self, session: Session, claim: Claim) -> None:
        with pytest.raises(InsufficientReasoningError):
            approve_claim(session, claim_id=claim.claim_id, reasoning="fine", analyst="iyad")


class TestResolution:
    def test_resolving_records_the_decision(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        outcome = resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        assert outcome["may_resume"] is True
        assert outcome["outstanding_exceptions"] == 0
        assert outcome["workflow_run_id"] == "wf-integration-1"

        row = session.get(ReviewQueue, exception_row.review_id)
        assert row.state == "resolved"
        assert row.resolution_note == REASON
        assert row.assigned_to == "iyad"
        assert row.resolved_at is not None

    def test_second_exception_blocks_resume(
        self, session: Session, tenant: Tenant, claim: Claim, exception_row: ReviewQueue
    ) -> None:
        """Suspend writes one row per reason so resolving one cannot clear another."""
        make_exception(
            session,
            tenant_id=tenant.tenant_id,
            claim_id=claim.claim_id,
            reason="low_extraction_confidence",
            severity="high",
        )
        outcome = resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        assert outcome["may_resume"] is False
        assert outcome["outstanding_exceptions"] == 1

    def test_deferring_does_not_resume(self, session: Session, exception_row: ReviewQueue) -> None:
        """Deferring is a decision to look again later, not a decision to proceed."""
        outcome = resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="deferred",
            reasoning="Awaiting the original commercial invoice from the forwarder.",
            analyst="iyad",
        )
        assert outcome["may_resume"] is False

    def test_rejecting_does_not_resume(self, session: Session, exception_row: ReviewQueue) -> None:
        outcome = resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="rejected",
            reasoning="Value confirmed at 18,000 SAR; genuinely below the Art. 16 §2 floor.",
            analyst="iyad",
        )
        assert outcome["may_resume"] is False

    def test_double_resolution_is_refused(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        with pytest.raises(AnalystError, match="already resolved"):
            resolve_exception(
                session,
                review_id=exception_row.review_id,
                resolution="rejected",
                reasoning=REASON,
                analyst="someone-else",
            )

    def test_unknown_resolution_is_refused(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        with pytest.raises(AnalystError, match="resolution must be"):
            resolve_exception(
                session,
                review_id=exception_row.review_id,
                resolution="probably-fine",
                reasoning=REASON,
                analyst="iyad",
            )


class TestValuationOverride:
    def test_override_records_figure_and_basis(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        outcome = override_valuation(
            session,
            review_id=exception_row.review_id,
            corrected_value=Decimal("21000.00"),
            currency="SAR",
            valuation_basis="art_28_export",
            reasoning=REASON,
            analyst="iyad",
        )
        assert outcome["resolution"] == "corrected"
        assert outcome["requires_rematch"] is True

        row = session.get(ReviewQueue, exception_row.review_id)
        override = row.payload["valuation_override"]
        assert override["corrected_value"] == "21000.00"
        assert override["valuation_basis"] == "art_28_export"
        assert "Art. 28" in override["citation"]

    def test_override_does_not_patch_the_refund(
        self, session: Session, claim: Claim, exception_row: ReviewQueue
    ) -> None:
        """Rewriting a refund without re-deriving it would break the provenance chain."""
        before = claim.total_refund
        override_valuation(
            session,
            review_id=exception_row.review_id,
            corrected_value=Decimal("21000.00"),
            currency="SAR",
            valuation_basis="art_28_export",
            reasoning=REASON,
            analyst="iyad",
        )
        assert session.get(Claim, claim.claim_id).total_refund == before

    def test_negative_override_is_refused(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        with pytest.raises(AnalystError, match="must be positive"):
            override_valuation(
                session,
                review_id=exception_row.review_id,
                corrected_value=Decimal("-1.00"),
                currency="SAR",
                valuation_basis="art_28_export",
                reasoning=REASON,
                analyst="iyad",
            )


class TestApproval:
    """The headline claim: exception -> resolve -> approved."""

    def test_full_path_reaches_approved(
        self, session: Session, claim: Claim, exception_row: ReviewQueue
    ) -> None:
        assert claim.state == "analyst_review"

        resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        outcome = approve_claim(
            session,
            claim_id=claim.claim_id,
            reasoning="All exceptions cleared; refund reconciles to the Bayan duty line.",
            analyst="iyad",
        )

        assert outcome["from_state"] == "analyst_review"
        assert outcome["state"] == "approved"
        assert session.get(Claim, claim.claim_id).state == "approved"

    def test_approval_is_refused_while_an_exception_is_open(
        self,
        session: Session,
        claim: Claim,
        exception_row: ReviewQueue,  # noqa: ARG002 - creates the blocking row
    ) -> None:
        with pytest.raises(AnalystError, match="unresolved exception"):
            approve_claim(
                session,
                claim_id=claim.claim_id,
                reasoning="Looks fine to me, approving without reading the queue.",
                analyst="iyad",
            )

    def test_approval_is_refused_from_the_wrong_state(
        self, session: Session, claim: Claim, exception_row: ReviewQueue
    ) -> None:
        """A claim reaches approval only from analyst_review."""
        resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        transition_claim(
            session,
            claim_id=claim.claim_id,
            to_state="rejected",
            actor="iyad",
            reason="withdrawn",
        )
        with pytest.raises(AnalystError, match="cannot move to approved"):
            approve_claim(
                session,
                claim_id=claim.claim_id,
                reasoning="Trying to approve a rejected claim, which must not work.",
                analyst="iyad",
            )

    def test_approval_writes_an_audit_transition(
        self, session: Session, claim: Claim, exception_row: ReviewQueue
    ) -> None:
        resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        note = "Refund reconciles to Bayan 20240115447821 duty line; approved for filing."
        approve_claim(session, claim_id=claim.claim_id, reasoning=note, analyst="iyad")

        history = claim_history(session, claim.claim_id)
        assert len(history) == 1
        assert history[0]["from_state"] == "analyst_review"
        assert history[0]["to_state"] == "approved"
        assert history[0]["actor"] == "iyad"
        assert history[0]["reason"] == note

    def test_unknown_claim_is_refused(self, session: Session) -> None:
        with pytest.raises(AnalystError, match="no claim"):
            approve_claim(
                session,
                claim_id=uuid4(),
                reasoning="Approving a claim that does not exist should fail.",
                analyst="iyad",
            )


class TestStateMachineGuards:
    def test_illegal_transition_is_refused(self, session: Session, claim: Claim) -> None:
        """analyst_review cannot jump straight to packaged."""
        with pytest.raises(AnalystError, match="cannot move to"):
            transition_claim(
                session,
                claim_id=claim.claim_id,
                to_state="packaged",
                actor="iyad",
            )

    def test_unknown_state_is_refused(self, session: Session, claim: Claim) -> None:
        with pytest.raises(AnalystError, match="unknown claim state"):
            transition_claim(session, claim_id=claim.claim_id, to_state="vibes", actor="iyad")

    def test_terminal_state_cannot_move(self, session: Session, claim: Claim) -> None:
        transition_claim(session, claim_id=claim.claim_id, to_state="rejected", actor="iyad")
        with pytest.raises(AnalystError, match="cannot move to"):
            transition_claim(session, claim_id=claim.claim_id, to_state="approved", actor="iyad")


class TestReopen:
    def test_reopen_preserves_the_prior_decision(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        """ "Approved then reopened" is materially different from "always open"."""
        resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        outcome = reopen_exception(
            session,
            review_id=exception_row.review_id,
            reasoning="Forwarder supplied a revised invoice; the value needs re-checking.",
            analyst="reviewer",
        )
        assert outcome["prior_resolution"] == "approved"

        row = session.get(ReviewQueue, exception_row.review_id)
        assert row.state == "open"
        assert row.resolution is None
        assert REASON in row.resolution_note
        assert "reopened by reviewer" in row.resolution_note

    def test_cannot_reopen_an_open_exception(
        self, session: Session, exception_row: ReviewQueue
    ) -> None:
        with pytest.raises(AnalystError, match="not resolved"):
            reopen_exception(
                session,
                review_id=exception_row.review_id,
                reasoning="Trying to reopen something that was never resolved.",
                analyst="reviewer",
            )

    def test_reopening_blocks_approval_again(
        self, session: Session, claim: Claim, exception_row: ReviewQueue
    ) -> None:
        resolve_exception(
            session,
            review_id=exception_row.review_id,
            resolution="approved",
            reasoning=REASON,
            analyst="iyad",
        )
        reopen_exception(
            session,
            review_id=exception_row.review_id,
            reasoning="Revised invoice received; the valuation needs a second look.",
            analyst="reviewer",
        )
        with pytest.raises(AnalystError, match="unresolved exception"):
            approve_claim(
                session,
                claim_id=claim.claim_id,
                reasoning="Should be refused because the exception is open again.",
                analyst="iyad",
            )


class TestClaimSummary:
    def test_summary_reports_open_exception_count(
        self,
        session: Session,
        claim: Claim,
        exception_row: ReviewQueue,  # noqa: ARG002 - creates the row being counted
    ) -> None:
        summary = claim_summary(session, claim.claim_id)
        assert summary["state"] == "analyst_review"
        assert summary["jurisdiction"] == "ksa"
        assert summary["currency"] == "SAR"
        assert summary["open_exceptions"] == 1
        assert summary["filing_deadline"] == "2025-03-12"
        assert summary["absolute_bar_date"] == "2027-02-08"

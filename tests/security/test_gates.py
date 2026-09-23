"""The approval gates, as a matrix: every kind of actor against every high-impact move.

Pure — no database, no transport. `services/api/src/gates.py` takes facts and an actor and
answers; the integration suite (`test_audit_trail.py`) proves the answers are asked at the
right moments. Split this way so the policy itself can be read, and argued with, as a table.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from drawbridge_schemas.agents import Role, Scope, scopes_for_roles
from drawbridge_schemas.claim import ClaimState
from services.api.src.gates import (
    Actor,
    ActorKind,
    GateRefusedError,
    ReviewFact,
    check_resolution,
    check_transition,
    is_high_value,
    require_human,
    value_in_usd,
)

PIPELINE = Actor(
    name="agent:n8n-pipeline",
    kind=ActorKind.MACHINE,
    permissions=frozenset(
        s.value
        for s in (
            Scope.CLAIMS_TRANSITION,
            Scope.CLAIMS_PERSIST,
            Scope.REVIEW_SUSPEND,
            Scope.PACKAGING_BUILD,
        )
    ),
    agent_id="agent:n8n-pipeline",
)


def human(name: str, *roles: Role) -> Actor:
    return Actor(
        name=name,
        kind=ActorKind.HUMAN,
        permissions=frozenset(s.value for s in scopes_for_roles(set(roles))),
    )


ALICE = human("alice@tenant", Role.ANALYST)
BOB = human("bob@tenant", Role.APPROVER)
CAROL = human("carol@tenant", Role.AUDITOR)
OPERATOR = human("ops@tenant", Role.OPERATOR)
LOCAL = Actor(name="cli", kind=ActorKind.LOCAL, permissions=None)

SMALL = Decimal("25092.14")
LARGE = Decimal("250000.00")


def resolved(reason: str, resolution: str, by: str) -> ReviewFact:
    return ReviewFact(reason=reason, state="resolved", resolution=resolution, resolved_by=by)


def move(
    actor: Actor,
    current: ClaimState,
    target: ClaimState,
    amount: Decimal = SMALL,
    currency: str = "USD",
    reviews: tuple[ReviewFact, ...] = (),
) -> None:
    check_transition(
        actor, current=current, target=target, amount=amount, currency=currency, reviews=reviews
    )


class TestTheMachineLane:
    @pytest.mark.parametrize("target", [ClaimState.HANDED_OFF, ClaimState.FILED, ClaimState.PAID])
    def test_the_pipeline_never_releases_a_claim(self, target: ClaimState) -> None:
        """Past PACKAGED the packet has left our control; only a person attests to that."""
        current = {
            ClaimState.HANDED_OFF: ClaimState.PACKAGED,
            ClaimState.FILED: ClaimState.HANDED_OFF,
            ClaimState.PAID: ClaimState.FILED,
        }[target]
        with pytest.raises(GateRefusedError, match="attested by a person"):
            move(PIPELINE, current, target)

    def test_the_clean_lane_below_the_ceiling_still_works(self) -> None:
        """Week 9's automated lane is intact: nothing to decide, small claim, approved."""
        move(PIPELINE, ClaimState.QUANTIFIED, ClaimState.APPROVED)

    def test_above_the_ceiling_it_needs_an_approvers_row(self) -> None:
        with pytest.raises(GateRefusedError, match="auto-approve ceiling"):
            move(PIPELINE, ClaimState.QUANTIFIED, ClaimState.APPROVED, LARGE)

    def test_with_the_approvers_row_resolved_it_may_proceed(self) -> None:
        move(
            PIPELINE,
            ClaimState.ANALYST_REVIEW,
            ClaimState.APPROVED,
            LARGE,
            reviews=(resolved("high_value_approval", "approved", BOB.name),),
        )

    @pytest.mark.parametrize("resolution", ["rejected", "deferred"])
    def test_it_cannot_approve_over_a_human_no(self, resolution: str) -> None:
        """`deferred` counted as decided until v1.1.0, so the pipeline approved over it."""
        with pytest.raises(GateRefusedError, match="rejected or deferred"):
            move(
                PIPELINE,
                ClaimState.ANALYST_REVIEW,
                ClaimState.APPROVED,
                reviews=(resolved("threshold_near_miss", resolution, ALICE.name),),
            )

    def test_it_abandons_its_own_work_freely(self) -> None:
        move(PIPELINE, ClaimState.EXTRACTING, ClaimState.REJECTED)

    def test_it_rejects_a_reviewed_claim_only_after_a_person_did(self) -> None:
        with pytest.raises(GateRefusedError, match="resolved as rejected"):
            move(PIPELINE, ClaimState.ANALYST_REVIEW, ClaimState.REJECTED)
        move(
            PIPELINE,
            ClaimState.ANALYST_REVIEW,
            ClaimState.REJECTED,
            reviews=(resolved("threshold_near_miss", "rejected", ALICE.name),),
        )

    def test_it_can_never_resolve_an_exception(self) -> None:
        with pytest.raises(GateRefusedError, match="human decision"):
            check_resolution(
                PIPELINE, reason="threshold_near_miss", resolution="approved", siblings=()
            )

    def test_a_machine_holding_the_scope_is_still_refused(self) -> None:
        """The token cannot talk its way in: machine-ness, not the scope list, decides."""
        forged = Actor(
            name="agent:n8n-pipeline",
            kind=ActorKind.MACHINE,
            permissions=frozenset({Scope.REVIEW_RESOLVE.value, Scope.VALUATION_OVERRIDE.value}),
        )
        with pytest.raises(GateRefusedError):
            require_human(forged, Scope.VALUATION_OVERRIDE, "overriding a valuation")


class TestPeople:
    def test_an_analyst_approves_below_the_ceiling(self) -> None:
        move(ALICE, ClaimState.ANALYST_REVIEW, ClaimState.APPROVED)

    def test_an_analyst_cannot_approve_above_it(self) -> None:
        with pytest.raises(GateRefusedError, match="approved by an approver"):
            move(ALICE, ClaimState.ANALYST_REVIEW, ClaimState.APPROVED, LARGE)

    def test_an_approver_can(self) -> None:
        move(BOB, ClaimState.ANALYST_REVIEW, ClaimState.APPROVED, LARGE)

    def test_four_eyes_the_approver_did_not_resolve_the_exceptions(self) -> None:
        reviews = (resolved("threshold_near_miss", "approved", BOB.name),)
        with pytest.raises(GateRefusedError, match="second person"):
            move(BOB, ClaimState.ANALYST_REVIEW, ClaimState.APPROVED, LARGE, reviews=reviews)

    def test_releasing_a_packet_is_the_approvers(self) -> None:
        with pytest.raises(GateRefusedError, match="claims:release"):
            move(ALICE, ClaimState.PACKAGED, ClaimState.HANDED_OFF)
        move(BOB, ClaimState.PACKAGED, ClaimState.HANDED_OFF)

    def test_an_auditor_moves_nothing(self) -> None:
        with pytest.raises(GateRefusedError, match="claims:transition"):
            move(CAROL, ClaimState.QUANTIFIED, ClaimState.ANALYST_REVIEW)

    def test_the_operator_who_can_stop_the_system_cannot_move_money(self) -> None:
        """Separation of duties, as a property of the role table."""
        with pytest.raises(GateRefusedError):
            move(OPERATOR, ClaimState.ANALYST_REVIEW, ClaimState.APPROVED)

    def test_a_high_value_row_is_an_approvers_to_resolve(self) -> None:
        with pytest.raises(GateRefusedError, match="claims:release"):
            check_resolution(
                ALICE, reason="high_value_approval", resolution="approved", siblings=()
            )
        check_resolution(BOB, reason="high_value_approval", resolution="approved", siblings=())

    def test_and_not_by_whoever_resolved_the_rest(self) -> None:
        siblings = (resolved("low_extraction_confidence", "corrected", BOB.name),)
        with pytest.raises(GateRefusedError, match="second person"):
            check_resolution(
                BOB, reason="high_value_approval", resolution="approved", siblings=siblings
            )

    def test_an_analyst_may_still_reject_a_high_value_row(self) -> None:
        """Only the release is an approver's. Saying no stays with whoever is looking."""
        check_resolution(ALICE, reason="high_value_approval", resolution="rejected", siblings=())


class TestLocalCallers:
    def test_in_process_code_is_not_role_checked(self) -> None:
        move(LOCAL, ClaimState.PACKAGED, ClaimState.HANDED_OFF)
        move(LOCAL, ClaimState.ANALYST_REVIEW, ClaimState.APPROVED, LARGE)


class TestTheCeiling:
    def test_sar_converts_at_the_peg(self) -> None:
        assert value_in_usd(Decimal("375000"), "SAR") == Decimal("100000.00")

    def test_exactly_at_the_ceiling_is_not_above_it(self) -> None:
        assert is_high_value(Decimal("100000"), "USD") is False
        assert is_high_value(Decimal("100000.01"), "USD") is True

    def test_a_currency_with_no_fixed_rate_counts_as_high_value(self) -> None:
        """The safe direction for an unknown is a person."""
        assert is_high_value(Decimal("1"), "EUR") is True

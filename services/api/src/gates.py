"""Approval gates: which actor may make which high-impact move, and on what evidence.

The state machine (`ClaimState.can_move_to`) says where a claim may go. It has never said
*who* may take it there, and until v1.1.0 nothing else did either: the pipeline's service
token could move a claim to `handed_off`, `filed` or `paid`; the REST resolve route let the
same token clear the exceptions that had stopped it; and the actor recorded on each move
was whatever string the request body supplied.

This module is the second question, asked at every transition and every decision, by
every caller — REST, MCP and n8n alike — because the checks live in `analyst.py` where the
state changes, not at the edges where they arrive.

**The high-impact actions in this workflow, and their gates:**

- **Resolve an exception** — a human with `review:resolve`; reasoning of at least 20
  characters (unchanged).
- **Resolve a `high_value_approval` row** — a human **approver** who did not resolve the
  claim's other exceptions.
- **Override a valuation** — a human with `valuation:override`; reasoning, and both figures
  in the ledger.
- **→ APPROVED** — the pipeline or an analyst, once every exception is decided. The
  pipeline additionally may not approve over an exception a person rejected or deferred,
  nor above the ceiling without a resolved high-value row.
- **→ APPROVED above the ceiling, by a human** — an **approver**, four-eyes as above.
- **→ HANDED_OFF / FILED / PAID** — a human **approver**; never the pipeline.
- **→ REJECTED** — the pipeline only from a state it is still working, or where a person
  already rejected an exception.

**Four-eyes is enforced where it is cheap to enforce**: an approver releasing a high-value
claim may not be the analyst who resolved its exceptions or overrode its valuation. The
comparison is on verified token subjects, which is why it could not exist while identity
came from the request body.

**The ceiling** (`Settings.auto_approve_ceiling_usd`, default USD 100,000) is a policy
number, not a measured one. It is where "the machine found nothing to question" stops
being sufficient on its own, and a deployment should set it from its own risk appetite.
SAR converts at the SAMA peg of 3.75, which has held since 1986 and is a statute-level
constant rather than a market rate — the same reasoning `COMPLIANCE-GCC.md` §7.1 gives for
not fetching one.

**In-process callers are not gated on role.** A CLI script or a test that calls
`analyst.approve_claim` directly already holds a database session and could write the row
itself; the gate exists for the paths a network caller reaches, where the principal is
verified. The evidence rules — no approval over an undecided exception — apply to everyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from drawbridge_schemas.agents import Scope
from drawbridge_schemas.claim import ClaimState
from services.api.src.auth import current_principal

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Riyals per US dollar. SAMA peg, unchanged since 1986.
SAR_PER_USD = Decimal("3.75")

#: States the pipeline itself works through, and may therefore abandon.
_PIPELINE_WORKING = frozenset(
    {
        ClaimState.INTAKE,
        ClaimState.EXTRACTING,
        ClaimState.CLASSIFYING,
        ClaimState.MATCHING,
    }
)

#: Past PACKAGED the packet has left our control, and the next two states are facts about
#: the outside world (a filer transmitted it; a Treasury paid it) that only a person can
#: attest.
RELEASE_STATES = frozenset({ClaimState.HANDED_OFF, ClaimState.FILED, ClaimState.PAID})

#: Review resolutions that let a claim continue.
_CONTINUING = frozenset({"approved", "corrected"})

HIGH_VALUE_REASON = "high_value_approval"


class GateRefusedError(PermissionError):
    """A decision refused because of who was making it, or on what evidence.

    A `PermissionError`, which the REST routes report as 403 and the MCP tools return as
    data. Distinct from `AnalystError` (409): that says the claim is not where the caller
    thought, this says the caller may not take it there.
    """


class ActorKind(StrEnum):
    HUMAN = "human"
    MACHINE = "machine"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is acting, as the gates need to see them."""

    name: str
    kind: ActorKind
    permissions: frozenset[str] | None
    """None for a local caller, which is not role-checked (see the module docstring)."""
    agent_id: str | None = None

    def can(self, scope: Scope) -> bool:
        return self.permissions is None or scope.value in self.permissions


def current_actor(declared: str | None = None) -> Actor:
    """The verified principal, or a local caller named by what it declared."""
    principal = current_principal()
    if principal is None:
        return Actor(name=declared or "local", kind=ActorKind.LOCAL, permissions=None)
    return Actor(
        name=principal.subject,
        kind=ActorKind.MACHINE if principal.is_machine else ActorKind.HUMAN,
        permissions=principal.effective_permissions,
        agent_id=principal.agent_id,
    )


@dataclass(frozen=True, slots=True)
class ReviewFact:
    """What the gates need to know about one review row on the claim."""

    reason: str
    state: str
    resolution: str | None
    resolved_by: str | None


def auto_approve_ceiling_usd() -> Decimal:
    from services.api.src.config import get_settings

    return Decimal(get_settings().auto_approve_ceiling_usd)


def value_in_usd(amount: Decimal, currency: str) -> Decimal | None:
    """The refund in USD, or None for a currency with no fixed conversion."""
    code = (currency or "").upper()
    if code == "USD":
        return amount
    if code == "SAR":
        return (amount / SAR_PER_USD).quantize(Decimal("0.01"))
    return None


def is_high_value(amount: Decimal, currency: str) -> bool:
    """Above the ceiling — and a currency we cannot convert counts as above it."""
    usd = value_in_usd(amount, currency)
    return usd is None or usd > auto_approve_ceiling_usd()


def _refuse(message: str) -> None:
    raise GateRefusedError(message)


def require_human(actor: Actor, scope: Scope, action: str) -> None:
    """A decision only a person may make, and only a person holding `scope`."""
    if actor.kind is ActorKind.MACHINE:
        _refuse(
            f"{action} is a human decision; {actor.name} is a machine identity and "
            "cannot make it, whatever its token says"
        )
    if not actor.can(scope):
        _refuse(f"{action} requires scope {scope.value}, which {actor.name} does not hold")


def _four_eyes(actor: Actor, reviews: Iterable[ReviewFact], action: str) -> None:
    others = {
        r.resolved_by
        for r in reviews
        if r.state == "resolved" and r.reason != HIGH_VALUE_REASON and r.resolved_by
    }
    if actor.kind is not ActorKind.LOCAL and actor.name in others:
        _refuse(
            f"{action} must be made by someone other than the analyst who resolved this "
            f"claim's exceptions ({actor.name}); a high-value release needs a second person"
        )


def check_resolution(
    actor: Actor, *, reason: str, resolution: str, siblings: Iterable[ReviewFact]
) -> None:
    """Gate on resolving one review row."""
    require_human(actor, Scope.REVIEW_RESOLVE, "resolving an exception")
    if reason == HIGH_VALUE_REASON and resolution in _CONTINUING:
        if not actor.can(Scope.CLAIMS_RELEASE):
            _refuse(
                "a high-value claim is approved by an approver; resolving this row "
                f"requires scope {Scope.CLAIMS_RELEASE.value}"
            )
        _four_eyes(actor, siblings, "approving a high-value claim")


def check_transition(
    actor: Actor,
    *,
    current: ClaimState,
    target: ClaimState,
    amount: Decimal,
    currency: str,
    reviews: Iterable[ReviewFact],
) -> None:
    """Gate on one claim transition. Raises `GateRefusedError`; returns None when allowed."""
    reviews = tuple(reviews)

    if actor.kind is not ActorKind.LOCAL and not actor.can(Scope.CLAIMS_TRANSITION):
        _refuse(f"moving a claim requires scope {Scope.CLAIMS_TRANSITION.value}")

    if actor.kind is ActorKind.MACHINE:
        _machine_transition(actor, current, target, amount, currency, reviews)
    elif actor.kind is ActorKind.HUMAN:
        _human_transition(actor, current, target, amount, currency, reviews)


def _machine_transition(
    actor: Actor,
    current: ClaimState,
    target: ClaimState,
    amount: Decimal,
    currency: str,
    reviews: tuple[ReviewFact, ...],
) -> None:
    if target in RELEASE_STATES:
        _refuse(
            f"{target.value} is attested by a person; {actor.name} may take a claim as far "
            "as packaged and no further"
        )
    if target is ClaimState.REJECTED and current not in _PIPELINE_WORKING:
        rejected_by_human = any(
            r.state == "resolved" and r.resolution == "rejected" for r in reviews
        )
        if not rejected_by_human:
            _refuse(
                f"the pipeline may abandon a claim it is still working on; rejecting one in "
                f"{current.value} needs an exception a person resolved as rejected"
            )
    if target is ClaimState.APPROVED:
        undecided = [
            r for r in reviews if r.state == "resolved" and r.resolution in {"rejected", "deferred"}
        ]
        if undecided:
            _refuse(
                "the pipeline may not approve a claim carrying an exception a person "
                f"rejected or deferred ({', '.join(sorted({r.reason for r in undecided}))})"
            )
        if is_high_value(amount, currency):
            released = any(
                r.reason == HIGH_VALUE_REASON
                and r.state == "resolved"
                and r.resolution in _CONTINUING
                for r in reviews
            )
            if not released:
                _refuse(
                    f"a refund of {amount} {currency} is above the auto-approve ceiling of "
                    f"USD {auto_approve_ceiling_usd()}; it needs a resolved "
                    f"{HIGH_VALUE_REASON} exception before the pipeline may approve it"
                )


def _human_transition(
    actor: Actor,
    current: ClaimState,
    target: ClaimState,
    amount: Decimal,
    currency: str,
    reviews: tuple[ReviewFact, ...],
) -> None:
    leaving_control = target in RELEASE_STATES or (
        target is ClaimState.REJECTED and current in RELEASE_STATES
    )
    if leaving_control and not actor.can(Scope.CLAIMS_RELEASE):
        _refuse(
            f"moving a claim to {target.value} requires scope {Scope.CLAIMS_RELEASE.value} "
            "(the approver role)"
        )
    if target is ClaimState.APPROVED:
        if not actor.can(Scope.CLAIMS_APPROVE):
            _refuse(f"approving a claim requires scope {Scope.CLAIMS_APPROVE.value}")
        if is_high_value(amount, currency):
            if not actor.can(Scope.CLAIMS_RELEASE):
                _refuse(
                    f"a refund of {amount} {currency} is above the auto-approve ceiling of "
                    f"USD {auto_approve_ceiling_usd()} and is approved by an approver"
                )
            _four_eyes(actor, reviews, "approving a high-value claim")

"""Reading and writing a tenant's filing identity.

The bridge between `tenant_profiles` (a row) and `packager.Claimant` (what prints on a
form). Both shapes already existed; what was missing was the sentence between them, which
is why `POST /packaging/build` has been asking its caller to supply an EIN.

Two rules the rest of the codebase depends on:

**Sufficiency is decided per jurisdiction, at build time.** A tenant filing only in the US
has no CR number and must not be forced to invent one. `TenantProfile.require_for` is
called when a packet is actually being rendered, which is the first moment the answer is
both knowable and actionable.

**An explicit claimant still wins.** A broker filing on behalf of a client, or an analyst
correcting a stale address without waiting for the profile to be updated, passes one in
the request. The profile is the default, not a lock — but the default is what removes the
opportunity to typo an EIN into a filing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from drawbridge_schemas.jurisdiction import Jurisdiction
from drawbridge_schemas.tenant import TenantProfile
from services.api.src.models import TenantProfile as TenantProfileRow
from services.packager.src.packet import Claimant

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_FIELDS = (
    "legal_name",
    "ein",
    "broker_code",
    "cr_number",
    "vat_number",
    "iban",
    "address_line1",
    "address_line2",
    "city",
    "postal_code",
    "country",
    "contact_email",
    "contact_phone",
)


class ProfileError(RuntimeError):
    """The tenant cannot be addressed on a filing, and the message says what is missing."""


def read(session: Session, tenant_id: UUID) -> TenantProfile | None:
    """The stored profile as a validated contract, or None.

    Validated on the way out as well as in. A row written by a migration or by psql has
    only passed the CHECK constraints, and those are shape rather than checksum — an IBAN
    that satisfies the regex and fails mod-97 is exactly the row worth catching before it
    reaches a payment instruction.
    """
    row = session.get(TenantProfileRow, tenant_id)
    if row is None:
        return None
    return TenantProfile(
        tenant_id=row.tenant_id,
        **{name: getattr(row, name) for name in _FIELDS},
    )


def write(session: Session, profile: TenantProfile) -> TenantProfileRow:
    """Upsert, having already validated. Does not commit — the caller owns the transaction.

    The tenant row must exist. Creating one here would let a typo'd uuid silently produce
    a tenant with a filing identity and no claims, which reads as a data-entry success.
    """
    row = session.get(TenantProfileRow, profile.tenant_id)
    if row is None:
        row = TenantProfileRow(tenant_id=profile.tenant_id, legal_name=profile.legal_name)
        session.add(row)
    for name in _FIELDS:
        setattr(row, name, getattr(profile, name))
    return row


def to_claimant(profile: TenantProfile, jurisdiction: Jurisdiction) -> Claimant:
    """The profile as the packager needs it, refusing if it cannot address the packet.

    `require_for` first, so the failure names the missing field rather than printing a
    form with an empty identifier box. A 7551 with no EIN is not a draft; it is a document
    that will be rejected after somebody has signed it.
    """
    profile.require_for(jurisdiction)
    return Claimant(
        name=profile.legal_name,
        identifier=profile.identifier_for(jurisdiction),
        address_line1=profile.address_line1,
        address_line2=profile.address_line2,
        city=profile.city,
        country=profile.country,
        postal_code=profile.postal_code,
        contact_email=profile.contact_email,
        contact_phone=profile.contact_phone,
        broker_identifier=profile.broker_code or "",
    )


def claimant_for(session: Session, tenant_id: UUID, jurisdiction: Jurisdiction) -> Claimant:
    """Resolve the claimant for a packet, or say precisely why it cannot be resolved.

    The two failures are deliberately distinct. "No profile" is an onboarding step nobody
    has taken; "missing ein, city" is a form somebody half-filled. Collapsing them into
    one message would send the reader to the wrong place.
    """
    profile = read(session, tenant_id)
    if profile is None:
        msg = (
            f"tenant {tenant_id} has no filing profile; a packet cannot be addressed "
            f"without one. Pass `claimant` in the request, or create the profile."
        )
        raise ProfileError(msg)
    try:
        return to_claimant(profile, jurisdiction)
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc


def refund_account(session: Session, tenant_id: UUID) -> str:
    """The IBAN a KSA refund settles to, or empty.

    Read separately from the claimant because `PacketRequest` carries it separately, and
    because a US packet must not print one: `refund_account_iban` is a ZATCA field, and a
    bank account on a CBP 7551 is information CBP did not ask for.
    """
    profile = read(session, tenant_id)
    return (profile.iban or "") if profile else ""

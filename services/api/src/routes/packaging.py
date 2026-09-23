"""Packaging endpoint — the end of the automated path.

n8n calls this once a claim reaches `approved`, whether it got there without stopping or
through an analyst. The response carries the rendered artifacts and, more importantly,
whether the packet may actually be transmitted: a packet with an open citation is returned
in full and marked unsendable, because the analyst who has to close the citation needs to
see what is blocked.

Artifacts come back base64-encoded rather than as a multipart download. The caller is a
workflow engine that will store them, not a browser, and one JSON body keeps the manifest
and the bytes together — a manifest that can drift from the artifacts it describes is the
kind of thing that surfaces during an audit.
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from drawbridge_schemas.agents import Scope
from drawbridge_schemas.jurisdiction import Jurisdiction
from services.api.src import gates, packaging, profiles
from services.api.src.auth import require
from services.api.src.models import Claim
from services.api.src.sync_db import in_thread_for_claim
from services.api.src.tenancy import TenantScopeError
from services.packager.src.packet import Claimant

router = APIRouter(prefix="/packaging", tags=["packaging"])


class ClaimantIn(BaseModel):
    """Filing identity, overriding the tenant's stored profile.

    Optional since week 13. Omit it and the claimant is read from `tenant_profiles`, which
    is the path that removes the opportunity to mistype an EIN into a filing. Supply it
    when a broker files for a client, or when an address has changed and the profile has
    not caught up yet.

    Every field is printed on a document addressed to a customs authority, so nothing here
    has a default that could stand in for a real value.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    identifier: str = Field(description="US: IRS/EIN with suffix. KSA: CR number or TIN.")
    address_line1: str
    city: str
    country: str
    postal_code: str = ""
    address_line2: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    broker_identifier: str = ""

    def to_claimant(self) -> Claimant:
        return Claimant(**self.model_dump())


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: UUID
    claimant: ClaimantIn | None = None
    """Omit to use the tenant's filing profile. A 422 names what the profile lacks."""

    manufacturer: ClaimantIn | None = None
    prepared_on: date | None = None
    port_code: str = ""
    refund_account_iban: str = ""
    """Omit to use the profile's IBAN. KSA only; a US packet prints no bank account."""

    notes: str = ""
    include_artifacts: bool = True
    """False returns the manifest alone. A workflow that only needs to know whether the
    packet is transmittable should not move megabytes of PDF to find out."""


#: May print a filing identity the request supplied rather than the tenant's profile. The
#: e2e harness exists only in development (registry `environments`) and drives tenants it
#: has just created; nothing in production may name the claimant of someone else's claim.
_IDENTITY_OVERRIDE_AGENTS = frozenset({"agent:e2e-harness"})


def _authorise_identity_override(what: str) -> None:
    """Who may print a claimant or refund account other than the tenant's own.

    The IBAN on a ZATCA refund request is where the money lands, and the EIN on a 7551 is
    who claims it. Week 13 made both a row rather than a request field (§18.1), and then
    left the request field in place as an override — which n8n forwarded straight from the
    webhook body. An approver may still override, and the ledger records that they did.
    """
    actor = gates.current_actor()
    if actor.kind is gates.ActorKind.LOCAL or actor.agent_id in _IDENTITY_OVERRIDE_AGENTS:
        return
    if actor.kind is gates.ActorKind.MACHINE or not actor.can(Scope.CLAIMS_RELEASE):
        raise gates.GateRefusedError(
            f"overriding the {what} on a filing packet requires a human approver; "
            "the tenant's filing profile is used otherwise"
        )


@router.post("/build", dependencies=[Depends(require(Scope.PACKAGING_BUILD))])
async def build(body: BuildRequest) -> dict[str, Any]:
    """Render the filing packet for an approved claim.

    A claim that is not approved is a 409, not a 422 — the request is well-formed and the
    claim is simply not ready, which is a state n8n can wait on rather than a payload it
    should stop retrying.
    """

    def _work(session: Any) -> dict[str, Any]:
        # Resolved inside the scoped session, so the profile read goes through the same
        # row-level policy as the claim it addresses. Reading it in the route would read
        # it unscoped.
        claim = session.get(Claim, body.claim_id)
        if claim is None:
            msg = f"no claim {body.claim_id}"
            raise packaging.PackagingError(msg)
        jurisdiction = Jurisdiction(claim.jurisdiction)

        source = {"claimant": "profile", "refund_account": "profile"}
        if body.claimant is not None:
            supplied = body.claimant.to_claimant()
            try:
                on_file = profiles.claimant_for(session, claim.tenant_id, jurisdiction)
            except profiles.ProfileError:
                on_file = None
            if supplied != on_file:
                _authorise_identity_override("claimant")
                source["claimant"] = "request"
            claimant = supplied
        else:
            claimant = profiles.claimant_for(session, claim.tenant_id, jurisdiction)

        profile_iban = (
            profiles.refund_account(session, claim.tenant_id)
            if jurisdiction is Jurisdiction.KSA
            else ""
        )
        iban = body.refund_account_iban or profile_iban
        if body.refund_account_iban and body.refund_account_iban != profile_iban:
            _authorise_identity_override("refund account")
            source["refund_account"] = "request"

        packet = packaging.build(
            session,
            claim_id=body.claim_id,
            claimant=claimant,
            prepared_on=body.prepared_on,
            manufacturer=body.manufacturer.to_claimant() if body.manufacturer else None,
            port_code=body.port_code,
            refund_account_iban=iban,
            notes=body.notes,
            identity_source={**source, "by": gates.current_actor().name},
        )
        result: dict[str, Any] = {
            "claim_id": packet.claim_id,
            "jurisdiction": packet.jurisdiction,
            "manifest": packet.manifest(),
            "transmittable": not packet.requires_analyst_review,
            "open_citations": [c.as_dict() for c in packet.open_citations],
        }
        if body.include_artifacts:
            result["artifacts"] = [
                {
                    "filename": artifact.filename,
                    "media_type": artifact.media_type,
                    "bytes": artifact.size,
                    "content_base64": base64.b64encode(artifact.content).decode("ascii"),
                }
                for artifact in packet.artifacts
            ]
        return result

    try:
        return await in_thread_for_claim(body.claim_id, _work)
    except TenantScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "claim_id": str(body.claim_id)},
        ) from exc
    except profiles.ProfileError as exc:
        # 422 and not 409: the claim is fine, the request is under-specified. The caller
        # can fix it by completing the profile or by passing a claimant, and the message
        # names which fields are missing so they do not have to guess.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "incomplete_filing_profile", "detail": str(exc)},
        ) from exc
    except packaging.PackagingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "not_packageable", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        # The packet router refuses an unrouted lane. That is a routing mistake in the
        # caller, not a transient condition, so it must not come back as a retryable 409.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "unroutable_lane", "message": str(exc)},
        ) from exc

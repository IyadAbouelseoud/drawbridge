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

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from services.api.src import packaging
from services.api.src.sync_db import in_thread
from services.packager.src.packet import Claimant

router = APIRouter(prefix="/packaging", tags=["packaging"])


class ClaimantIn(BaseModel):
    """Filing identity, supplied per request until tenant profiles exist.

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
    claimant: ClaimantIn
    manufacturer: ClaimantIn | None = None
    prepared_on: date | None = None
    port_code: str = ""
    refund_account_iban: str = ""
    notes: str = ""
    include_artifacts: bool = True
    """False returns the manifest alone. A workflow that only needs to know whether the
    packet is transmittable should not move megabytes of PDF to find out."""


@router.post("/build")
async def build(body: BuildRequest) -> dict[str, Any]:
    """Render the filing packet for an approved claim.

    A claim that is not approved is a 409, not a 422 — the request is well-formed and the
    claim is simply not ready, which is a state n8n can wait on rather than a payload it
    should stop retrying.
    """

    def _work(session: Any) -> dict[str, Any]:
        packet = packaging.build(
            session,
            claim_id=body.claim_id,
            claimant=body.claimant.to_claimant(),
            prepared_on=body.prepared_on,
            manufacturer=body.manufacturer.to_claimant() if body.manufacturer else None,
            port_code=body.port_code,
            refund_account_iban=body.refund_account_iban,
            notes=body.notes,
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
        return await in_thread(_work)
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

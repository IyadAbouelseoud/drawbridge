"""Jurisdiction and lane routing for the packager.

One entry point, two outputs that have nothing structurally in common: the US lane emits
CBP AcroForm PDFs, the KSA lane emits a ZATCA e-Services JSON payload. Routing on the claim
rather than on a caller-supplied flag means nothing downstream of the matcher has to know
which lane it is on — the same reason the matcher itself is two strategies behind one
interface (`docs/ARCHITECTURE.md` §3.5).

Lane matters too, as of week 7. Drawback routes on jurisdiction alone, but the two US
non-drawback lanes — post summary correction and §1520(d) — need input a generic
`PacketRequest` does not carry, so the router refuses them with a pointer instead of
rendering a 7551 for a claim that is not a drawback claim.
"""

from __future__ import annotations

from drawbridge_schemas.jurisdiction import Jurisdiction
from services.packager.src.cbp_forms import build_us_packet
from services.packager.src.packet import FilingPacket, PacketRequest
from services.packager.src.us_alternate import LANES as ALTERNATE_LANES
from services.packager.src.zatca_payload import build_ksa_packet

_BUILDERS = {
    Jurisdiction.US: build_us_packet,
    Jurisdiction.KSA: build_ksa_packet,
}


def build_packet(request: PacketRequest) -> FilingPacket:
    """Render the filing packet for a claim's jurisdiction.

    Raises on an unrouted jurisdiction rather than falling back to a default. A packet
    rendered under the wrong lane would be a coherent-looking document filed against the
    wrong statute, which is the most expensive failure this system can have.
    """
    lane = request.lane
    if lane in ALTERNATE_LANES:
        # PSC and §1520(d) need arguments a generic PacketRequest does not carry — the
        # entry being corrected, the agreement claimed. Rendering them through the
        # drawback builder would produce a coherent 7551 for a claim that is not drawback,
        # so the router directs rather than guesses.
        msg = (
            f"lane {lane} is rendered by services.packager.src.us_alternate."
            f"{ALTERNATE_LANES[lane]}, which needs lane-specific input this router "
            "cannot supply"
        )
        raise ValueError(msg)

    builder = _BUILDERS.get(request.jurisdiction)
    if builder is None:
        msg = (
            f"no packager for jurisdiction {request.jurisdiction}; "
            f"routed lanes are {sorted(_BUILDERS)}"
        )
        raise ValueError(msg)
    return builder(request)

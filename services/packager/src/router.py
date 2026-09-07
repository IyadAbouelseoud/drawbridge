"""Jurisdiction routing for the packager.

One entry point, two outputs that have nothing structurally in common: the US lane emits
CBP AcroForm PDFs, the KSA lane emits a ZATCA e-Services JSON payload. Routing on the claim
rather than on a caller-supplied flag means nothing downstream of the matcher has to know
which lane it is on — the same reason the matcher itself is two strategies behind one
interface (`docs/ARCHITECTURE.md` §3.5).
"""

from __future__ import annotations

from drawbridge_schemas.jurisdiction import Jurisdiction
from services.packager.src.cbp_forms import build_us_packet
from services.packager.src.packet import FilingPacket, PacketRequest
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
    builder = _BUILDERS.get(request.jurisdiction)
    if builder is None:
        msg = (
            f"no packager for jurisdiction {request.jurisdiction}; "
            f"routed lanes are {sorted(_BUILDERS)}"
        )
        raise ValueError(msg)
    return builder(request)

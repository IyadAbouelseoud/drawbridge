"""Shared data contracts for Drawbridge.

Single source of truth. Every service and MCP server imports its types from here;
nothing redefines a claim, an entry line, or a provenance span locally.
"""

from drawbridge_schemas.claim import (
    Claim,
    ClaimState,
    DrawbackType,
    RecoveryLane,
    RefundLine,
)
from drawbridge_schemas.provenance import Confidence, DocumentRef, Provenance, Span
from drawbridge_schemas.trade import (
    EntryLine,
    ExportLine,
    HTSCode,
    LineMatch,
    Money,
)

__all__ = [
    "Claim",
    "ClaimState",
    "Confidence",
    "DocumentRef",
    "DrawbackType",
    "EntryLine",
    "ExportLine",
    "HTSCode",
    "LineMatch",
    "Money",
    "Provenance",
    "RecoveryLane",
    "RefundLine",
    "Span",
]

__version__ = "0.1.0"

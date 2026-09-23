"""Shared data contracts for Drawbridge.

Single source of truth. Every service and MCP server imports its types from here;
nothing redefines a claim, an entry line, or a provenance span locally.
"""

from drawbridge_schemas.agents import (
    AGENTS,
    HUMAN_ONLY_SCOPES,
    ROLE_SCOPES,
    AgentIdentity,
    AgentKind,
    OwnerRole,
    Role,
    Scope,
)
from drawbridge_schemas.bom import (
    BillOfMaterials,
    BomComponent,
    ManufacturingBasis,
)
from drawbridge_schemas.claim import (
    Claim,
    ClaimState,
    DrawbackType,
    RecoveryLane,
    RefundLine,
)
from drawbridge_schemas.jurisdiction import (
    GCC_MIN_REEXPORT_VALUE_USD,
    KSA_PROFILE,
    US_PROFILE,
    ClockAnchor,
    Currency,
    Deadline,
    DeadlineUnit,
    Jurisdiction,
    JurisdictionProfile,
    MatchTheory,
    profile_for,
)
from drawbridge_schemas.provenance import (
    Confidence,
    DocumentKind,
    DocumentRef,
    Language,
    Provenance,
    Span,
)
from drawbridge_schemas.tenant import TenantProfile, iban_checksum_ok
from drawbridge_schemas.trade import (
    EntryLine,
    ExportLine,
    HTSCode,
    LineMatch,
    Money,
    ValuationBasis,
)

__all__ = [
    "AGENTS",
    "GCC_MIN_REEXPORT_VALUE_USD",
    "HUMAN_ONLY_SCOPES",
    "KSA_PROFILE",
    "ROLE_SCOPES",
    "US_PROFILE",
    "AgentIdentity",
    "AgentKind",
    "BillOfMaterials",
    "BomComponent",
    "Claim",
    "ClaimState",
    "ClockAnchor",
    "Confidence",
    "Currency",
    "Deadline",
    "DeadlineUnit",
    "DocumentKind",
    "DocumentRef",
    "DrawbackType",
    "EntryLine",
    "ExportLine",
    "HTSCode",
    "Jurisdiction",
    "JurisdictionProfile",
    "Language",
    "LineMatch",
    "ManufacturingBasis",
    "MatchTheory",
    "Money",
    "OwnerRole",
    "Provenance",
    "RecoveryLane",
    "RefundLine",
    "Role",
    "Scope",
    "Span",
    "TenantProfile",
    "ValuationBasis",
    "iban_checksum_ok",
    "profile_for",
]

__version__ = "0.4.0"

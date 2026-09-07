"""Legal citations on a filing packet, and what happens when one is not obtainable.

The system's governing rule is that the LLM never originates a number. Week 6 extends it:
**the packager never originates an article number either.**

Two classes of citation appear on a KSA packet:

- **GCC Common Customs Law and its Rules of Implementation** — transcribed from the GCC
  Secretariat's own publication, authoritative, emitted verbatim.
- **ZATCA Resolution 28624** — the Saudi *procedural* implementation. Its article numbers
  are not obtainable from any published source; every route 404s, 500s or resets
  (`docs/COMPLIANCE-GCC.md` §8.4). These are emitted as `ANALYST_REVIEW` placeholders.

A wrong article number on a refund request is worse than a missing one. A missing number
invites a request for information; a confidently wrong number is a misstatement to the
authority. So the placeholder is a first-class value carrying the reason it is open — not
an empty string, which reads as an oversight, and not a guess, which reads as a fact.

Closing this needs no code change beyond this module: when the Arabic text is obtained, the
article numbers go into `ZATCA_PROCEDURAL` and every placeholder resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# The reason string carried on every unresolved ZATCA procedural citation. Verbatim on the
# packet, so whoever clears it sees why it is open rather than an unexplained blank.
UNOBTAINABLE_REASON = (
    "ZATCA Resolution 28624 article number not obtainable from any published source "
    "(ZATCA PDF 404, Umm Al-Qura 500, istitlaa reset); see COMPLIANCE-GCC.md §8.4"
)

ZATCA_28624_AUTHORITY = "ZATCA Administrative Decision 28624 (23/05/1445 AH)"


class CitationStatus(StrEnum):
    """Whether a citation may be filed as it stands."""

    VERIFIED = "verified"
    """Traced to a primary source held on file. Files as-is."""

    ANALYST_REVIEW = "analyst_review"
    """The authority is known; the article number is not. Blocks transmission."""


@dataclass(frozen=True, slots=True)
class Citation:
    """One legal reference on a packet.

    `article` is `None` exactly when `status` is `ANALYST_REVIEW`. The two are kept
    separate rather than collapsed into a nullable article, so a consumer that forgets to
    check for `None` still sees the status.
    """

    authority: str
    article: str | None
    proposition: str
    status: CitationStatus = CitationStatus.VERIFIED
    reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status is CitationStatus.ANALYST_REVIEW

    def render(self) -> str:
        """One-line form for a PDF field or a narrative.

        An open citation renders as an explicit placeholder rather than as the authority
        alone: a reader skimming the packet must not mistake a half-cited provision for a
        complete one.
        """
        if self.is_open:
            return f"{self.authority}, art. [ANALYST_REVIEW] — {self.proposition}"
        return f"{self.authority}, {self.article} — {self.proposition}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "article": self.article,
            "proposition": self.proposition,
            "status": self.status,
            "reason": self.reason,
        }


def analyst_review(proposition: str, *, authority: str = ZATCA_28624_AUTHORITY) -> Citation:
    """A citation whose authority is known and whose article number is not."""
    return Citation(
        authority=authority,
        article=None,
        proposition=proposition,
        status=CitationStatus.ANALYST_REVIEW,
        reason=UNOBTAINABLE_REASON,
    )


# ------------------------------------------------------------------- verified citations

GCC_ART_97 = Citation(
    authority="GCC Common Customs Law",
    article="Article 97",
    proposition=(
        "Customs duties collected on foreign goods are wholly or partially refunded on "
        "re-exportation, per the Rules of Implementation"
    ),
)

GCC_IMPL_ART_15C = Citation(
    authority="GCC Rules of Implementation",
    article="Article 15(c)",
    proposition="The import declaration number must be affixed to the re-export declaration",
)

GCC_IMPL_ART_16_2 = Citation(
    authority="GCC Rules of Implementation",
    article="Article 16 §2",
    proposition="Value of the re-exported goods must not be less than USD 5,000",
)

GCC_IMPL_ART_16_3A = Citation(
    authority="GCC Rules of Implementation",
    article="Article 16 §3(a)",
    proposition="Re-export within one Gregorian year of the date of duty payment",
)

GCC_IMPL_ART_16_3B = Citation(
    authority="GCC Rules of Implementation",
    article="Article 16 §3(b)",
    proposition="Claim filed within six Gregorian months of the date of re-export",
)

GCC_IMPL_ART_16_4 = Citation(
    authority="GCC Rules of Implementation",
    article="Article 16 §4",
    proposition=(
        "Single consignment; part shipments permitted where proven to belong to the same "
        "consignment"
    ),
)

GCC_IMPL_ART_16_5 = Citation(
    authority="GCC Rules of Implementation",
    article="Article 16 §5",
    proposition="Goods not locally used after import and in the same condition as imported",
)

GCC_IMPL_ART_16_6 = Citation(
    authority="GCC Rules of Implementation",
    article="Article 16 §6",
    proposition="Refund limited to the customs duties actually paid",
)

GCC_ART_28 = Citation(
    authority="GCC Common Customs Law",
    article="Article 28",
    proposition=(
        "Value of exported goods is the declared value plus all costs until arrival at "
        "the customs office"
    ),
)

GCC_ART_174 = Citation(
    authority="GCC Common Customs Law",
    article="Article 174",
    proposition="No refund claim accepted for duties paid more than three years earlier",
)

US_1313J = Citation(
    authority="19 U.S.C.",
    article="§1313(j)",
    proposition="Unused merchandise drawback, including 8-digit HTS substitution under TFTEA",
)

US_1313A = Citation(
    authority="19 U.S.C.",
    article="§1313(a)",
    proposition="Manufacturing drawback on the same imported merchandise used in manufacture",
)

US_1313B = Citation(
    authority="19 U.S.C.",
    article="§1313(b)",
    proposition="Substitution manufacturing drawback under the 8-digit subheading",
)

US_1313R = Citation(
    authority="19 U.S.C.",
    article="§1313(r)",
    proposition="Drawback claim filed within three years of the date of exportation",
)

US_190_SUBPART_B = Citation(
    authority="19 CFR Part 190",
    article="subpart B",
    proposition="Relative value apportionment where one process yields several products",
)


# ---------------------------------------------------- ZATCA procedural — all unresolved

ZATCA_PROCEDURAL: dict[str, Citation] = {
    "refund_request": analyst_review(
        "Procedure for submitting a customs duty refund request through ZATCA e-Services"
    ),
    "reexport_bayan_link": analyst_review(
        "Requirement to create a re-export Bayan linked to the original import Bayan"
    ),
    "goods_identification": analyst_review(
        "Identification of imported goods on re-export, including photographic record and "
        "descriptive literature"
    ),
    "supporting_documents": analyst_review(
        "Supporting documents required to accompany a refund request"
    ),
    "refund_settlement": analyst_review(
        "Settlement of an approved refund and the account it is paid to"
    ),
}
"""Every ZATCA procedural citation the packager can emit, all open.

Keyed by what the citation is *for*, not by an article number, precisely because the
article numbers are what is missing. When the Arabic text arrives, each value becomes a
verified `Citation` and nothing else in the codebase changes.
"""


def zatca_procedural(key: str) -> Citation:
    """The procedural citation for one step of a KSA filing.

    Raises on an unknown key rather than fabricating a placeholder: an unrecognised step
    means the caller is citing something the packet does not model, and inventing an open
    citation for it would hide that.
    """
    try:
        return ZATCA_PROCEDURAL[key]
    except KeyError:
        msg = (
            f"unknown ZATCA procedural citation {key!r}; known keys are {sorted(ZATCA_PROCEDURAL)}"
        )
        raise KeyError(msg) from None


def open_citations(citations: list[Citation]) -> list[Citation]:
    """Those blocking transmission. An empty list is what clears a packet to file."""
    return [citation for citation in citations if citation.is_open]

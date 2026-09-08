"""Who prepared the packet, and what that party is allowed to say about itself.

A broker running Drawbridge on their own network is not deploying a copy of our product
with our name on it. They are preparing filings for their own clients, under their own
licence, and the document that goes to CBP has to say so. That makes white-labelling a
compliance surface rather than a cosmetic one, which is why it is a module and not a
template variable.

**The preparer notice is a representation to a customs authority.** Every 7551 and 7552
this repository has produced since week 4 carries the same paragraph: *"Prepared by
Drawbridge for filing by a licensed customs broker. Drawbridge is not a customs broker and
does not transmit to CBP."* Both halves of that are true of us and load-bearing — §5 of the
architecture turns on it. Neither half is true of a licensed broker preparing their own
client's claim, and printing it on their form would be a false statement about who prepared
the document and a false disclaimer about their own licence.

So the notice is **composed**, not substituted. `is_licensed_broker` selects which second
sentence is true, and a broker who sets it also has to supply the filer code that makes the
claim checkable. A deployment that renamed the preparer and kept our disclaimer would be
worse than one that changed nothing, because it would read as deliberate.

**What is not brandable.** The certifications, the statutory citations, the form titles,
and the sentence stating that the document is a transcription rather than a CBP-issued
form. Those are the authority's words or facts about the document, and a deployment that
could edit them could quietly weaken a declaration somebody signs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _sentence(text: str) -> str:
    """End a clause with exactly one full stop.

    A broker's legal name very often ends in one — "Harborline Customs Brokers, Inc." —
    and appending another produces "Inc.." on a document filed with a federal agency.
    """
    stripped = text.rstrip()
    return stripped if stripped.endswith((".", "!", "?")) else f"{stripped}."


#: A CBP filer code: three alphanumerics, assigned to a licensed broker. Same shape as
#: `TenantProfile.broker_code`, checked separately because these two identify different
#: parties — the tenant's own broker, and the party operating this deployment.
_FILER_CODE = re.compile(r"^[A-Z0-9]{3}$")

#: The line every US packet carries regardless of who prepared it. Not brandable: it is a
#: fact about the document, and a deployment that could remove it could present a
#: transcription as the authority's own form.
TRANSCRIPTION_NOTICE = "transcription for filing, not a CBP-issued form"


@dataclass(frozen=True, slots=True)
class Preparer:
    """The party whose name goes on the packet.

    Defaults to us, unbranded, and not a broker — which is the true description of the
    hosted deployment and the only safe default for one that has not been configured.
    """

    name: str = "Drawbridge"
    is_licensed_broker: bool = False
    filer_code: str = ""
    contact: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "a packet must name its preparer; set DRAWBRIDGE_PREPARER_NAME"
            raise ValueError(msg)
        if self.is_licensed_broker and not _FILER_CODE.match(self.filer_code.upper()):
            # A deployment claiming a licence must say which one. Without the filer code
            # the claim is unverifiable by the party reading the form, and an
            # unverifiable claim of licensure on a customs filing is the specific thing
            # this check exists to prevent.
            msg = (
                f"preparer {self.name!r} claims to be a licensed customs broker but "
                f"supplies no valid three-character filer code (got {self.filer_code!r})"
            )
            raise ValueError(msg)

    @property
    def notice(self) -> str:
        """The preparer paragraph, composed from what is actually true of this deployer.

        Three sentences: who prepared it, what they may do with it, and where the figures
        came from. The middle one is the only one that varies, because it is the only one
        that is a claim about the preparer rather than about the document.
        """
        who = f"Prepared by {self.name.strip()}"
        if self.is_licensed_broker:
            capacity = (
                f"{self.name.strip()} is a licensed customs broker (filer code "
                f"{self.filer_code.upper()}) and transmits this claim under a Power of "
                f"Attorney held for the claimant named above."
            )
        else:
            who = f"{who} for filing by a licensed customs broker"
            capacity = f"{self.name.strip()} is not a customs broker and does not transmit to CBP."
        provenance = (
            "Every figure below traces to a source-document span retained under 19 CFR "
            "163; the supporting schedule accompanies this form."
        )
        parts = [_sentence(who), capacity, provenance]
        if self.contact.strip():
            parts.append(f"Questions: {self.contact.strip()}.")
        return " ".join(parts)

    @property
    def footer(self) -> str:
        """A short attribution for the document subtitle line."""
        return self.name.strip()


#: What every packet gets when nothing has been configured. Module-level and frozen so the
#: renderers can default to it without importing settings — the packager renders and does
#: not read configuration, which is the rule that makes a packet reproducible from stored
#: input alone.
DEFAULT_PREPARER = Preparer()

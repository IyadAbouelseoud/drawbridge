"""The numeric guard: no figure in a generated memo that the facts did not supply.

`CLAUDE.md` states the rule — *the LLM writes narratives and judgment calls; it never
originates a number*. A prompt instruction is not enforcement. This module is.

Every memo is scanned after generation. Each numeric token in the prose must be present
in the fact set the model was given, or in the statutory constants that appear in the
citations it was allowed to use. A token that is in neither is an invented figure, and
the memo is rejected rather than repaired: a memo with one hallucinated quantity is not
90% usable, it is evidence the generation cannot be trusted on this input.

**Why token matching rather than asking the model to be careful.** The failure this
catches is not the model lying. It is the model being *fluent*: rendering 4,812.50 as
"approximately 4,800 units", or restating a duty figure with a transposed digit. Both
read perfectly. Neither survives a comparison against the source.

**What is deliberately allowed.** Statutory numbers (1313, 190, 8-digit, 10-digit, the
three-year bar), because they are part of the legal vocabulary and are seeded from the
citations rather than invented. Ordinals and small counts under `_SMALL_INTEGER_CEILING`,
because "the first of two conditions" is prose, not a figure. Everything else must match.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# Numbers below this are prose, not figures: "both of the two tests", "a third factor".
# A real customs quantity or duty amount is essentially never in this range, and treating
# small integers as figures would reject fluent English for no safety gain.
_SMALL_INTEGER_CEILING = 12

# Digits attached to a word (HS codes inside "subheading 8471.30", part number "M12") are
# matched as whole tokens including their separators, so 8471.30 does not decompose into
# 8471 and 30 and quietly pass because 30 happened to appear elsewhere.
_NUMERIC = re.compile(r"\d[\d,.\u066b\u066c/:%-]*\d|\d")

# Arabic-Indic digits appear in ZATCA source text and must fold to their ASCII form
# before comparison, or an Arabic-sourced figure would never match its own fact.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class UngroundedFigureError(ValueError):
    """Generated text contained a number the facts do not support.

    Carries every offending token rather than the first, so a rejected generation can be
    diagnosed in one look instead of one round trip per token.
    """

    def __init__(self, field: str, tokens: Iterable[str]) -> None:
        self.field = field
        self.tokens = sorted(set(tokens))
        joined = ", ".join(repr(token) for token in self.tokens)
        super().__init__(
            f"{field} contains {len(self.tokens)} figure(s) absent from the supplied "
            f"facts: {joined}. The narrative may not originate a number."
        )


def _fold(token: str) -> str:
    """Reduce a numeric token to the form two spellings of the same figure share.

    `4,812.50`, `4812.5` and `٤٨١٢.٥٠` are one figure written three ways. Comparing them
    literally would reject a correct restatement; comparing them folded accepts the
    restatement and still rejects a transposition.
    """
    cleaned = token.translate(_ARABIC_DIGITS)
    cleaned = cleaned.replace("\u066c", "").replace(",", "").replace("\u066b", ".")
    cleaned = cleaned.strip("%-/: .")
    if not cleaned:
        return ""
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return cleaned
    normalised = value.normalize()
    # normalize() renders 4812.50 as 4.8125E+3; unwind the exponent so the string form is
    # comparable rather than merely equal-as-Decimal.
    return f"{normalised:f}"


def _tokens(text: str) -> Iterator[str]:
    for match in _NUMERIC.finditer(text):
        folded = _fold(match.group())
        if folded:
            yield folded


def _is_prose_number(folded: str) -> bool:
    try:
        value = Decimal(folded)
    except InvalidOperation:
        return False
    return value == value.to_integral_value() and 0 <= value <= _SMALL_INTEGER_CEILING


def allowed_figures(facts: Any) -> set[str]:
    """Every number the model is permitted to write, harvested from what it was given.

    Walks the fact structure rather than taking a curated list, because a curated list
    drifts: a new field added to the prompt payload would be visible to the model and
    invisible to the guard, and the guard would start rejecting correct memos.
    """
    allowed: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                allowed.update(_tokens(str(key)))
                walk(value)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)
        elif node is not None and not isinstance(node, bool):
            allowed.update(_tokens(str(node)))

    walk(facts)
    return allowed


def check(text: str, allowed: set[str], *, field: str) -> None:
    """Raise if `text` contains a figure outside `allowed`."""
    offending = [
        token for token in _tokens(text) if token not in allowed and not _is_prose_number(token)
    ]
    if offending:
        raise UngroundedFigureError(field, offending)


# Not scanned numerically. A citation is a *name*, and its digits — the 19 in 19 CFR, the
# section number, the ruling's serial — are part of that name rather than figures the memo
# asserts. Scanning them here would reject "19 CFR §163.1" as three invented numbers, which
# is the guard failing closed on correct output: the way guards get disabled.
#
# Citations are not therefore unchecked. They are checked *harder*, by membership: each
# drafter verifies every citation against the closed set it supplied, so an invented
# authority is caught as an invented authority rather than as a stray digit.
_NOT_FIGURES: frozenset[str] = frozenset({"citations"})


def check_model(memo: Any, allowed: set[str]) -> None:
    """Scan every prose field of a generated memo.

    Lists are walked too: a `checks` bullet is as capable of carrying an invented duty
    figure as a paragraph is, and is likelier to be skimmed.

    Statutory numbers in the *prose* are still checked, and pass because the citation set
    the drafter was given is part of the facts — so "under 19 U.S.C. §1313(j)(2)" is
    grounded exactly when that authority was actually supplied.
    """
    for field, value in memo.model_dump().items():
        if field in _NOT_FIGURES:
            continue
        if isinstance(value, str):
            check(value, allowed, field=field)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    check(item, allowed, field=f"{field}[{index}]")


def check_citations(cited: Any, facts: Any) -> None:
    """Refuse a citation the facts did not offer.

    A fabricated ruling number or article number in an audit-facing memo is the
    highest-cost error this system can make: it is confident, specific, checkable, and
    wrong — and it appears in a document handed to an agency.

    Shared by both drafters rather than implemented per-drafter, because the cost is the
    same in an exception memo as in a filing narrative and a per-drafter copy is one
    someone forgets to write.
    """
    available = {str(c) for c in facts.get("available_citations", ())}
    available |= {
        str(ruling.get("ruling_number"))
        for ruling in facts.get("supporting_rulings", ())
        if isinstance(ruling, dict)
    }
    available |= {
        str(ruling.get("ruling_number"))
        for ruling in facts.get("superseded_rulings", ())
        if isinstance(ruling, dict)
    }
    invented = [c for c in cited if c not in available]
    if invented:
        msg = (
            f"memo cites {invented!r}, which was not in the supplied citation set. "
            "The agent may not originate an authority."
        )
        raise ValueError(msg)

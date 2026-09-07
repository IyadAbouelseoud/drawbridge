"""Jurisdiction-driven strategy selection.

The one place that branches on jurisdiction. Everything downstream of the matcher
consumes `MatchResult` and never asks which statute produced it.
"""

from __future__ import annotations

from functools import cache

from drawbridge_schemas.jurisdiction import Jurisdiction, profile_for
from services.matcher.src.base import MatchRequest, MatchResult, MatchStrategy
from services.matcher.src.gcc_linkage import GccLinkageMatcher
from services.matcher.src.us_substitution import UsSubstitutionMatcher


@cache
def strategy_for(jurisdiction: Jurisdiction) -> MatchStrategy:
    """The matcher for a jurisdiction.

    Strategies are stateless, so one instance per jurisdiction is enough.
    """
    match jurisdiction:
        case Jurisdiction.US:
            return UsSubstitutionMatcher()
        case Jurisdiction.KSA:
            return GccLinkageMatcher()


def run_match(request: MatchRequest) -> MatchResult:
    """Route a request to its jurisdiction's matcher."""
    return strategy_for(request.profile.jurisdiction).match(request)


def profile_and_strategy(
    jurisdiction: Jurisdiction,
) -> tuple[object, MatchStrategy]:
    """Profile and strategy together, for callers building a request from scratch."""
    return profile_for(jurisdiction), strategy_for(jurisdiction)

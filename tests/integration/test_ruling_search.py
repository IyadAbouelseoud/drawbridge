"""Ruling search against bodies the length CROSS actually publishes.

`search_rulings` returned nothing for every query for eleven weeks, and no test noticed,
because the only rulings in the fixture corpus had two-sentence bodies. `similarity()` is
set-symmetric — shared trigrams over the union of both sides — so a four-word query
against a four-thousand-word ruling is dominated by the denominator. Loading 114 real
CROSS rulings put the best score any query achieved at 0.127, under a `LEXICAL_FLOOR` of
0.15.

The lesson is the length, not the query. Every ruling written here carries a body of
realistic size, because a short one reproduces the conditions under which the bug was
invisible.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.classifier.src.search import RULING_BODY_CHARS, search_rulings
from services.ingest.src.cross import SOURCE
from services.ingest.src.tariff import RulingRecord, load_rulings

pytest_plugins = ("tests.integration.conftest",)

#: Filler shaped like a ruling and matching nothing. CROSS rulings recite the same
#: procedural boilerplate at length before reaching the goods, which is precisely what
#: drowns a short query in a whole-document similarity score.
_BOILERPLATE = (
    "This ruling letter is issued under the provisions of Part 177 of the Customs "
    "Regulations (19 C.F.R. 177). A copy of this ruling letter should be attached to the "
    "entry documents filed at the time this merchandise is imported. If the documents "
    "have been filed without a copy, this ruling should be brought to the attention of "
    "the Customs officer handling the transaction. "
) * 12


def _ruling(number: str, code: str, subject: str, goods: str) -> RulingRecord:
    return RulingRecord(
        jurisdiction="us",
        source=SOURCE,
        ruling_number=number,
        ruling_date=date(2019, 4, 2),
        classified_code=code,
        subject=subject,
        body=f"FACTS:\n\n{goods}\n\n{_BOILERPLATE}"[: RULING_BODY_CHARS * 2],
        url=f"https://rulings.cbp.gov/ruling/{number}",
    )


CORPUS = (
    _ruling(
        "N999001",
        "8471300100",
        "The tariff classification of a ruggedised field laptop from China",
        "The merchandise is a portable automatic data processing machine weighing 2.4 kg, "
        "incorporating a central processing unit, a keyboard and a display.",
    ),
    _ruling(
        "N999002",
        "0901210030",
        "The tariff classification of roasted coffee from Tanzania",
        "The product is arabica coffee, roasted and not decaffeinated, packed for retail "
        "sale in valved foil bags of 340 grams.",
    ),
    _ruling(
        "H999003",
        "6404110000",
        "Application for further review of protest number 1601-19-100044",
        "The articles at issue are women's athletic shoes with textile uppers and rubber "
        "outer soles, of the slip-on type.",
    ),
)


@pytest.fixture
def rulings(session: Session) -> Iterator[None]:
    """Load the corpus, and take it out again.

    `tariff_rulings` is a shared table — `tenancy.SHARED_TABLES` — so these rows are
    visible to every other test in the run until they are removed.
    """
    load_rulings(session, CORPUS, source=SOURCE)
    session.commit()
    yield
    session.execute(
        text("DELETE FROM tariff_rulings WHERE ruling_number = ANY(:numbers)"),
        {"numbers": [r.ruling_number for r in CORPUS]},
    )
    session.commit()


@pytest.mark.usefixtures("rulings")
class TestFindingARulingAtRealisticLength:
    def test_a_goods_query_reaches_its_ruling(self, session: Session) -> None:
        """The regression. Before the scorer changed this returned zero rows, for every
        query, against any corpus whose bodies were the length CROSS publishes.

        Membership rather than rank, and not because rank is unimportant: `tariff_rulings`
        is shared and a developer who has run `make cross-ingest` has a hundred real
        rulings in the same table. A test that asserted rank 1 would pass on an empty
        corpus and fail on a realistic one, which is the wrong way round.
        """
        hits = search_rulings(
            session, query="portable laptop computer", jurisdiction="us", limit=10
        )
        assert "N999001" in {h["ruling_number"] for h in hits}

    def test_the_code_filter_narrows_to_the_heading(self, session: Session) -> None:
        """Every hit shares the subheading asked for, and the fixture ruling is among
        them. `_RULING_SQL` matches on the full code or on hs6, so a sibling ten-digit
        line under 0901.21 is a hit and a coffee ruling under 0901.11 is not."""
        hits = search_rulings(
            session, query="roasted coffee beans", jurisdiction="us", code="0901210030"
        )
        assert "N999002" in {h["ruling_number"] for h in hits}
        assert all(h["classified_code"].startswith("090121") for h in hits)

    def test_a_procedural_subject_is_still_found_through_its_body(self, session: Session) -> None:
        """H999003 is titled "Application for further review of protest number ...". The
        goods appear only in the text, which is why the body is scored at all rather than
        the subject alone."""
        hits = search_rulings(
            session, query="women's athletic shoes with textile uppers", jurisdiction="us"
        )
        assert "H999003" in {h["ruling_number"] for h in hits}

    def test_the_whole_document_similarity_that_was_used_before_scores_below_the_floor(
        self, session: Session
    ) -> None:
        """The bug, pinned as an assertion rather than as a comment.

        `similarity(subject || ' ' || body, :q)` on these bodies cannot reach
        `LEXICAL_FLOOR`, so the old query filtered out its own correct answer. If a future
        change reverts to it, this fails and says why.
        """
        best = session.execute(
            text(
                "SELECT max(similarity(subject || ' ' || body, :q)) FROM tariff_rulings "
                "WHERE ruling_number = ANY(:numbers)"
            ),
            {
                "q": "portable laptop computer",
                "numbers": [r.ruling_number for r in CORPUS],
            },
        ).scalar_one()
        assert float(best) < 0.15

    def test_a_query_that_is_not_a_good_does_not_reach_the_top(self, session: Session) -> None:
        """Not "returns nothing" — it does not. `LEXICAL_FLOOR` is left where it is
        because paraphrased goods queries and non-goods queries overlap at this corpus
        size, and `find_rulings` returns candidates for a person to read. What must hold
        is that a service, not a good, does not outrank an actual match."""
        hits = search_rulings(
            session, query="marine cargo insurance brokerage commission", jurisdiction="us"
        )
        assert "N999001" not in {h["ruling_number"] for h in hits[:1]}

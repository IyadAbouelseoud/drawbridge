"""The analyst tools as the MCP layer actually exposes them.

The previous module tests the service functions directly. This one goes through the tool
wrappers, because that layer has its own behaviour worth pinning: refusals come back as
data rather than exceptions, UUIDs arrive as strings, and the resume notification is
attempted only when nothing else blocks the claim.

The tools open their own sessions via `session_scope`, so it is pointed at the same
database the fixtures write to and their writes are committed rather than rolled back —
these tests clean up after themselves instead.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from mcp_servers.mcp_claims import db as claims_db
from services.api.src.models import Claim, ReviewQueue, Tenant
from tests.integration.conftest import TEST_DSN

pytest_plugins = ("tests.integration.conftest",)


@pytest.fixture(autouse=True)
def _point_tools_at_the_test_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make session_scope resolve to the test DSN.

    The caches are cleared on the way in and out so a stale engine from another test
    module cannot leak across.
    """
    monkeypatch.setenv("DRAWBRIDGE_DATABASE_URL", TEST_DSN)
    claims_db.engine.cache_clear()
    claims_db._session_factory.cache_clear()
    yield
    claims_db.engine.cache_clear()
    claims_db._session_factory.cache_clear()


@pytest.fixture
def committed_claim(engine) -> Iterator[tuple[UUID, UUID, UUID]]:
    """A tenant, claim and open exception that really exist in the database.

    Committed rather than rolled back because the MCP tools open their own connections
    and would not see an uncommitted outer transaction.
    """
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(engine, expire_on_commit=False)
    tenant_id, claim_id, review_id = uuid4(), uuid4(), uuid4()

    with factory() as session:
        session.add(Tenant(tenant_id=tenant_id, name="MCP Test Tenant", default_jurisdiction="ksa"))
        # Flushed before the claim so the foreign key resolves; SQLAlchemy does not
        # order inserts across unrelated mappers on its own.
        session.flush()
        session.add(
            Claim(
                claim_id=claim_id,
                tenant_id=tenant_id,
                state="analyst_review",
                jurisdiction="ksa",
                currency="SAR",
                lane="gcc_reexport_drawback",
                drawback_type="gcc_unused_reexport",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                filing_deadline=date(2025, 3, 12),
                total_refund=Decimal("19531.25"),
            )
        )
        session.add(
            ReviewQueue(
                review_id=review_id,
                tenant_id=tenant_id,
                claim_id=claim_id,
                reason="threshold_near_miss",
                severity="normal",
                summary="18,000 SAR = 4,800 USD; short by 200.00 USD",
                citation="GCC Rules of Implementation Art. 16 §2",
                payload={"details": ["near_miss"]},
                resume_token=uuid4().hex,
                # No workflow run: the resume path is exercised separately, and a live
                # n8n call has no place in a test that is about the tool contract.
                workflow_run_id=None,
                state="open",
            )
        )
        session.commit()

    yield tenant_id, claim_id, review_id

    with factory() as session:
        session.execute(text("DELETE FROM review_queue WHERE tenant_id = :t"), {"t": tenant_id})
        session.execute(text("DELETE FROM claim_transitions WHERE claim_id = :c"), {"c": claim_id})
        session.execute(text("DELETE FROM claims WHERE claim_id = :c"), {"c": claim_id})
        # The ledger refuses deletion, which is the point of it, so teardown has to say so
        # out loud: disable the guards, remove this test's rows, put them back. Only the
        # table's owner can do this, and no application role is the owner — a test tearing
        # down its own fixtures is the one setting where emptying an audit trail is
        # legitimate, and it should look as deliberate as it is.
        session.execute(text("ALTER TABLE audit_ledger DISABLE TRIGGER USER"))
        session.execute(text("DELETE FROM audit_ledger WHERE tenant_id = :t"), {"t": tenant_id})
        session.execute(text("ALTER TABLE audit_ledger ENABLE TRIGGER USER"))
        session.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant_id})
        session.commit()


def _tools():
    """Import the tool functions after the environment is pointed at the test DSN."""
    from mcp_servers.mcp_claims import server as claims

    return claims


class TestMcpToolContract:
    def test_queue_lists_the_exception(self, committed_claim) -> None:
        tenant_id, _, review_id = committed_claim
        result = _tools().list_review_queue(tenant_id=str(tenant_id))

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["items"][0]["review_id"] == str(review_id)

    def test_inspect_returns_the_payload(self, committed_claim) -> None:
        _, _, review_id = committed_claim
        result = _tools().inspect_exception(review_id=str(review_id))

        assert result["ok"] is True
        assert result["exception"]["payload"]["details"] == ["near_miss"]

    def test_refusals_come_back_as_data_not_exceptions(self) -> None:
        """An analyst in Claude Code should see the reason, not a stack trace."""
        result = _tools().inspect_exception(review_id="not-a-uuid")
        assert result["ok"] is False
        assert "not a UUID" in result["detail"]

    def test_thin_reasoning_is_refused_as_data(self, committed_claim) -> None:
        _, _, review_id = committed_claim
        result = _tools().resolve_review_exception(
            review_id=str(review_id),
            resolution="approved",
            reasoning="ok",
            analyst="iyad",
        )
        assert result["ok"] is False
        assert result["error"] == "InsufficientReasoningError"

    def test_unknown_claim_is_refused_as_data(self) -> None:
        result = _tools().describe_claim(claim_id=str(uuid4()))
        assert result["ok"] is False
        assert "no claim" in result["detail"]


class TestMcpEndToEnd:
    """Exception -> resolve via MCP -> claim reaches approved."""

    def test_resolve_then_approve(self, committed_claim, engine) -> None:
        _, claim_id, review_id = committed_claim
        tools = _tools()

        resolved = tools.resolve_review_exception(
            review_id=str(review_id),
            resolution="approved",
            reasoning="Verified 21,000 SAR against Bayan 20240115447821 line 1 invoice.",
            analyst="iyad",
        )
        assert resolved["ok"] is True
        assert resolved["may_resume"] is True
        assert resolved["outstanding_exceptions"] == 0
        # No workflow run id on the fixture, so nothing to wake — reported, not raised.
        assert resolved["resume"]["delivered"] is False
        assert "no workflow_run_id" in resolved["resume"]["detail"]

        approved = tools.approve(
            claim_id=str(claim_id),
            reasoning="All exceptions cleared; refund reconciles to the Bayan duty line.",
            analyst="iyad",
        )
        assert approved["ok"] is True
        assert approved["state"] == "approved"

        with Session(engine) as session:
            assert session.get(Claim, claim_id).state == "approved"

    def test_approval_blocked_until_the_queue_clears(self, committed_claim) -> None:
        _, claim_id, _ = committed_claim
        result = _tools().approve(
            claim_id=str(claim_id),
            reasoning="Attempting approval while an exception is still open.",
            analyst="iyad",
        )
        assert result["ok"] is False
        assert "unresolved exception" in result["detail"]

    def test_transition_history_is_recorded(self, committed_claim) -> None:
        _, claim_id, review_id = committed_claim
        tools = _tools()

        tools.resolve_review_exception(
            review_id=str(review_id),
            resolution="approved",
            reasoning="Verified against the commercial invoice supplied by the broker.",
            analyst="iyad",
        )
        tools.approve(
            claim_id=str(claim_id),
            reasoning="Cleared for filing; figures reconcile to source documents.",
            analyst="iyad",
        )

        history = tools.claim_transitions(claim_id=str(claim_id))
        assert history["ok"] is True
        assert history["count"] == 1
        assert history["transitions"][0]["to_state"] == "approved"
        assert history["transitions"][0]["actor"] == "iyad"

    def test_valuation_override_records_basis_and_requires_rematch(self, committed_claim) -> None:
        _, _, review_id = committed_claim
        result = _tools().override_declared_valuation(
            review_id=str(review_id),
            corrected_value="21000.00",
            currency="SAR",
            valuation_basis="art_28_export",
            reasoning="Original figure was CIF; Art. 28 basis is 21,000 SAR per invoice.",
            analyst="iyad",
        )
        assert result["ok"] is True
        assert result["resolution"] == "corrected"
        assert result["requires_rematch"] is True
        assert result["valuation_basis"] == "art_28_export"

    def test_non_decimal_override_is_refused(self, committed_claim) -> None:
        _, _, review_id = committed_claim
        result = _tools().override_declared_valuation(
            review_id=str(review_id),
            corrected_value="twenty-one thousand",
            currency="SAR",
            valuation_basis="art_28_export",
            reasoning="This should be refused because the value is not a decimal.",
            analyst="iyad",
        )
        assert result["ok"] is False


class TestLedgerTools:
    def test_audit_trail_shows_the_approval(self, committed_claim) -> None:
        from mcp_servers.mcp_ledger import server as ledger

        _, claim_id, review_id = committed_claim
        tools = _tools()
        tools.resolve_review_exception(
            review_id=str(review_id),
            resolution="approved",
            reasoning="Verified against the Bayan and the forwarder's commercial invoice.",
            analyst="iyad",
        )
        tools.approve(
            claim_id=str(claim_id),
            reasoning="Approved for filing; all figures traced to source documents.",
            analyst="iyad",
        )

        trail = ledger.claim_audit_trail(claim_id=str(claim_id))
        assert trail["ok"] is True
        assert trail["claim"]["state"] == "approved"
        assert trail["transition_count"] == 1

    def test_decision_log_carries_the_reasoning_verbatim(self, committed_claim) -> None:
        from mcp_servers.mcp_ledger import server as ledger

        _, claim_id, review_id = committed_claim
        reasoning = "Verified 21,000 SAR against Bayan 20240115447821; Art. 28 basis."
        _tools().resolve_review_exception(
            review_id=str(review_id),
            resolution="approved",
            reasoning=reasoning,
            analyst="iyad",
        )

        log = ledger.decision_log(claim_id=str(claim_id))
        assert log["ok"] is True
        assert log["count"] == 1
        assert log["decisions"][0]["reasoning"] == reasoning
        assert log["decisions"][0]["analyst"] == "iyad"
        assert log["decisions"][0]["citation"] == "GCC Rules of Implementation Art. 16 §2"


class TestHtsTools:
    """These pin the contract, not the data.

    Week 5 wrote them against an empty corpus and asserted zero results, which quietly
    made "the ingest works" indistinguishable from "the ingest never ran". Week 6 loads
    real corpora, so the assertions moved to what must hold at any corpus size.
    """

    def test_corpus_status_reports_what_is_loaded(self) -> None:
        from mcp_servers.mcp_hts import server as hts

        result = hts.corpus_status()
        assert result["ok"] is True
        assert isinstance(result["tariff_lines"], list)
        for row in result["tariff_lines"]:
            # Embedding coverage must be reported, because vector search silently skips
            # unembedded rows and a partly-embedded corpus answers badly without saying so.
            assert row["embedded"] <= row["lines"]
            assert 0.0 <= row["embedded_pct"] <= 100.0

    def test_classify_returns_a_well_formed_result_at_any_corpus_size(self) -> None:
        from mcp_servers.mcp_hts import server as hts

        result = hts.classify(description="portable data processing machines")
        assert result["ok"] is True
        assert result["method"] in {"lexical", "vector", "hybrid"}
        assert result["count"] == len(result["candidates"])
        for candidate in result["candidates"]:
            assert candidate["code"].isdigit()
            assert candidate["matched_by"] in {"lexical", "vector", "both"}

    def test_a_lexical_hit_is_confirmed_exactly_when_it_clears_the_floor(self) -> None:
        """A vector-only hit is a suggestion; a strong trigram match on published text is not.

        Rewritten in week 13, when the corpus went from nine lines to 28,899. The previous
        form asserted that *no* lexical candidate ever needs confirmation, which held only
        because nine lines could not produce a weak tail: at volume `classify` returns ten
        candidates and the last four score under 0.19, which is exactly the case the floor
        exists to catch. The old assertion also passed vacuously on an empty result.

        What is actually contracted is that the flag tracks the floor carried on the hit —
        not the constant, because a stored classification has to stay explicable against
        the threshold that was in force when it was made.
        """
        from mcp_servers.mcp_hts import server as hts

        result = hts.classify(description="portable data processing machines")
        lexical = [c for c in result["candidates"] if c["matched_by"] == "lexical"]
        assert lexical, "no lexical candidate at all; the trigram index is not answering"
        assert any(not c["needs_analyst_confirmation"] for c in lexical), (
            "every lexical hit needs confirmation; published tariff text should match "
            "published tariff language"
        )
        for candidate in lexical:
            clears = candidate["lexical_score"] >= candidate["confirmation_lexical_floor"]
            assert candidate["needs_analyst_confirmation"] is not clears

    def test_lookup_of_an_absent_code_reports_not_found(self) -> None:
        from mcp_servers.mcp_hts import server as hts

        # Chapter 99 is reserved for temporary US provisions; this suffix is not a line
        # any published schedule carries, so it stays absent whatever has been ingested.
        result = hts.lookup_code(code="9999999999")
        assert result["ok"] is True
        assert result["found"] is False
        assert "no us tariff line" in result["detail"]


@pytest.mark.skipif(
    not os.environ.get("DRAWBRIDGE_TEST_DATABASE_URL"),
    reason="uses the default DSN",
)
def test_sync_dsn_swaps_the_async_driver() -> None:
    """The MCP servers share one setting with the API and swap the driver.

    A second setting would drift out of step with the first.
    """
    assert "+asyncpg" not in claims_db.database_url()
    assert "psycopg" in claims_db.database_url()

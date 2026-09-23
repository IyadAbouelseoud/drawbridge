"""One test per defect the v1.1.0 security pass found, named for what it was.

The repository's recurring lesson (§20, §23) is that a check on an artefact's form holds
for weeks while its use is broken. So these read the artefacts n8n imports, the compose
files Docker reads and the Caddyfile Caddy reads — not the code that generates them — and
each names the defect it pins so a future failure says what came back.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from drawbridge_schemas.provenance import DocumentKind
from mcp_servers.mcp_docs.store import document_id_for, object_key_for, safe_suffix, tenant_owns
from services.api.src.auth import Principal, set_principal
from services.matcher.src.base import MatchResult, SolverStatus
from services.rules.src.triage import ReviewReason, triage

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / "n8n" / "workflows"


def _workflow(name: str) -> dict:
    return json.loads((WORKFLOWS / f"{name}.json").read_text(encoding="utf-8"))


def _node(workflow: dict, name: str) -> dict:
    return next(n for n in workflow["nodes"] if n["name"] == name)


class TestTheWorkflows:
    def test_the_ingest_webhook_authenticates_its_caller(self) -> None:
        """An unauthenticated webhook drove a cross-tenant token at any named tenant."""
        pipeline = _workflow("drawbridge-claim-pipeline")
        admit = _node(pipeline, "Admit Run")
        header = admit["parameters"]["headerParameters"]["parameters"][0]["value"]
        assert "Ingest Webhook" in header and "headers.authorization" in header
        assert pipeline["connections"]["Validate Payload"]["main"][0][0]["node"] == "Admit Run"

    def test_no_workflow_carries_a_static_token(self) -> None:
        for path in WORKFLOWS.glob("*.json"):
            assert "DRAWBRIDGE_SERVICE_TOKEN" not in path.read_text(encoding="utf-8")

    def test_the_error_workflow_no_longer_invents_a_tenant(self) -> None:
        """It posted `tenant_id: e.tenant_id` from a payload that has none."""
        error = _workflow("drawbridge-pipeline-error")
        js = _node(error, "Build Exception")["parameters"]["jsCode"]
        code = " ".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
        assert "e.tenant_id" not in code
        assert "solver_infeasible" not in code
        assert _node(error, "Report Failure")["parameters"]["url"].endswith("/pipeline/failure")

    def test_the_dispatcher_does_not_read_a_tenant_from_a_schedule(self) -> None:
        dispatcher = _workflow("drawbridge-review-dispatcher")
        fetch = _node(dispatcher, "Fetch Open Queue")["parameters"]["url"]
        assert "/review/overview" in fetch
        assert "$json.tenant_id" not in json.dumps(dispatcher)

    def test_a_deferral_does_not_reject_the_claim(self) -> None:
        """`deferred` shared the halt branch with `rejected` and moved the claim to a
        terminal state for a decision meaning "look again later"."""
        pipeline = _workflow("drawbridge-claim-pipeline")
        rejected_branch = pipeline["connections"]["Analyst Rejected"]["main"]
        assert rejected_branch[0][0]["node"] == "Halt Claim"
        assert rejected_branch[1][0]["node"] == "Respond Deferred"

    def test_the_resolution_is_read_from_the_api_not_the_webhook(self) -> None:
        pipeline = _workflow("drawbridge-claim-pipeline")
        order = pipeline["connections"]
        assert order["Resume Access Token"]["main"][0][0]["node"] == "Verify Resolution"
        assert order["Verify Resolution"]["main"][0][0]["node"] == "Apply Resolution"
        js = _node(pipeline, "Apply Resolution")["parameters"]["jsCode"]
        assert "may_continue" in js and "'defer'" in js

    def test_the_filing_identity_is_not_taken_from_the_webhook(self) -> None:
        pipeline = _workflow("drawbridge-claim-pipeline")
        assert "claimant" not in _node(pipeline, "Build Packet")["parameters"]["jsonBody"]
        assert "'claimant'" not in _node(pipeline, "Validate Payload")["parameters"]["jsCode"]

    def test_writes_after_the_wait_use_a_token_minted_after_it(self) -> None:
        """A fifteen-minute token is long dead when an analyst resumes the run."""
        pipeline = _workflow("drawbridge-claim-pipeline")
        for name in ("Verify Resolution", "Halt Claim", "Approve Claim", "Build Packet"):
            header = _node(pipeline, name)["parameters"]["headerParameters"]["parameters"][0]
            assert "Get Access Token" not in header["value"], name


class TestTheDeploymentFiles:
    def test_development_ports_bind_loopback_only(self) -> None:
        lines = (REPO / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
        compose = " ".join(line for line in lines if not line.lstrip().startswith("#"))
        published = re.findall(r'"((?:[\d.]+:)?\d+:\d+)"', compose)
        assert published
        assert all(p.startswith("127.0.0.1:") for p in published), published

    def test_n8n_may_read_the_client_secret_and_not_a_token(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.onprem.yml"):
            text = (REPO / name).read_text(encoding="utf-8")
            allow = re.search(r"N8N_VARS_ALLOWLIST: (\S+)", text)
            assert allow is not None
            assert "SERVICE_TOKEN" not in allow.group(1)
            assert "DRAWBRIDGE_PIPELINE_CLIENT_SECRET" in allow.group(1)

    def test_on_prem_refuses_to_start_without_named_owners(self) -> None:
        text = (REPO / "docker-compose.onprem.yml").read_text(encoding="utf-8")
        for role in ("PLATFORM", "COMPLIANCE", "SECURITY"):
            assert f"DRAWBRIDGE_OWNER_{role}: ${{DRAWBRIDGE_OWNER_{role}:?" in text

    def test_resume_webhooks_are_not_public(self) -> None:
        caddy = (REPO / "infra" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        assert re.search(r"handle /n8n/webhook-waiting/\*\s*\{\s*respond", caddy)
        assert "remote_ip private_ranges" in caddy


class TestDocuments:
    def test_an_object_key_under_another_tenant_is_not_yours(self) -> None:
        mine, theirs = uuid4(), uuid4()
        key = object_key_for(theirs, DocumentKind.CBP_7501, "a" * 64, ".pdf")
        assert tenant_owns(theirs, key)
        assert not tenant_owns(mine, key)

    def test_a_traversal_is_never_owned(self) -> None:
        tenant = uuid4()
        assert not tenant_owns(tenant, f"tenants/{tenant}/../{uuid4()}/x.pdf")

    def test_the_suffix_cannot_shape_the_key(self) -> None:
        assert safe_suffix("/../../etc") == ".bin"
        assert safe_suffix(".PDF") == ".pdf"

    def test_identical_bytes_in_two_tenants_are_two_documents(self) -> None:
        """One id for both was a primary-key collision and a cross-tenant oracle."""
        sha = "b" * 64
        assert document_id_for(sha, uuid4()) != document_id_for(sha, uuid4())


class TestFilingIdentityOverride:
    @pytest.fixture(autouse=True)
    def _reset(self) -> object:
        yield
        set_principal(None)

    def _as(self, principal: Principal | None) -> None:
        set_principal(principal)

    def test_the_pipeline_may_not_print_another_claimant(self) -> None:
        from services.api.src.gates import GateRefusedError
        from services.api.src.routes.packaging import _authorise_identity_override

        self._as(
            Principal(subject="agent:n8n-pipeline", tenant_id=None, agent_id="agent:n8n-pipeline")
        )
        with pytest.raises(GateRefusedError, match="human approver"):
            _authorise_identity_override("refund account")

    def test_an_analyst_may_not_either(self) -> None:
        from services.api.src.gates import GateRefusedError
        from services.api.src.routes.packaging import _authorise_identity_override

        self._as(Principal(subject="alice", tenant_id=uuid4(), roles=frozenset({"analyst"})))
        with pytest.raises(GateRefusedError):
            _authorise_identity_override("claimant")

    def test_an_approver_may(self) -> None:
        from services.api.src.routes.packaging import _authorise_identity_override

        self._as(Principal(subject="bob", tenant_id=uuid4(), roles=frozenset({"approver"})))
        _authorise_identity_override("claimant")


class TestTriage:
    def _result(self) -> MatchResult:
        from drawbridge_schemas.jurisdiction import Jurisdiction

        return MatchResult(jurisdiction=Jurisdiction.US, status=SolverStatus.OPTIMAL)

    def test_a_large_refund_needs_an_approver(self) -> None:
        verdict = triage(
            self._result(),
            refund=(Decimal("250000"), "USD"),
            refund_usd=Decimal("250000"),
            approval_ceiling_usd=Decimal("100000"),
        )
        assert [i.reason for i in verdict.items] == [ReviewReason.HIGH_VALUE_APPROVAL]

    def test_a_small_one_does_not(self) -> None:
        verdict = triage(
            self._result(),
            refund=(Decimal("25092.14"), "USD"),
            refund_usd=Decimal("25092.14"),
            approval_ceiling_usd=Decimal("100000"),
        )
        assert verdict.items == ()


class TestTheMcpSurface:
    SERVERS = ("mcp_claims", "mcp_ledger", "mcp_docs", "mcp_hts", "mcp_ace")

    @pytest.mark.parametrize("name", SERVERS)
    def test_every_tool_is_guarded(self, name: str) -> None:
        import importlib

        module = importlib.import_module(f"mcp_servers.{name}.server")
        for tool in module.server._tool_manager.list_tools():
            fn = getattr(module, tool.name)
            assert hasattr(fn, "__wrapped__"), f"{name}.{tool.name} is not guarded"

    def test_every_write_tool_asks_its_client_first(self) -> None:
        from mcp_servers.mcp_claims import server

        writes = {
            "resolve_review_exception",
            "override_declared_valuation",
            "approve",
            "transition",
            "reopen",
            "draft_exception_memo",
        }
        for tool in server.server._tool_manager.list_tools():
            if tool.name in writes:
                assert tool.annotations is not None and tool.annotations.destructive_hint

    @pytest.mark.parametrize("name", SERVERS)
    def test_every_server_is_built_to_require_a_token(self, name: str) -> None:
        import importlib

        from services.api.src.config import get_settings

        if not get_settings().auth_required:
            pytest.skip("authentication switched off in this environment")
        module = importlib.import_module(f"mcp_servers.{name}.server")
        assert module.server._token_verifier is not None
        assert module.server.settings.auth is not None


def test_the_drafter_never_drafts_what_is_for_a_person() -> None:
    from services.agent.src.exceptions import NEVER_DRAFTED

    assert ReviewReason.SUSPECTED_PROMPT_INJECTION in NEVER_DRAFTED
    assert ReviewReason.PIPELINE_FAILURE in NEVER_DRAFTED


def test_the_uuid_import_is_used() -> None:
    assert UUID(int=0)

"""Who may act, on what, and who answers for it.

Until v1.1.0 the deployment had one kind of machine credential: a token carrying
`drawbridge:service`, minted for a day at a time, that could do anything any route allowed
to any tenant it named. Everything that was not a human — n8n, the drafting worker, the
e2e harness, an analyst's MCP client — was either that token or nothing. So "which agent
did this" had no answer, "who is accountable for that agent" had no answer, and "what is
that agent allowed to do" had the same answer for every one of them: everything.

This module is the answer to all three, as data rather than as convention:

- **Every machine actor has a registered identity.** `agent_id` is the `sub` its tokens
  carry and the `actor` its ledger rows record. A machine token naming an unregistered
  subject is refused at the door (`services/api/src/auth.py`), because an identity that
  exists only in the token is an identity anyone who can mint a token can invent.
- **Every identity has an accountable human.** `owner` is a role, and a deployment binds
  each role to a named person (`Settings.owner_*`). Outside development the API refuses
  to start while any role an agent depends on is unbound — an agent nobody answers for is
  the agent nobody notices misbehaving.
- **Every identity has the least set of scopes its job needs.** Enumerated, closed, and
  checked on every route and every MCP tool. A token's own `scope` claim cannot widen
  them: the registry is the ceiling and the token can only say less.

**Human-only scopes.** Five permissions move money or stop the system, and no machine
identity may hold one: resolving an exception, overriding a valuation, approving a claim,
releasing it past the point it leaves our control, and engaging the kill switch through
the API. `test_no_machine_identity_holds_a_human_only_scope` pins it. The pipeline can
*reach* approval — the automated lane from week 9 is intact — but only through the gate in
`services/api/src/gates.py`, which admits it where a human has already decided or where
there was nothing to decide.

**Short-lived by construction.** `max_token_ttl_seconds` is enforced by the verifier, not
requested of the issuer: a token whose `exp - iat` exceeds its identity's ceiling is
refused however validly it is signed. The 24-hour service token this replaces would now
fail at the first request.

What this is not: a policy engine. Authorisation here is a closed set of scopes, a closed
set of roles and one gate module. A deployment that needs attribute-based rules has
outgrown a registry, and the honest place to say so is here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Scope(StrEnum):
    """One permission. Checked by name on every route and tool that needs it."""

    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_WRITE = "documents:write"
    EXTRACTION_RUN = "extraction:run"
    CLASSIFICATION_RUN = "classification:run"
    MATCHING_RUN = "matching:run"
    TRIAGE_RUN = "triage:run"
    PIPELINE_START = "pipeline:start"

    CLAIMS_READ = "claims:read"
    CLAIMS_PERSIST = "claims:persist"
    CLAIMS_TRANSITION = "claims:transition"
    # An analyst approving a claim at or under the auto-approve ceiling.
    CLAIMS_APPROVE = "claims:approve"
    # High-value approval, and every transition after PACKAGED: the packet leaving our
    # control (HANDED_OFF) and the two facts only a human can attest (FILED, PAID).
    CLAIMS_RELEASE = "claims:release"

    REVIEW_READ = "review:read"
    REVIEW_SUSPEND = "review:suspend"
    REVIEW_RESOLVE = "review:resolve"
    REVIEW_DRAFT = "review:draft"
    VALUATION_OVERRIDE = "valuation:override"

    PACKAGING_BUILD = "packaging:build"
    LEDGER_READ = "ledger:read"
    CORPUS_READ = "corpus:read"

    CONTROL_READ = "control:read"
    CONTROL_KILL = "control:kill"


#: Decisions a machine identity may never hold, whatever its registry entry says.
HUMAN_ONLY_SCOPES: frozenset[Scope] = frozenset(
    {
        Scope.REVIEW_RESOLVE,
        Scope.VALUATION_OVERRIDE,
        Scope.CLAIMS_APPROVE,
        Scope.CLAIMS_RELEASE,
        Scope.CONTROL_KILL,
    }
)


class Role(StrEnum):
    """What a human principal is, carried in the token's `roles` claim."""

    AUDITOR = "auditor"
    ANALYST = "analyst"
    APPROVER = "approver"
    OPERATOR = "operator"


_READ: frozenset[Scope] = frozenset(
    {
        Scope.DOCUMENTS_READ,
        Scope.CLAIMS_READ,
        Scope.REVIEW_READ,
        Scope.LEDGER_READ,
        Scope.CORPUS_READ,
        Scope.CONTROL_READ,
    }
)

_ANALYST: frozenset[Scope] = _READ | {
    Scope.DOCUMENTS_WRITE,
    Scope.EXTRACTION_RUN,
    Scope.CLASSIFICATION_RUN,
    Scope.MATCHING_RUN,
    Scope.TRIAGE_RUN,
    Scope.PIPELINE_START,
    Scope.CLAIMS_PERSIST,
    Scope.CLAIMS_TRANSITION,
    Scope.CLAIMS_APPROVE,
    Scope.REVIEW_SUSPEND,
    Scope.REVIEW_RESOLVE,
    Scope.REVIEW_DRAFT,
    Scope.VALUATION_OVERRIDE,
    Scope.PACKAGING_BUILD,
}

#: Role to permissions. The operator is deliberately *not* an analyst: the person who can
#: stop the system does not thereby acquire the ability to move money through it, and the
#: approver does not thereby acquire the ability to stop it. Separation of duties is a
#: property of this table or it is a property of nothing.
ROLE_SCOPES: dict[Role, frozenset[Scope]] = {
    Role.AUDITOR: _READ,
    Role.ANALYST: _ANALYST,
    Role.APPROVER: _ANALYST | {Scope.CLAIMS_RELEASE},
    Role.OPERATOR: frozenset({Scope.CONTROL_READ, Scope.CONTROL_KILL}),
}


def scopes_for_roles(roles: frozenset[Role] | set[Role]) -> frozenset[Scope]:
    out: set[Scope] = set()
    for role in roles:
        out |= ROLE_SCOPES[role]
    return frozenset(out)


class OwnerRole(StrEnum):
    """An accountable function. A deployment binds each to a named person."""

    PLATFORM = "platform"
    COMPLIANCE = "compliance"
    SECURITY = "security"


class AgentKind(StrEnum):
    #: Calls the API with its own short-lived token.
    API_CLIENT = "api_client"
    #: Connects to Postgres directly under the app role. Never holds a bearer token.
    DATABASE_WORKER = "database_worker"
    #: Serves MCP tools to authenticated callers and acts only on the caller's authority.
    #: For this kind, `scopes` is what a caller must hold to open a session at all; each
    #: tool then checks its own scope on top.
    TOOL_SERVER = "tool_server"
    #: An operator job that needs the owner DSN, and is listed here so that it is known.
    PRIVILEGED_JOB = "privileged_job"


_AGENT_ID = r"^agent:[a-z0-9][a-z0-9-]{2,62}$"


class AgentIdentity(BaseModel):
    """One registered machine actor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: Annotated[str, Field(pattern=_AGENT_ID)]
    display_name: Annotated[str, Field(min_length=3, max_length=80)]
    purpose: Annotated[str, Field(min_length=20, max_length=400)]
    kind: AgentKind
    owner: OwnerRole
    scopes: frozenset[Scope]
    cross_tenant: bool
    max_token_ttl_seconds: Annotated[int, Field(ge=60, le=3600)] = 900
    #: Deployments where the identity may authenticate at all. The e2e harness mints its
    #: own tokens and must not be a way into a production API.
    environments: frozenset[str] = frozenset({"development", "staging", "production"})
    #: The one exception to "the app role only", stated rather than discovered.
    holds_owner_dsn: bool = False

    @model_validator(mode="after")
    def _no_human_decisions(self) -> Self:
        held = self.scopes & HUMAN_ONLY_SCOPES
        if held:
            msg = (
                f"{self.agent_id} may not hold human-only scope(s) {sorted(s.value for s in held)}"
            )
            raise ValueError(msg)
        return self


_PIPELINE_SCOPES: frozenset[Scope] = frozenset(
    {
        Scope.DOCUMENTS_WRITE,
        Scope.EXTRACTION_RUN,
        Scope.CLASSIFICATION_RUN,
        Scope.MATCHING_RUN,
        Scope.TRIAGE_RUN,
        Scope.CLAIMS_READ,
        Scope.CLAIMS_PERSIST,
        Scope.CLAIMS_TRANSITION,
        Scope.REVIEW_READ,
        Scope.REVIEW_SUSPEND,
        Scope.REVIEW_DRAFT,
        Scope.PACKAGING_BUILD,
    }
)

_REGISTRY: tuple[AgentIdentity, ...] = (
    AgentIdentity(
        agent_id="agent:n8n-pipeline",
        display_name="n8n claim pipeline",
        purpose=(
            "Drives ingest -> extract -> classify -> match -> triage -> persist -> "
            "package for whichever tenant an admitted run names. Holds no business state."
        ),
        kind=AgentKind.API_CLIENT,
        owner=OwnerRole.PLATFORM,
        scopes=_PIPELINE_SCOPES,
        cross_tenant=True,
        max_token_ttl_seconds=900,
    ),
    AgentIdentity(
        agent_id="agent:memo-drafter",
        display_name="Exception memo drafter",
        purpose=(
            "Drafts advisory pre-analysis for open review rows with the Claude model. "
            "Writes one column; moves no claim; resolves nothing."
        ),
        kind=AgentKind.DATABASE_WORKER,
        owner=OwnerRole.COMPLIANCE,
        scopes=frozenset({Scope.REVIEW_READ, Scope.REVIEW_DRAFT}),
        cross_tenant=True,
        max_token_ttl_seconds=900,
    ),
    AgentIdentity(
        agent_id="agent:mcp-claims",
        display_name="mcp-claims tool server",
        purpose=(
            "Serves the analyst's claim and review tools. Every decision it records is "
            "made on the authenticated caller's authority, never its own."
        ),
        kind=AgentKind.TOOL_SERVER,
        owner=OwnerRole.COMPLIANCE,
        scopes=frozenset({Scope.REVIEW_READ}),
        cross_tenant=False,
    ),
    AgentIdentity(
        agent_id="agent:mcp-ledger",
        display_name="mcp-ledger tool server",
        purpose="Serves read-only audit trail, provenance and chain-verification tools.",
        kind=AgentKind.TOOL_SERVER,
        owner=OwnerRole.COMPLIANCE,
        scopes=frozenset({Scope.LEDGER_READ}),
        cross_tenant=False,
    ),
    AgentIdentity(
        agent_id="agent:mcp-docs",
        display_name="mcp-docs tool server",
        purpose="Serves document storage and span-verification tools, scoped to one tenant.",
        kind=AgentKind.TOOL_SERVER,
        owner=OwnerRole.PLATFORM,
        scopes=frozenset({Scope.DOCUMENTS_READ}),
        cross_tenant=False,
    ),
    AgentIdentity(
        agent_id="agent:mcp-hts",
        display_name="mcp-hts tool server",
        purpose="Serves tariff classification and ruling search over shared reference data.",
        kind=AgentKind.TOOL_SERVER,
        owner=OwnerRole.PLATFORM,
        scopes=frozenset({Scope.CORPUS_READ}),
        cross_tenant=False,
    ),
    AgentIdentity(
        agent_id="agent:mcp-ace",
        display_name="mcp-ace tool server",
        purpose="Placeholder for ACE entry-summary tools; exposes a liveness probe only.",
        kind=AgentKind.TOOL_SERVER,
        owner=OwnerRole.PLATFORM,
        scopes=frozenset(),
        cross_tenant=False,
    ),
    AgentIdentity(
        agent_id="agent:retention",
        display_name="Backup and chain verification job",
        purpose=(
            "pg_dump into object-locked storage and hash-chain verification across every "
            "tenant. Needs the owner DSN: a dump under RLS is a dump of nothing."
        ),
        kind=AgentKind.PRIVILEGED_JOB,
        owner=OwnerRole.SECURITY,
        scopes=frozenset(),
        cross_tenant=True,
        holds_owner_dsn=True,
    ),
    AgentIdentity(
        agent_id="agent:e2e-harness",
        display_name="End-to-end pipeline harness",
        purpose=(
            "Drives the deployed API the way n8n does, for scripts/e2e_pipeline_test.py "
            "and the pilots. Development only."
        ),
        kind=AgentKind.API_CLIENT,
        owner=OwnerRole.PLATFORM,
        # It starts runs as well as driving them: the harness plays both the caller of
        # the ingest webhook and the pipeline behind it.
        scopes=_PIPELINE_SCOPES | {Scope.PIPELINE_START},
        cross_tenant=True,
        max_token_ttl_seconds=900,
        environments=frozenset({"development"}),
    ),
)

AGENTS: dict[str, AgentIdentity] = {agent.agent_id: agent for agent in _REGISTRY}

if len(AGENTS) != len(_REGISTRY):  # pragma: no cover - a duplicate is a coding error
    msg = "duplicate agent_id in the registry"
    raise RuntimeError(msg)


def agent(agent_id: str) -> AgentIdentity | None:
    return AGENTS.get(agent_id)


def is_agent_subject(subject: str) -> bool:
    """Whether a token subject claims to be a machine identity.

    The prefix is the claim, not the proof: `auth.decode` then requires the subject to be
    registered, so a token saying `agent:anything` is refused rather than trusted.
    """
    return subject.startswith("agent:")

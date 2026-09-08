"""Generate the Drawbridge n8n workflows.

Workflows are version-controlled as JSON and imported into n8n, not authored in its UI:
a workflow edited in the browser has no diff, no review, and no way to reproduce a run
from the repository. Re-run this after changing a definition, then import the output.

    uv run python n8n/generate_workflows.py
"""

from __future__ import annotations

import itertools
import json
import pathlib

API = "http://api:8000"


def node(
    name: str,
    ntype: str,
    x: int,
    y: int,
    params: dict,
    tv: float = 1,
    extra: dict | None = None,
) -> dict:
    result = {
        "parameters": params,
        "id": name.lower().replace(" ", "-").replace("?", ""),
        "name": name,
        "type": ntype,
        "typeVersion": tv,
        "position": [x, y],
    }
    if extra:
        result.update(extra)
    return result


def http(name: str, x: int, y: int, method: str, url: str, body: str | None = None) -> dict:
    params: dict = {
        "method": method,
        "url": url,
        "options": {"response": {"response": {"neverError": True}}},
    }
    if body is not None:
        params |= {"sendBody": True, "specifyBody": "json", "jsonBody": body}
    return node(name, "n8n-nodes-base.httpRequest", x, y, params, tv=4.2)


def code(name: str, x: int, y: int, js: str) -> dict:
    return node(name, "n8n-nodes-base.code", x, y, {"jsCode": js.strip()}, tv=2)


def boolean_if(name: str, x: int, y: int, left: str, operation: str, right: str = "") -> dict:
    op = (
        {"type": "boolean", "operation": operation}
        if operation in {"true", "false"}
        else {"type": "string", "operation": operation}
    )
    return node(
        name,
        "n8n-nodes-base.if",
        x,
        y,
        {
            "conditions": {
                "options": {"caseSensitive": True, "version": 2},
                "conditions": [
                    {"id": "c1", "operator": op, "leftValue": left, "rightValue": right}
                ],
                "combinator": "and",
            },
            "options": {},
        },
        tv=2,
    )


def chain(*names: str) -> dict:
    out: dict = {}
    for src, dst in itertools.pairwise(names):
        out.setdefault(src, {"main": [[]]})
        out[src]["main"][0].append({"node": dst, "type": "main", "index": 0})
    return out


def branch(name: str, true_node: str, false_node: str) -> dict:
    return {
        name: {
            "main": [
                [{"node": true_node, "type": "main", "index": 0}],
                [{"node": false_node, "type": "main", "index": 0}],
            ]
        }
    }


VALIDATE_JS = """
// Fail fast on a malformed batch. A claim reaching the matcher without its jurisdiction
// would be matched under whichever statute happens to be the default, which is exactly
// the failure that looks plausible all the way to a filing.
//
// `claimant` is required here rather than at the packaging step because that step runs
// twenty minutes and one analyst decision later, and discovering then that the filing
// identity was never supplied means the run is lost, not delayed.
const b = $input.first().json;
const REQUIRED = ['tenant_id', 'jurisdiction', 'documents', 'imports', 'exports', 'claimant'];
for (const f of REQUIRED) {
  if (!b[f]) throw new Error('ingest payload missing required field: ' + f);
}
if (!['us', 'ksa'].includes(b.jurisdiction)) {
  throw new Error('unknown jurisdiction: ' + b.jurisdiction);
}
if (!b.imports.length || !b.exports.length) {
  throw new Error('a claim needs at least one import line and one export line');
}
return [{ json: Object.assign({}, b, {
  as_of: b.as_of || new Date().toISOString().slice(0, 10),
}) }];
"""

CONFIDENCE_JS = """
// /extraction/run already applied the floor and said what it found. This node decides
// what the pipeline does about it, which is a different question and belongs here rather
// than in the API: the floor is a rule, the response to breaching it is a policy.
//
// A document that could not be read at all is treated as below floor, not as absent. The
// two are indistinguishable downstream and only one of them is safe to ignore.
const x = $input.first().json;
const unreadable = (x.documents || []).filter(d => d.readable === false).length;
const below = (x.confidence_below_floor || 0) + unreadable;
return [{ json: Object.assign({}, x, {
  confidence_below_floor: below,
  confidence_ok: below === 0,
  unreadable_documents: unreadable,
}) }];
"""

CLASSIFICATION_JS = """
// A declared code the schedule does not corroborate is not an error and not an approval.
// It is the case an analyst should see, because classification drives the duty rate and
// the pipeline is not permitted to change a code that was actually declared.
const c = $input.first().json;
const unsupported = c.unsupported || [];
return [{ json: Object.assign({}, c, {
  classification_ok: unsupported.length === 0,
  unsupported_count: unsupported.length,
}) }];
"""

RESOLUTION_JS = """
// An analyst may approve, correct, reject or defer. Only the first two continue; a
// rejected claim stops here with its reason recorded rather than proceeding quietly.
// A deferred one also stops — it is still open, and the dispatcher will bring it back.
const r = $input.first().json;
const proceed = ['approved', 'corrected'].indexOf(r.resolution) !== -1;
return [{ json: Object.assign({}, r, {
  halt: !proceed,
  next_state: proceed ? 'approved' : 'rejected',
}) }];
"""

RANK_JS = """
// Blocking first, then by how little time is left on the filing window. Age alone is
// the wrong sort: a three-week-old item with six months of runway is less urgent than
// yesterday's item with nine days.
//
// Reads the queue from the fetch node rather than from its own input, because the
// drafting sweep sits between them and returns a count, not the rows.
const items = $('Fetch Open Queue').all().map(i => i.json);
const RANK = { blocking: 0, high: 1, normal: 2, low: 3 };
items.sort(function (a, b) {
  const s = (RANK[a.severity] === undefined ? 9 : RANK[a.severity])
          - (RANK[b.severity] === undefined ? 9 : RANK[b.severity]);
  if (s !== 0) return s;
  const da = (a.payload && a.payload.days !== undefined) ? a.payload.days : 9999;
  const db = (b.payload && b.payload.days !== undefined) ? b.payload.days : 9999;
  return da - db;
});
return items.map(function (json) { return { json: json }; });
"""

ESCALATE_JS = """
// Blocking exceptions page immediately. Everything else waits for the digest: paging on
// a normal-severity item trains people to ignore the pager.
return [{ json: {
  channel: 'page',
  severity: $json.severity,
  summary: $json.summary,
  citation: $json.citation,
  review_id: $json.review_id,
} }];
"""

DIGEST_JS = """
return [{ json: {
  channel: 'digest',
  count: $input.all().length,
  items: $input.all().map(function (i) {
    return { id: i.json.review_id, reason: i.json.reason, summary: i.json.summary };
  }),
} }];
"""

ERROR_JS = """
// A crashed run must leave the claim in a state an analyst can act on, not stranded
// mid-transition with no record of why it stopped.
const e = $input.first().json;
const ex = e.execution || {};
return [{ json: {
  tenant_id: e.tenant_id,
  reason: 'solver_infeasible',
  severity: 'blocking',
  summary: 'pipeline run ' + ex.id + ' failed at node ' + ex.lastNodeExecuted
         + ': ' + ((ex.error && ex.error.message) || 'unknown error'),
  payload: { node: ex.lastNodeExecuted, workflow: (e.workflow || {}).name },
} }];
"""


def pipeline() -> dict:
    """The closed loop: a document goes in, a filing packet comes out.

    Two paths through it, and the branch is `requires_review` from `/triage/evaluate`:

    - **Automated.** Confidence clears the floor, the schedule corroborates the declared
      codes, the solver reached optimality. The claim persists in `quantified`, transitions
      straight to `approved`, and is packaged. No human is involved and none is needed;
      `analyst.transition_claim` still refuses the approval if any exception is open, so
      this lane cannot be reached by a claim that has one.

    - **Exception.** Triage returns one row per reason. Those are posted to
      `/review/suspend`, the agent drafts pre-analysis into each of them, and the run parks
      on a Wait node until `mcp-claims` resolves the exception and calls the resume
      webhook. An approval or correction rejoins the automated path at `approved`; a
      rejection or deferral ends the run with the claim where the analyst left it.

    The claim is persisted *before* the branch, deliberately. An analyst opening a
    suspended run needs a claim to look at — with an id, a refund figure and a derivation —
    not a workflow variable. It also means a crashed run loses the workflow and not the
    work.
    """
    return {
        "name": "drawbridge-claim-pipeline",
        "meta": {
            "description": (
                "Ingest -> extract -> classify -> match -> triage -> persist -> "
                "approve or suspend -> package. The Postgres claim state machine is "
                "authoritative; this workflow fires transitions and never holds business "
                "state (docs/ARCHITECTURE.md section 4)."
            )
        },
        "settings": {
            "executionOrder": "v1",
            "saveManualExecutions": True,
            "errorWorkflow": "drawbridge-pipeline-error",
        },
        "nodes": [
            node(
                "Ingest Webhook",
                "n8n-nodes-base.webhook",
                -1120,
                300,
                {
                    "httpMethod": "POST",
                    "path": "drawbridge/ingest",
                    "responseMode": "responseNode",
                    "options": {},
                },
                tv=2,
                extra={"webhookId": "drawbridge-ingest"},
            ),
            code("Validate Payload", -940, 300, VALIDATE_JS),
            http(
                "Store Documents",
                -760,
                300,
                "POST",
                API + "/documents/batch",
                "={{ JSON.stringify({ tenant_id: $json.tenant_id, documents: $json.documents }) }}",
            ),
            http(
                "Extract",
                -580,
                300,
                "POST",
                API + "/extraction/run",
                "={{ JSON.stringify({ "
                "tenant_id: $('Validate Payload').item.json.tenant_id, "
                "documents: $json.stored.map(function (d) { return d.document_id; }) }) }}",
            ),
            code("Confidence Gate", -400, 300, CONFIDENCE_JS),
            http(
                "Classify",
                -220,
                300,
                "POST",
                API + "/classification/run",
                "={{ JSON.stringify({ "
                "jurisdiction: $('Validate Payload').item.json.jurisdiction, "
                "lines: $('Validate Payload').item.json.imports.map(function (l) { "
                "return { line_id: l.line_id, description: l.description, "
                "declared_code: l.hts.code }; }) }) }}",
            ),
            code("Classification Gate", -40, 300, CLASSIFICATION_JS),
            http(
                "Match",
                140,
                300,
                "POST",
                API + "/matching/run",
                "={{ JSON.stringify({ "
                "jurisdiction: $('Validate Payload').item.json.jurisdiction, "
                "imports: $('Validate Payload').item.json.imports, "
                "exports: $('Validate Payload').item.json.exports, "
                "as_of: $('Validate Payload').item.json.as_of }) }}",
            ),
            http(
                "Triage",
                320,
                300,
                "POST",
                API + "/triage/evaluate",
                "={{ JSON.stringify({ match_result: $json, "
                "confidences: $('Confidence Gate').item.json.confidences, "
                "as_of: $('Validate Payload').item.json.as_of }) }}",
            ),
            http(
                "Persist Claim",
                500,
                300,
                "POST",
                API + "/claims/persist",
                "={{ JSON.stringify({ "
                "tenant_id: $('Validate Payload').item.json.tenant_id, "
                "jurisdiction: $('Validate Payload').item.json.jurisdiction, "
                "imports: $('Validate Payload').item.json.imports, "
                "exports: $('Validate Payload').item.json.exports, "
                "matches: $('Match').item.json.matches, "
                "total_refund: $('Match').item.json.total_refund, "
                "requires_review: $('Triage').item.json.requires_review }) }}",
            ),
            boolean_if(
                "Needs Review",
                680,
                300,
                "={{ $('Triage').item.json.requires_review }}",
                "true",
            ),
            http(
                "Suspend For Analyst",
                880,
                140,
                "POST",
                API + "/review/suspend",
                "={{ JSON.stringify({ "
                "tenant_id: $('Validate Payload').item.json.tenant_id, "
                "claim_id: $('Persist Claim').item.json.claim_id, "
                "workflow_run_id: $execution.id, "
                "items: $('Triage').item.json.items }) }}",
            ),
            http(
                "Draft Analyst Memos",
                1060,
                140,
                "POST",
                API + "/review/draft?tenant_id={{ $('Validate Payload').item.json.tenant_id }}",
            ),
            node(
                "Await Resolution",
                "n8n-nodes-base.wait",
                1240,
                140,
                {"resume": "webhook", "options": {}},
                tv=1.1,
                extra={"webhookId": "drawbridge-review-resume"},
            ),
            code("Apply Resolution", 1420, 140, RESOLUTION_JS),
            boolean_if("Analyst Continued", 1600, 140, "={{ $json.halt }}", "false"),
            http(
                "Approve Claim",
                1820,
                300,
                "POST",
                API + "/claims/transition",
                "={{ JSON.stringify({ "
                "claim_id: $('Persist Claim').item.json.claim_id, "
                "to_state: 'approved', actor: 'pipeline', "
                "reason: 'pipeline run ' + $execution.id }) }}",
            ),
            http(
                "Halt Claim",
                1820,
                -20,
                "POST",
                API + "/claims/transition",
                "={{ JSON.stringify({ "
                "claim_id: $('Persist Claim').item.json.claim_id, "
                "to_state: 'rejected', actor: 'analyst', "
                "reason: $('Apply Resolution').item.json.resolution_note "
                "|| 'analyst did not approve' }) }}",
            ),
            http(
                "Build Packet",
                2000,
                300,
                "POST",
                API + "/packaging/build",
                "={{ JSON.stringify({ "
                "claim_id: $('Persist Claim').item.json.claim_id, "
                "claimant: $('Validate Payload').item.json.claimant, "
                "include_artifacts: false }) }}",
            ),
            http(
                "Mark Packaged",
                2180,
                300,
                "POST",
                API + "/claims/transition",
                "={{ JSON.stringify({ "
                "claim_id: $('Persist Claim').item.json.claim_id, "
                "to_state: 'packaged', actor: 'pipeline', "
                "reason: 'packet rendered by run ' + $execution.id }) }}",
            ),
            node(
                "Respond",
                "n8n-nodes-base.respondToWebhook",
                2360,
                300,
                {
                    "respondWith": "json",
                    "responseBody": (
                        "={{ JSON.stringify({ "
                        "claim_id: $('Persist Claim').item.json.claim_id, "
                        "state: $json.state, "
                        "refund: $('Persist Claim').item.json.total_refund, "
                        "transmittable: $('Build Packet').item.json.transmittable, "
                        "artifacts: $('Build Packet').item.json.manifest.artifacts }) }}"
                    ),
                    "options": {},
                },
                tv=1.1,
            ),
            node(
                "Respond Halted",
                "n8n-nodes-base.respondToWebhook",
                2000,
                -20,
                {
                    "respondWith": "json",
                    "responseBody": (
                        "={{ JSON.stringify({ "
                        "claim_id: $('Persist Claim').item.json.claim_id, "
                        "state: 'rejected', "
                        "resolution: $('Apply Resolution').item.json.resolution }) }}"
                    ),
                    "options": {},
                },
                tv=1.1,
            ),
        ],
        "connections": {
            **chain(
                "Ingest Webhook",
                "Validate Payload",
                "Store Documents",
                "Extract",
                "Confidence Gate",
                "Classify",
                "Classification Gate",
                "Match",
                "Triage",
                "Persist Claim",
                "Needs Review",
            ),
            **branch("Needs Review", "Suspend For Analyst", "Approve Claim"),
            **chain(
                "Suspend For Analyst",
                "Draft Analyst Memos",
                "Await Resolution",
                "Apply Resolution",
                "Analyst Continued",
            ),
            **branch("Analyst Continued", "Approve Claim", "Halt Claim"),
            **chain("Halt Claim", "Respond Halted"),
            **chain("Approve Claim", "Build Packet", "Mark Packaged", "Respond"),
        },
        "pinData": {},
        "tags": [{"name": "drawbridge"}, {"name": "pipeline"}],
    }


def review_dispatcher() -> dict:
    return {
        "name": "drawbridge-review-dispatcher",
        "meta": {
            "description": (
                "Polls the review queue, drafts pre-analysis for anything still "
                "undrafted, and escalates. Deadline-driven: an exception whose filing "
                "window is closing deserves more attention than an older one whose "
                "window is open. The drafting sweep is a backstop for rows queued "
                "outside a pipeline run — the error workflow queues some — and is a "
                "no-op for rows the pipeline already drafted."
            )
        },
        "settings": {"executionOrder": "v1"},
        "nodes": [
            node(
                "Every 15 Minutes",
                "n8n-nodes-base.scheduleTrigger",
                -620,
                300,
                {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}},
                tv=1.2,
            ),
            http(
                "Fetch Open Queue",
                -400,
                300,
                "GET",
                API + "/review/queue?tenant_id={{ $json.tenant_id }}&state=open&limit=200",
            ),
            http(
                "Draft Missing Memos",
                -220,
                300,
                "POST",
                API + "/review/draft?tenant_id={{ $json.tenant_id }}",
            ),
            code("Rank By Urgency", -40, 300, RANK_JS),
            boolean_if("Is Blocking", 160, 300, "={{ $json.severity }}", "equals", "blocking"),
            code("Escalate", 380, 160, ESCALATE_JS),
            code("Digest", 380, 440, DIGEST_JS),
        ],
        "connections": {
            **chain(
                "Every 15 Minutes",
                "Fetch Open Queue",
                "Draft Missing Memos",
                "Rank By Urgency",
                "Is Blocking",
            ),
            **branch("Is Blocking", "Escalate", "Digest"),
        },
        "pinData": {},
        "tags": [{"name": "drawbridge"}, {"name": "hitl"}],
    }


def error_workflow() -> dict:
    return {
        "name": "drawbridge-pipeline-error",
        "meta": {
            "description": (
                "Error workflow for the pipeline. A crashed run leaves a blocking "
                "review row rather than a claim stranded mid-transition."
            )
        },
        "settings": {"executionOrder": "v1"},
        "nodes": [
            node("Error Trigger", "n8n-nodes-base.errorTrigger", -420, 300, {}, tv=1),
            code("Build Exception", -200, 300, ERROR_JS),
            http(
                "Queue Exception",
                20,
                300,
                "POST",
                API + "/review/suspend",
                "={{ JSON.stringify({ tenant_id: $json.tenant_id, "
                "workflow_run_id: $json.payload.workflow, items: [$json] }) }}",
            ),
        ],
        "connections": chain("Error Trigger", "Build Exception", "Queue Exception"),
        "pinData": {},
        "tags": [{"name": "drawbridge"}],
    }


def main() -> None:
    out = pathlib.Path(__file__).resolve().parent / "workflows"
    out.mkdir(parents=True, exist_ok=True)
    for workflow in (pipeline(), review_dispatcher(), error_workflow()):
        path = out / f"{workflow['name']}.json"
        path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.name} ({len(workflow['nodes'])} nodes)")


if __name__ == "__main__":
    main()

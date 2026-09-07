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
const b = $input.first().json;
for (const f of ['tenant_id', 'jurisdiction', 'documents']) {
  if (!b[f]) throw new Error('ingest payload missing required field: ' + f);
}
if (!['us', 'ksa'].includes(b.jurisdiction)) {
  throw new Error('unknown jurisdiction: ' + b.jurisdiction);
}
return [{ json: Object.assign({}, b, {
  as_of: b.as_of || new Date().toISOString().slice(0, 10),
}) }];
"""

CONFIDENCE_JS = """
// Extraction confidence below the floor means a figure may be misread. Matching on a
// misread figure yields an internally consistent claim for the wrong amount, which no
// downstream check would catch.
const FLOOR = 0.95;
const x = $input.first().json;
const scores = (x.confidences || []).map(c => c.score);
const below = scores.filter(s => s < FLOOR);
return [{ json: Object.assign({}, x, {
  confidence_min: scores.length ? Math.min.apply(null, scores) : null,
  confidence_below_floor: below.length,
  confidence_ok: below.length === 0,
}) }];
"""

RESOLUTION_JS = """
// An analyst may approve, correct, reject or defer. Only the first two continue; a
// rejected claim stops here with its reason recorded rather than proceeding quietly.
const r = $input.first().json;
if (['approved', 'corrected'].indexOf(r.resolution) === -1) {
  return [{ json: Object.assign({}, r, { halt: true, final_state: 'rejected' }) }];
}
return [{ json: Object.assign({}, r, { halt: false }) }];
"""

RANK_JS = """
// Blocking first, then by how little time is left on the filing window. Age alone is
// the wrong sort: a three-week-old item with six months of runway is less urgent than
// yesterday's item with nine days.
const RANK = { blocking: 0, high: 1, normal: 2, low: 3 };
const items = $input.all().map(i => i.json);
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
    return {
        "name": "drawbridge-claim-pipeline",
        "meta": {
            "description": (
                "Ingest -> extract -> match -> triage -> persist. The Postgres claim "
                "state machine is authoritative; this workflow fires transitions and "
                "never holds business state (docs/ARCHITECTURE.md section 4)."
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
                -760,
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
            code("Validate Payload", -560, 300, VALIDATE_JS),
            http(
                "Store Documents",
                -360,
                300,
                "POST",
                API + "/documents/batch",
                "={{ JSON.stringify({ tenant_id: $json.tenant_id, documents: $json.documents }) }}",
            ),
            http(
                "Extract",
                -160,
                300,
                "POST",
                API + "/extraction/run",
                "={{ JSON.stringify({ tenant_id: $('Validate Payload').item.json.tenant_id, "
                "documents: $json.stored }) }}",
            ),
            code("Confidence Gate", 40, 300, CONFIDENCE_JS),
            http(
                "Match",
                240,
                300,
                "POST",
                API + "/matching/run",
                "={{ JSON.stringify({ "
                "jurisdiction: $('Validate Payload').item.json.jurisdiction, "
                "imports: $json.imports, exports: $json.exports, "
                "as_of: $('Validate Payload').item.json.as_of }) }}",
            ),
            http(
                "Triage",
                440,
                300,
                "POST",
                API + "/triage/evaluate",
                "={{ JSON.stringify({ match_result: $json, "
                "confidences: $('Confidence Gate').item.json.confidences, "
                "as_of: $('Validate Payload').item.json.as_of }) }}",
            ),
            boolean_if("Needs Review", 640, 300, "={{ $json.requires_review }}", "true"),
            http(
                "Suspend For Analyst",
                860,
                160,
                "POST",
                API + "/review/suspend",
                "={{ JSON.stringify({ "
                "tenant_id: $('Validate Payload').item.json.tenant_id, "
                "claim_id: $('Match').item.json.claim_id, "
                "workflow_run_id: $execution.id, items: $json.items }) }}",
            ),
            node(
                "Await Resolution",
                "n8n-nodes-base.wait",
                1060,
                160,
                {"resume": "webhook", "options": {}},
                tv=1.1,
                extra={"webhookId": "drawbridge-review-resume"},
            ),
            code("Apply Resolution", 1260, 160, RESOLUTION_JS),
            http(
                "Persist Claim",
                1480,
                300,
                "POST",
                API + "/claims/persist",
                "={{ JSON.stringify({ "
                "tenant_id: $('Validate Payload').item.json.tenant_id, "
                "jurisdiction: $('Validate Payload').item.json.jurisdiction, "
                "match_result: $('Match').item.json, "
                "review: $('Triage').item.json }) }}",
            ),
            http(
                "Advance State",
                1680,
                300,
                "POST",
                API + "/claims/transition",
                "={{ JSON.stringify({ claim_id: $json.claim_id, "
                "to_state: $json.next_state, actor: 'n8n', "
                "reason: 'pipeline run ' + $execution.id }) }}",
            ),
            node(
                "Respond",
                "n8n-nodes-base.respondToWebhook",
                1880,
                300,
                {
                    "respondWith": "json",
                    "responseBody": (
                        "={{ JSON.stringify({ claim_id: $json.claim_id, "
                        "state: $json.state, refund: $json.total_refund }) }}"
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
                "Match",
                "Triage",
                "Needs Review",
            ),
            **branch("Needs Review", "Suspend For Analyst", "Persist Claim"),
            **chain(
                "Suspend For Analyst",
                "Await Resolution",
                "Apply Resolution",
                "Persist Claim",
                "Advance State",
                "Respond",
            ),
        },
        "pinData": {},
        "tags": [{"name": "drawbridge"}, {"name": "pipeline"}],
    }


def review_dispatcher() -> dict:
    return {
        "name": "drawbridge-review-dispatcher",
        "meta": {
            "description": (
                "Polls the review queue and escalates. Deadline-driven: an exception "
                "whose filing window is closing deserves more attention than an older "
                "one whose window is open."
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
            code("Rank By Urgency", -180, 300, RANK_JS),
            boolean_if("Is Blocking", 40, 300, "={{ $json.severity }}", "equals", "blocking"),
            code("Escalate", 280, 160, ESCALATE_JS),
            code("Digest", 280, 440, DIGEST_JS),
        ],
        "connections": {
            **chain("Every 15 Minutes", "Fetch Open Queue", "Rank By Urgency", "Is Blocking"),
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

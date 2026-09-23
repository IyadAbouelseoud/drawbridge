"""The generation call. One function, heavily constrained, used by every drafter.

Three guardrails are hardcoded rather than parameterised, because each is a property of
the *product* rather than of a call site, and a parameter is an invitation to differ:

- `MAX_TOKENS = 1024`. A memo that needs more than this is not a better memo; it is the
  model padding. The cap also bounds the cost of a runaway loop over a queue.
- `THINKING = {"type": "disabled"}`. The memo is short, the reasoning it needs is in the
  facts, and forced tool use is the output contract — which a thinking model may not be
  held to. Claude Opus 5 accepts `disabled` at its default effort.
- **Forced tool use for output.** The model is given exactly one tool, the memo schema,
  and `tool_choice` requires it. This is not JSON-mode-by-prompting; the model has no
  path to a prose reply, so there is no parse step that can fail on a preamble.

**What happened to `temperature = 0` (v1.1.0).** It was here from week 8, and Claude Opus 5
rejects sampling parameters with a 400. Every call this module would have made against the
configured model would have failed — unnoticed, because no deployment has ever held a key
(§20.8), and the stub the tests use accepts any keyword. The determinism it stood for was
never available from the model anyway; it comes from the record: the memo is stored
verbatim with the model and prompt version that produced it (`queue.model_tag`), so what an
analyst read in 2026 is what an auditor reads in 2030, without re-running anything.

**The transport is bounded.** One client per process (connection reuse; the previous shape
built a new one per memo), a 60-second request timeout rather than the SDK's ten minutes,
and the SDK's own two retries on 429/5xx/connection errors. A worker that can hang for ten
minutes per row holds a row lock for ten minutes per row.

**A refusal is not a memo.** `stop_reason == "refusal"` raises `AgentRefusedError`, which
the queue treats like any other contract failure: no memo, the row read unaided. Server-side
refusal fallbacks to another model are deliberately not enabled — `agent_model` must name
the model that wrote the memo, and a silent switch would make that record untrue.

Retries on schema violation: one, with the validation error fed back. A second schema
failure will not become a third success — it means the schema and the task disagree, which
is a bug to fix rather than a call to repeat.
"""

from __future__ import annotations

import json
import os
import threading
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from services.api.src.telemetry import tracer

if TYPE_CHECKING:
    from collections.abc import Sequence

# Matches `Settings.model_reasoning`. Named here as well so the agent package can be used
# without the API's settings object — a drafting run from the CLI should not have to
# construct FastAPI configuration to know which model to call.
MODEL = "claude-opus-5"

# Hardcoded. See the module docstring — these are not call-site decisions.
MAX_TOKENS = 1024
THINKING: dict[str, str] = {"type": "disabled"}

#: Seconds per request, and the SDK's own retry count on transient failures.
REQUEST_TIMEOUT_SECONDS = 60.0
TRANSPORT_RETRIES = 2

_MAX_ATTEMPTS = 2

_CLIENTS: dict[str, Any] = {}
_CLIENTS_LOCK = threading.Lock()


class AgentUnavailableError(RuntimeError):
    """No API key, or the API could not be reached.

    Distinct from a bad generation. A queue item with no memo is a queue item an analyst
    reads unaided, which is exactly how the system worked before this service existed;
    a queue item with a *wrong* memo is worse than none. Callers are expected to catch
    this and attach nothing.
    """


class AgentRefusedError(RuntimeError):
    """The model produced output that does not satisfy the contract after retry."""


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """The memo schema as an Anthropic tool definition."""
    schema = model.model_json_schema()
    return {
        "name": "record_memo",
        "description": (
            f"Record the completed {model.__name__}. Every field is required to be "
            "grounded in the supplied facts."
        ),
        "input_schema": schema,
    }


def _client(api_key: str | None) -> Any:
    """One SDK client per key per process — see the module docstring."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        msg = "ANTHROPIC_API_KEY is not set; the agent layer cannot draft"
        raise AgentUnavailableError(msg)
    with _CLIENTS_LOCK:
        cached = _CLIENTS.get(key)
        if cached is not None:
            return cached
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - anthropic is a hard dependency
            msg = "the anthropic SDK is not installed"
            raise AgentUnavailableError(msg) from exc
        client = Anthropic(
            api_key=key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=TRANSPORT_RETRIES
        )
        _CLIENTS[key] = client
        return client


def _tool_input(response: Any) -> dict[str, Any]:
    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        msg = "the model declined to draft this memo (stop_reason=refusal)"
        raise AgentRefusedError(msg)
    if stop == "max_tokens":
        # A tool input cut off at the cap is a partial record, and the schema might still
        # accept it — a truncated `blocking_unknowns` list is a valid, shorter list.
        msg = f"the memo hit the {MAX_TOKENS}-token cap and may be truncated"
        raise AgentRefusedError(msg)
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_memo":
            payload = block.input
            return dict(payload) if isinstance(payload, dict) else {}
    msg = "model returned no tool call despite tool_choice requiring one"
    raise AgentRefusedError(msg)


def _call(
    anthropic: Any,
    *,
    model: str,
    system: str,
    tool: dict[str, Any],
    messages: list[dict[str, Any]],
) -> Any:
    """One model call, with every transport failure collapsed to one exception type.

    Extracted from `generate` when the span went in: an unavailable model is a condition
    the caller handles by leaving the memo undrafted, and the shape of the exception is
    what it dispatches on.
    """
    try:
        return anthropic.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            thinking=THINKING,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_memo"},
            messages=messages,
        )
    except AgentUnavailableError:
        raise
    except Exception as exc:
        msg = f"the Anthropic API call failed: {exc}"
        raise AgentUnavailableError(msg) from exc


def generate[T: BaseModel](
    *,
    output_model: type[T],
    system: str,
    facts: Any,
    instruction: str,
    api_key: str | None = None,
    model: str = MODEL,
    client: Any | None = None,
) -> T:
    """Draft one memo, schema-validated.

    `facts` is serialised as JSON rather than prose. Prose framing invites the model to
    treat a figure as approximate; a JSON block reads as a record to be quoted.
    """
    anthropic = client if client is not None else _client(api_key)
    tool = _schema_for(output_model)

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"{instruction}\n\n"
                "FACTS (the complete record; do not introduce figures or citations "
                "that do not appear here):\n"
                f"```json\n{json.dumps(facts, indent=2, ensure_ascii=False, default=str)}\n```"
            ),
        }
    ]

    last_error: ValidationError | None = None
    span_maker = tracer("drawbridge.agent")
    for attempt in range(_MAX_ATTEMPTS):
        # One span per attempt rather than one per call: the retry exists because the
        # model returned something the schema refused, and collapsing the two into one
        # span would hide exactly the event worth seeing.
        with span_maker.start_as_current_span("agent.generate") as span:
            span.set_attribute("drawbridge.model", model)
            span.set_attribute("drawbridge.output_model", output_model.__name__)
            span.set_attribute("drawbridge.attempt", attempt + 1)
            response = _call(anthropic, model=model, system=system, tool=tool, messages=messages)
        payload = _tool_input(response)
        try:
            return output_model.model_validate(payload)
        except ValidationError as exc:
            last_error = exc
            if attempt + 1 >= _MAX_ATTEMPTS:
                break
            messages += [
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": _tool_use_id(response),
                            "is_error": True,
                            "content": (
                                "The recorded memo failed validation:\n"
                                f"{exc}\n\nCall record_memo again, corrected."
                            ),
                        }
                    ],
                },
            ]

    msg = f"model output failed the {output_model.__name__} contract after retry: {last_error}"
    raise AgentRefusedError(msg)


def _tool_use_id(response: Any) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return str(block.id)
    return "unknown"


def redact(facts: Any, keys: Sequence[str]) -> Any:
    """Drop named keys before a payload leaves the deployment.

    The review-queue payload carries whatever the matcher put there, which over time will
    include things that have no business in a prompt. Naming the removals at the call site
    keeps that decision visible instead of implicit in what happens to be present today.
    """
    if isinstance(facts, dict):
        return {k: redact(v, keys) for k, v in facts.items() if k not in set(keys)}
    if isinstance(facts, list):
        return [redact(item, keys) for item in facts]
    return facts

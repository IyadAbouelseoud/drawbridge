"""The generation call. One function, heavily constrained, used by every drafter.

Three guardrails are hardcoded rather than parameterised, because each is a property of
the *product* rather than of a call site, and a parameter is an invitation to differ:

- `MAX_TOKENS = 1024`. A memo that needs more than this is not a better memo; it is the
  model padding. The cap also bounds the cost of a runaway loop over a queue.
- `TEMPERATURE = 0`. Two analysts opening the same claim must see the same memo, and a
  filing built on a sampled narrative cannot be reproduced when an auditor asks four
  years later why it says what it says.
- **Forced tool use for output.** The model is given exactly one tool, the memo schema,
  and `tool_choice` requires it. This is not JSON-mode-by-prompting; the model has no
  path to a prose reply, so there is no parse step that can fail on a preamble.

Retries: one, and only for schema violation, with the validation error fed back. A second
schema failure at temperature 0 will not become a third success — it means the schema and
the task disagree, which is a bug to fix rather than a call to repeat.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

# Matches `Settings.model_reasoning`. Named here as well so the agent package can be used
# without the API's settings object — a drafting run from the CLI should not have to
# construct FastAPI configuration to know which model to call.
MODEL = "claude-opus-5"

# Hardcoded. See the module docstring — these are not call-site decisions.
MAX_TOKENS = 1024
TEMPERATURE = 0.0

_MAX_ATTEMPTS = 2


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
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        msg = "ANTHROPIC_API_KEY is not set; the agent layer cannot draft"
        raise AgentUnavailableError(msg)
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - anthropic is a hard dependency
        msg = "the anthropic SDK is not installed"
        raise AgentUnavailableError(msg) from exc
    return Anthropic(api_key=key)


def _tool_input(response: Any) -> dict[str, Any]:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_memo":
            payload = block.input
            return dict(payload) if isinstance(payload, dict) else {}
    msg = "model returned no tool call despite tool_choice requiring one"
    raise AgentRefusedError(msg)


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
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = anthropic.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
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

"""The output validation pipeline: nothing a model wrote reaches a person until it passes.

A memo is attached to a review row and read by an analyst — and by the analyst's own
Claude Code session through `inspect_exception`. The attach is the action; this module is
the gate in front of it. Every generated memo passes the same stages in order, each one
cheaper than being wrong about the next:

1. **contract** (`client.generate`) — a tool input the Pydantic schema rejects, after one
   retry.
2. **figures** (`grounding.check_model`) — any number the facts did not supply.
3. **authorities** (`grounding.check_citations`) — any citation outside the closed set.
4. **policy** (`check_policy`, here) — links, addresses, tool names, secrets, markup,
   injected instructions.
5. **coherence** (`check_coherence`, here) — a recommendation its own `blocking_unknowns`
   contradict.

Stages 1-3 existed from week 8 and catch the model being *fluent*. Stages 4 and 5 catch the
model being *steered*: a successful prompt injection has to produce something to be of any
use — a link to click, a tool to call, an "approve" over a record the memo itself says is
incomplete — and each of those is a property of the output that can be checked without
knowing what the injection said.

**Fails closed, never repairs.** Each stage raises; the queue records the refusal and leaves
the memo column NULL. A memo with its offending sentence cut out is a memo the guard has
already caught lying once, which is the same position `test_agent_adversarial.py` takes on
figures.
"""

from __future__ import annotations

import re
from typing import Any

from services.agent.src.injection import TOOL_NAMES, scan_text


class OutputPolicyError(ValueError):
    """A generated memo carried something no memo may carry. Names the rule and the field."""

    def __init__(self, rule: str, field: str, excerpt: str) -> None:
        self.rule = rule
        self.field = field
        self.excerpt = excerpt[:120]
        super().__init__(f"memo field {field!r} violates output policy {rule!r}: {self.excerpt!r}")


_POLICY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"(https?|ftp|file|data|javascript):\S+|www\.\S+", re.IGNORECASE)),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # A tag starts `<name` with no space — so "a value < USD 5,000 and > 4,500" is prose.
    (
        "markup",
        re.compile(
            r"</?[a-z][a-z0-9-]*(\s[^<>]*)?/?>|```|!\[[^\]]*\]\(|\[[^\]]+\]\([^)]+\)", re.IGNORECASE
        ),
    ),
    (
        "secret",
        re.compile(
            r"sk-ant-[\w-]{8,}|eyJ[\w-]{10,}\.[\w-]{10,}|AKIA[0-9A-Z]{16}|"
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bhvs\.[\w-]{20,}",
        ),
    ),
    (
        "tool_name",
        re.compile(r"\b(" + "|".join(re.escape(t) for t in TOOL_NAMES) + r")\b", re.IGNORECASE),
    ),
    # A function-call shape: `approve(`, `transition(claim_id=`. Prose does not do this.
    ("call_syntax", re.compile(r"\b[a-z_]{3,40}\s*\(\s*[a-z_]+\s*=", re.IGNORECASE)),
)


def _strings(value: Any, path: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for key, item in value.items():
            out.extend(_strings(item, f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(value, list | tuple):
        out = []
        for index, item in enumerate(value):
            out.extend(_strings(item, f"{path}[{index}]"))
        return out
    return []


def check_policy(memo: Any) -> None:
    """Stage 4. Every string field, every rule; the first violation raises."""
    for field, text in _strings(memo.model_dump(mode="json"), ""):
        if field.startswith("citations"):
            # Checked by membership in `grounding.check_citations`. A ruling citation is a
            # name, and some rulings are cited by URL-shaped identifiers we supplied.
            continue
        for rule, pattern in _POLICY:
            match = pattern.search(text)
            if match:
                raise OutputPolicyError(rule, field, text[max(0, match.start() - 20) :])
        findings = scan_text(text, path=field)
        if findings:
            raise OutputPolicyError(
                f"injection_echo:{findings[0].rule}", field, findings[0].excerpt
            )


def check_coherence(memo: Any) -> None:
    """Stage 5. A recommendation to proceed over unknowns the memo itself calls blocking.

    §13.3 names the failure: "a confident memo over a thin record is the one an analyst is
    most likely to accept without checking". The prompt asks for GATHER in that case; this
    refuses the memo that did not comply, whether it drifted there or was pushed.
    """
    recommendation = str(getattr(memo, "recommendation", "") or "")
    unknowns = list(getattr(memo, "blocking_unknowns", None) or [])
    if recommendation == "approve" and unknowns:
        raise OutputPolicyError(
            "approve_over_blocking_unknowns",
            "recommendation",
            f"approve with {len(unknowns)} blocking unknown(s)",
        )


def validate(memo: Any) -> None:
    """Stages 4 and 5. Stages 1-3 run inside the drafters, before this is called."""
    check_policy(memo)
    check_coherence(memo)

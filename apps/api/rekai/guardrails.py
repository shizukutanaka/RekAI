"""Heuristic prompt-injection guardrail (OWASP LLM01).

A lightweight, dependency-free first line of defence: regex patterns for the most
common prompt-injection / jailbreak phrasings. Opt-in via
``REKAI_GUARDRAILS_ENABLED``.

This is intentionally a *heuristic* — it catches obvious attempts but is not a
security boundary. Determined, obfuscated, or encoded attacks evade pattern
matching (see arXiv:2504.11168), so pair it with least-privilege tool design and,
for higher assurance, a classifier-based guardrail.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous_instructions",
        re.compile(
            r"ignore\s+(all\s+|any\s+)?(the\s+|your\s+)?"
            r"(previous|prior|earlier|above|preceding)\s+"
            r"(instructions?|prompts?|messages?|rules?|context)",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_above",
        re.compile(
            r"disregard\s+(all\s+)?(the\s+|your\s+)?(previous|prior|above|system)",
            re.IGNORECASE,
        ),
    ),
    (
        "forget_instructions",
        re.compile(
            r"forget\s+(everything|all|your\s+(instructions|rules|guidelines|programming))",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal_system_prompt",
        re.compile(
            r"(reveal|show|print|repeat|expose|tell\s+me)\s+(me\s+)?(your\s+|the\s+)?"
            r"(system\s+prompt|initial\s+prompt|instructions|system\s+message)",
            re.IGNORECASE,
        ),
    ),
    (
        "override_safety",
        re.compile(
            r"override\s+(the\s+)?(system|safety|security|previous|content)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "new_instructions",
        re.compile(r"\bnew\s+instructions?\s*[:\-]", re.IGNORECASE),
    ),
    (
        "jailbreak_persona",
        re.compile(
            r"\b(do\s+anything\s+now|DAN\s+mode|developer\s+mode\s+enabled|jailbreak)\b",
            re.IGNORECASE,
        ),
    ),
]


class _HasRoleContent(Protocol):
    @property
    def role(self) -> str: ...
    @property
    def content(self) -> str | None: ...


def detect_prompt_injection(text: str) -> str | None:
    """Return the name of the first matching injection pattern, or None."""
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return name
    return None


def scan_messages(messages: Iterable[_HasRoleContent], enabled: bool) -> str | None:
    """Scan the user-authored text of a conversation for injection patterns.

    Returns the matched pattern name when ``enabled`` and a match is found, else
    None. Only user messages are scanned (system/assistant text is the operator's
    and the model's own output).
    """
    if not enabled:
        return None
    text = "\n".join(m.content or "" for m in messages if m.role == "user")
    return detect_prompt_injection(text)

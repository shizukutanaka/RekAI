"""Heuristic guardrails: prompt-injection input scanning (OWASP LLM01) and
secret/API-key output redaction (OWASP LLM02).

Both are lightweight, dependency-free first lines of defence: regex patterns
for the most common injection phrasings and secret formats. Opt-in via
``REKAI_GUARDRAILS_ENABLED`` / ``REKAI_OUTPUT_REDACTION_ENABLED``.

These are intentionally *heuristics* — they catch obvious cases but are not a
security boundary. Determined, obfuscated, or encoded attacks evade pattern
matching (see arXiv:2504.11168), so pair them with least-privilege tool design
and, for higher assurance, a classifier-based guardrail.
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


# Common secret/API-key formats worth redacting from a model's *output* before
# it reaches the client (e.g. a tool result or RAG context the model echoed
# back). Ordered roughly most-specific first so a private-key block isn't also
# chewed up by a looser pattern.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key_block",
        re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]+?-----END[A-Z ]*PRIVATE KEY-----"),
    ),
    ("openai_api_key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{20,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-]{20,}\b")),
    ("stripe_secret_key", re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.=]{20,}\b")),
]


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Redact common secret/API-key patterns from ``text``.

    Returns ``(redacted_text, pattern_names)`` — the names of every pattern that
    matched at least once, in the order checked. Each match is replaced with
    ``[REDACTED:<pattern_name>]``; the raw secret is never included in the
    return value.
    """
    hit_names: list[str] = []
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            hit_names.append(name)
            text = pattern.sub(f"[REDACTED:{name}]", text)
    return text, hit_names


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

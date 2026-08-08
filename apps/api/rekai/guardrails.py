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

# Every pattern below requires an object that refers to *the model's own
# instructions or safety configuration*. That constraint is the whole design.
# The looser earlier versions matched on the verb plus a bare noun — "show …
# the instructions", "override the system …", a standalone "jailbreak" — which
# fires on ordinary prose: "show me the instructions for assembling this
# bookshelf", "override the system clock in a unit test", "summarize this paper
# about jailbreak techniques". Under the old block-by-default those were hard
# 403s on legitimate traffic, and a guardrail that punishes normal use gets
# switched off, taking whatever value it had with it.
_TARGET = r"(instructions?|prompts?|messages?|rules?|guidelines?|context|directions?)"

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous_instructions",
        re.compile(
            r"ignore\s+(all\s+|any\s+)?(the\s+|your\s+)?"
            r"(previous|prior|earlier|above|preceding)\s+" + _TARGET,
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_above",
        re.compile(
            r"disregard\s+(all\s+|any\s+)?(the\s+|your\s+)?"
            r"(previous|prior|earlier|above|preceding)\s+"
            + _TARGET
            # "disregard the system prompt" — qualified by "system", not by a
            # position word, so it needs its own branch.
            + r"|disregard\s+(the\s+|your\s+)?system\s+(prompt|message|"
            + _TARGET
            + r")",
            re.IGNORECASE,
        ),
    ),
    (
        "forget_instructions",
        re.compile(
            r"forget\s+(all\s+)?(your|the)\s+"
            r"(system\s+|initial\s+|original\s+)?"
            r"(instructions?|rules?|guidelines?|programming|training|prompt)"
            # "forget everything above/before/you were told" — but NOT
            # "forget everything I said about the deadline".
            r"|forget\s+(everything|all)\s+(that\s+)?"
            r"(above|before|previous|prior|you\s+(were\s+told|know|were\s+given))",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal_system_prompt",
        re.compile(
            r"(reveal|show|print|repeat|expose|output|tell\s+me|what\s+are)\s+"
            r"(me\s+)?(your|the)\s+"
            # The qualifier is mandatory: a bare "the instructions" is almost
            # always about something in the user's own domain.
            r"(system\s+prompt|initial\s+prompt|system\s+message"
            r"|(system|initial|original|previous|above|hidden|secret)\s+instructions?"
            r"|instructions?\s+(you\s+were\s+given|above))"
            r"|(reveal|show|print|repeat|expose|output|tell\s+me)\s+(me\s+)?"
            r"your\s+(instructions?|prompt|rules|guidelines)",
            re.IGNORECASE,
        ),
    ),
    (
        "override_safety",
        re.compile(
            r"override\s+(all\s+)?(your|the)?\s*"
            r"((safety|security|content)\s+(filters?|policy|policies|guidelines?|"
            r"restrictions?|settings?|rules?)"
            r"|(system\s+prompt|guardrails?)"
            r"|(your|the)\s+(instructions?|rules|guidelines|programming))",
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
            # A bare "jailbreak" is a normal noun in security writing; require
            # it to be used as a mode/state the model is told to enter.
            r"\b(do\s+anything\s+now|DAN\s+mode|developer\s+mode\s+enabled"
            r"|jailbroken|jailbreak\s+(mode|prompt)|enter\s+jailbreak)\b",
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


# The literal prefixes every pattern above begins with. A secret can only start
# where one of these appears, which is what lets StreamRedactor hold back a few
# characters instead of buffering the whole reply. `test_guardrails.py` pins the
# invariant that every pattern's matches start with one of these, so adding a
# pattern without its sentinel fails the suite rather than silently leaking.
#
# Each carries the longest run of text its pattern can plausibly span, which
# caps how long a stream stalls behind it. A PEM block runs all the way to its
# END marker, so it needs real room; a key format does not, so a stray "sk-" in
# prose costs a few hundred characters of delay rather than kilobytes.
_STREAM_SENTINELS: tuple[tuple[str, int], ...] = (
    ("-----BEGIN", 8192),
    ("sk-", 256),
    ("sk_", 256),
    ("ghp_", 256),
    ("gho_", 256),
    ("ghu_", 256),
    ("ghs_", 256),
    ("ghr_", 256),
    ("xox", 256),
    ("AKIA", 64),
    ("Bearer", 256),
)
_SENTINEL_OVERLAP = max(len(s) for s, _ in _STREAM_SENTINELS) - 1
_MAX_SENTINEL_SPAN = max(span for _, span in _STREAM_SENTINELS)


class StreamRedactor:
    """Redacts secrets from a token stream without buffering the whole reply.

    Redaction was previously skipped on ``/v1/chat/stream`` because a secret can
    straddle two SSE chunks — ``sk-aaaa`` | ``aaaa…`` matches nothing in either
    half — and the alternative appeared to be buffering the entire response,
    which defeats streaming. It isn't: a match can only *begin* at one of the
    sentinel prefixes above, so it is enough to hold back the text from the last
    sentinel onward and release everything before it.

    In practice that means ordinary prose is delayed by ``_SENTINEL_OVERLAP``
    characters — just enough that a sentinel split across a chunk boundary is
    still recognised — and the buffer only grows once something that *looks
    like* the start of a secret appears. Once the pattern completes it is
    replaced, the replacement contains no sentinel, and the buffer collapses
    back to the overlap.

    The buffered region is deliberately kept **raw**. Scrubbing it on every
    delta looks tempting but is wrong: patterns end in ``{20,}``, so a
    half-arrived key matches at its minimum length, gets replaced, and the rest
    of the key then streams out as literal text after the replacement — leaking
    the tail of the very secret that was just caught. Text is scrubbed only as
    it leaves the buffer, by which point no sentinel in it can still be growing.

    A secret longer than its sentinel's span can still slip through; the spans
    are sized so that is not a realistic case for the formats above.
    """

    def __init__(self, max_hold: int = _MAX_SENTINEL_SPAN) -> None:
        self._max = max_hold
        self._pending = ""
        self._hits: list[str] = []

    @property
    def hits(self) -> list[str]:
        """Names of the patterns redacted so far, in the order first seen."""
        return list(self._hits)

    def feed(self, text: str) -> str:
        """Absorb one delta; return the text that is now safe to emit."""
        self._pending += text
        hold = self._hold_length(self._pending)
        if len(self._pending) <= hold:
            return ""
        emit, self._pending = self._pending[:-hold], self._pending[-hold:]
        return self._scrub(emit)

    def flush(self) -> str:
        """Emit whatever is still held back, at end of stream."""
        remaining, self._pending = self._pending, ""
        return self._scrub(remaining)

    def _scrub(self, text: str) -> str:
        scrubbed, hits = redact_secrets(text)
        for name in hits:
            if name not in self._hits:
                self._hits.append(name)
        return scrubbed

    def _hold_length(self, text: str) -> int:
        """How many trailing characters must stay buffered.

        The distance back to the nearest in-flight sentinel, or just the
        sentinel overlap when none is in sight — enough that a sentinel split
        across a chunk boundary is still recognised. A sentinel further back
        than its span is treated as settled: whatever it started has finished,
        so holding for it would stall the stream for nothing.
        """
        tail = text[-self._max :]
        hold = _SENTINEL_OVERLAP
        for sentinel, span in _STREAM_SENTINELS:
            index = tail.rfind(sentinel)
            if index != -1:
                distance = len(tail) - index
                if distance <= span:
                    hold = max(hold, distance)
        return min(hold, self._max)


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

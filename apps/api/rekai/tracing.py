"""W3C Trace Context (https://www.w3.org/TR/trace-context/) helpers.

RekAI parses an incoming ``traceparent`` header so a request can be correlated
across services in a distributed trace, threads the ``trace_id`` into structured
logs, and returns a ``traceparent`` of its own. This is the dependency-free
subset of OpenTelemetry's HTTP propagation — enough to slot into a traced system
without taking on the full SDK.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

# The current request's trace id, so a provider deep in the call stack can
# attach a traceparent to its outbound HTTP call without every function from
# the route handler down needing a trace_id parameter threaded through it.
# Set by main.py's request-context middleware for the lifetime of one request;
# a ContextVar (not a plain module global) so concurrent requests don't leak
# into each other's trace — Starlette awaits the handler in the same task the
# middleware set it in, so the value is visible all the way down, and each
# request gets its own copy of the context.
_current_trace_id: ContextVar[str | None] = ContextVar("rekai_trace_id", default=None)


def set_current_trace_id(trace_id: str | None) -> Token[str | None]:
    """Set the ambient trace id for this request; returns a token for reset_current_trace_id."""
    return _current_trace_id.set(trace_id)


def reset_current_trace_id(token: Token[str | None]) -> None:
    _current_trace_id.reset(token)


def current_traceparent() -> str | None:
    """A fresh ``traceparent`` (new span id, same ambient trace id) for an
    outbound provider call — or ``None`` outside a request context (e.g. a
    provider invoked directly in a unit test), in which case callers should
    omit the header entirely rather than send a synthetic/zero trace id."""
    trace_id = _current_trace_id.get()
    if trace_id is None:
        return None
    return format_traceparent(trace_id, new_span_id())


def parse_trace_id(traceparent: str | None) -> str | None:
    """Return the 32-hex ``trace-id`` from a valid W3C ``traceparent``, else None.

    Format: ``version-trace_id-parent_id-flags`` (e.g.
    ``00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01``).
    """
    if not traceparent:
        return None
    parts = traceparent.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, _flags = parts
    if version != "00" or len(trace_id) != 32 or len(span_id) != 16:
        return None
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    try:
        int(trace_id, 16)
        int(span_id, 16)
    except ValueError:
        return None
    return trace_id.lower()


def new_trace_id() -> str:
    """A fresh 16-byte trace id as 32 lowercase hex chars."""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """A fresh 8-byte span id as 16 lowercase hex chars."""
    return uuid.uuid4().hex[:16]


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"

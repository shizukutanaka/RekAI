"""W3C Trace Context (https://www.w3.org/TR/trace-context/) helpers.

RekAI parses an incoming ``traceparent`` header so a request can be correlated
across services in a distributed trace, threads the ``trace_id`` into structured
logs, and returns a ``traceparent`` of its own. This is the dependency-free
subset of OpenTelemetry's HTTP propagation — enough to slot into a traced system
without taking on the full SDK.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

# The spec's ABNF builds trace-id/parent-id from HEXDIGLC — *lowercase* hex,
# nothing else. Validating with ``int(value, 16)`` instead was far too
# permissive: Python accepts a leading sign, underscores as digit separators,
# and surrounding ASCII whitespace, so "+bf92…", "4bf9…47_6" and "\tbf92…\t"
# all passed. Those values were then formatted straight back into the response
# and the outbound provider ``traceparent``, i.e. RekAI emitted a syntactically
# invalid header that a conforming downstream parser must reject — silently
# breaking the correlation the header exists to provide.
_TRACE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_SPAN_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
_VERSION_RE = re.compile(r"\A[0-9a-f]{2}\Z")

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

    Two spec rules beyond the obvious shape check:

    * ``ff`` is reserved as an invalid version and is rejected.
    * A **higher** version must still be parsed, not discarded. The spec
      requires future versions to keep ``version-trace_id-parent_id-flags`` as
      their first four fields and may append more, so an implementation that
      only accepts ``00`` would restart every trace the day the spec advances —
      exactly the breakage forward-compatibility is meant to prevent. Extra
      fields are ignored.
    """
    if not traceparent:
        return None
    parts = traceparent.strip().split("-")
    if len(parts) < 4:
        return None
    version, trace_id, span_id = parts[0], parts[1], parts[2]
    if not _VERSION_RE.match(version) or version == "ff":
        return None
    # Version 00 is exactly four fields; only later versions may carry more.
    if version == "00" and len(parts) != 4:
        return None
    if not _TRACE_ID_RE.match(trace_id) or not _SPAN_ID_RE.match(span_id):
        return None
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return trace_id


# --- tracestate --------------------------------------------------------------
# traceparent's companion header carries vendor-specific state (sampling
# decisions, a vendor's own trace id) as `key=value` list members. The spec
# requires an implementation that forwards traceparent to forward tracestate
# with it; dropping it strands whatever the upstream vendor put there. A gateway
# is precisely the hop where that hurts, since *every* call crosses it.
#
# It is also attacker-controlled and goes back out in a header, so it is
# validated rather than forwarded verbatim: printable ASCII only (the spec's
# charset excludes control characters, and this is what stops a client using
# it as a smuggling channel), and bounded in size — the spec tells intermediaries
# they may drop members beyond the 32nd and need only carry 512 bytes.
_TRACESTATE_RE = re.compile(r"\A[ -~]*\Z")  # printable ASCII, no control chars
_TRACESTATE_MAX_LEN = 512
_TRACESTATE_MAX_MEMBERS = 32

_current_tracestate: ContextVar[str | None] = ContextVar("rekai_tracestate", default=None)


def parse_tracestate(tracestate: str | None) -> str | None:
    """Return a ``tracestate`` safe to forward, or None if absent/unusable."""
    if not tracestate:
        return None
    value = tracestate.strip()
    if not value or not _TRACESTATE_RE.match(value):
        return None
    members = [m.strip() for m in value.split(",") if m.strip()]
    if not members:
        return None
    forwarded = ",".join(members[:_TRACESTATE_MAX_MEMBERS])
    if len(forwarded) > _TRACESTATE_MAX_LEN:
        # Truncate on a member boundary; a half-member would be malformed.
        kept: list[str] = []
        size = 0
        for member in members[:_TRACESTATE_MAX_MEMBERS]:
            size += len(member) + (1 if kept else 0)
            if size > _TRACESTATE_MAX_LEN:
                break
            kept.append(member)
        forwarded = ",".join(kept)
    return forwarded or None


def set_current_tracestate(tracestate: str | None) -> Token[str | None]:
    return _current_tracestate.set(tracestate)


def reset_current_tracestate(token: Token[str | None]) -> None:
    _current_tracestate.reset(token)


def current_tracestate() -> str | None:
    return _current_tracestate.get()


def new_trace_id() -> str:
    """A fresh 16-byte trace id as 32 lowercase hex chars."""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """A fresh 8-byte span id as 16 lowercase hex chars."""
    return uuid.uuid4().hex[:16]


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"

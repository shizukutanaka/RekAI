"""Tests for W3C trace-context parsing and propagation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.tracing import (
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_trace_id,
)

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_parse_trace_id_valid() -> None:
    assert parse_trace_id(VALID) == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_parse_trace_id_rejects_malformed() -> None:
    assert parse_trace_id(None) is None
    assert parse_trace_id("") is None
    assert parse_trace_id("garbage") is None
    assert parse_trace_id("01-" + "a" * 32 + "-" + "b" * 16 + "-01") is None  # bad version
    assert parse_trace_id("00-tooshort-00f067aa0ba902b7-01") is None
    assert parse_trace_id("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None  # all-zero trace
    assert parse_trace_id("00-" + "z" * 32 + "-00f067aa0ba902b7-01") is None  # non-hex


def test_id_generators_are_well_formed() -> None:
    tid, sid = new_trace_id(), new_span_id()
    assert len(tid) == 32 and int(tid, 16) >= 0
    assert len(sid) == 16 and int(sid, 16) >= 0
    assert format_traceparent(tid, sid) == f"00-{tid}-{sid}-01"
    assert format_traceparent(tid, sid, sampled=False).endswith("-00")


def _client() -> TestClient:
    return TestClient(create_app(Settings(environment="test", rate_limit_enabled=False)))


def test_response_carries_traceparent() -> None:
    resp = _client().get("/health")
    tp = resp.headers["traceparent"]
    assert parse_trace_id(tp) is not None  # a well-formed traceparent


def test_incoming_trace_id_is_propagated() -> None:
    resp = _client().get("/health", headers={"traceparent": VALID})
    out = resp.headers["traceparent"]
    # Same trace-id continues; RekAI is a new span (different parent/span id).
    assert parse_trace_id(out) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert out != VALID

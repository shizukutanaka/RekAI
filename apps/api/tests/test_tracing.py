"""Tests for W3C trace-context parsing and propagation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.tracing import (
    current_traceparent,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_trace_id,
    reset_current_trace_id,
    set_current_trace_id,
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


def test_current_traceparent_is_none_outside_a_request() -> None:
    # No ambient trace id set -> a provider called directly (e.g. in a unit
    # test) must not send a synthetic/zero trace id upstream.
    assert current_traceparent() is None


def test_current_traceparent_uses_the_ambient_trace_id_with_a_fresh_span() -> None:
    token = set_current_trace_id("4bf92f3577b34da6a3ce929d0e0e4736")
    try:
        tp1 = current_traceparent()
        tp2 = current_traceparent()
        assert parse_trace_id(tp1) == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert parse_trace_id(tp2) == "4bf92f3577b34da6a3ce929d0e0e4736"
        # Each outbound call gets its own span id, not a reused one.
        assert tp1 != tp2
    finally:
        reset_current_trace_id(token)
    assert current_traceparent() is None  # reset -> back to no ambient trace


def test_provider_outbound_call_carries_a_traceparent(monkeypatch) -> None:
    # End-to-end: a request carrying an incoming traceparent should result in
    # a (new-span, same-trace) traceparent on the provider's outbound HTTP call.
    import httpx

    from rekai.providers import register_provider
    from rekai.providers.openai import OpenAIProvider

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "hi", "role": "assistant"}}],
                "model": "gpt-4o-mini",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    register_provider(OpenAIProvider())

    resp = _client().post(
        "/v1/chat",
        json={
            "model": "gpt-4o-mini",
            "provider": "openai",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={
            "traceparent": VALID,
            "X-Provider-Key": "sk-test",
        },
    )
    assert resp.status_code == 200
    assert "traceparent" in captured["headers"]
    # Same trace id forwarded upstream, but a fresh span (not the client's own).
    assert parse_trace_id(captured["headers"]["traceparent"]) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert captured["headers"]["traceparent"] != VALID

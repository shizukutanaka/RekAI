"""Tests for W3C trace-context parsing and propagation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.tracing import (
    current_traceparent,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_trace_id,
    parse_tracestate,
    reset_current_trace_id,
    reset_current_tracestate,
    set_current_trace_id,
    set_current_tracestate,
)

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_parse_trace_id_valid() -> None:
    assert parse_trace_id(VALID) == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_parse_trace_id_rejects_malformed() -> None:
    assert parse_trace_id(None) is None
    assert parse_trace_id("") is None
    assert parse_trace_id("garbage") is None
    # Version "01" is NOT rejected — the spec requires future versions to be
    # parsed (see test_future_version_is_still_parsed). "ff" is the reserved
    # invalid one. This assertion previously read "01" and encoded behavior the
    # spec's forward-compatibility rule exists to prevent.
    assert parse_trace_id("ff-" + "a" * 32 + "-" + "b" * 16 + "-01") is None
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


# --- W3C conformance: strict hex validation -----------------------------------
# The spec's ABNF builds trace-id/parent-id from HEXDIGLC — lowercase hex only.
# Validating with int(value, 16) accepted a leading sign, underscore digit
# separators, and surrounding ASCII whitespace, and those values were then
# formatted back into the response and the outbound provider traceparent — so
# RekAI emitted a header a conforming parser must reject, silently breaking the
# correlation the header exists to provide.


@pytest.mark.parametrize(
    "trace_id",
    [
        "4BF92F3577B34DA6A3CE929D0E0E4736",  # uppercase: not HEXDIGLC
        "4bf92f3577b34da6a3ce929d0e0e47_6",  # int() reads _ as a separator
        "+bf92f3577b34da6a3ce929d0e0e4736",  # int() accepts a sign
        "\tbf92f3577b34da6a3ce929d0e0e473\t",  # int() strips whitespace
        "4bf92f3577b34da6a3ce929d0e0e473g",  # not hex at all
    ],
)
def test_malformed_trace_id_is_rejected(trace_id: str) -> None:
    assert parse_trace_id(f"00-{trace_id}-00f067aa0ba902b7-01") is None


@pytest.mark.parametrize(
    "span_id",
    ["00F067AA0BA902B7", "00f067aa0ba902_7", "+0f067aa0ba902b7", "\t0f067aa0ba902b\t"],
)
def test_malformed_span_id_is_rejected(span_id: str) -> None:
    assert parse_trace_id(f"00-4bf92f3577b34da6a3ce929d0e0e4736-{span_id}-01") is None


# --- W3C conformance: version handling ----------------------------------------


def test_future_version_is_still_parsed() -> None:
    # The spec fixes version-trace_id-parent_id-flags as the first fields of any
    # future version and allows extra ones. Rejecting anything but "00" would
    # restart every trace the day the spec advances — the exact breakage
    # forward-compatibility exists to prevent.
    tp = "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert parse_trace_id(tp) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert parse_trace_id(tp + "-extra-field") == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_version_ff_is_reserved_and_rejected() -> None:
    assert parse_trace_id("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") is None


def test_version_00_must_have_exactly_four_fields() -> None:
    # Extra fields are a later-version affordance only.
    assert parse_trace_id("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-x") is None


def test_malformed_version_is_rejected() -> None:
    for version in ("0", "000", "0g", "-1"):
        assert (
            parse_trace_id(f"{version}-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
            is None
        )


# --- W3C conformance: tracestate ----------------------------------------------
# traceparent's companion header. Forwarding one without the other strands the
# upstream vendor's state, and a gateway is the hop every call crosses.


def test_tracestate_round_trips() -> None:
    assert parse_tracestate("rojo=00f067aa0ba902b7,congo=t61rcWkgMzE") == (
        "rojo=00f067aa0ba902b7,congo=t61rcWkgMzE"
    )


@pytest.mark.parametrize("value", [None, "", "   ", ",", " , "])
def test_empty_tracestate_is_dropped(value) -> None:
    assert parse_tracestate(value) is None


def test_tracestate_with_control_characters_is_dropped() -> None:
    # It is attacker-controlled and goes back out in a header, so it is
    # validated rather than forwarded verbatim.
    assert parse_tracestate("a=1\r\nX-Injected: evil") is None
    assert parse_tracestate("a=1\x00b=2") is None


def test_tracestate_member_count_is_bounded() -> None:
    # The spec lets an intermediary drop members past the 32nd.
    forwarded = parse_tracestate(",".join(f"k{i}=v" for i in range(50)))
    assert forwarded is not None
    assert len(forwarded.split(",")) == 32


def test_tracestate_length_is_bounded_on_a_member_boundary() -> None:
    # Truncating mid-member would emit a malformed list.
    forwarded = parse_tracestate(",".join(f"key{i}={'v' * 60}" for i in range(20)))
    assert forwarded is not None
    assert len(forwarded) <= 512
    assert all("=" in member for member in forwarded.split(","))


def test_tracestate_propagates_to_provider_calls() -> None:
    from rekai.providers.base import trace_headers

    trace_token = set_current_trace_id("4bf92f3577b34da6a3ce929d0e0e4736")
    state_token = set_current_tracestate("rojo=00f067aa0ba902b7")
    try:
        headers = trace_headers()
        assert headers["tracestate"] == "rojo=00f067aa0ba902b7"
        assert headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    finally:
        reset_current_tracestate(state_token)
        reset_current_trace_id(trace_token)


def test_no_tracestate_header_when_absent() -> None:
    from rekai.providers.base import trace_headers

    token = set_current_trace_id("4bf92f3577b34da6a3ce929d0e0e4736")
    try:
        assert "tracestate" not in trace_headers()
    finally:
        reset_current_trace_id(token)


def test_endpoint_echoes_and_forwards_tracestate() -> None:
    settings = Settings(environment="test", default_provider="echo", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    resp = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        headers={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "rojo=00f067aa0ba902b7",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["tracestate"] == "rojo=00f067aa0ba902b7"
    assert resp.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")

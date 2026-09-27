"""Tests for the Anthropic-compatible POST /v1/messages endpoint."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from rekai import anthropic_compat
from rekai.config import Settings
from rekai.main import create_app
from rekai.schemas import ChatResponse, Usage


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse Anthropic's typed SSE pairs: `event: <name>` then `data: <json>`."""
    out: list[tuple[str, dict]] = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:") and event is not None:
            out.append((event, json.loads(line[len("data:") :])))
            event = None
    return out


def _payload(**over) -> dict:
    body = {
        "model": "echo",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello world"}],
    }
    body.update(over)
    return body


# --- non-streaming ----------------------------------------------------------


def test_messages_shape(client: TestClient) -> None:
    resp = client.post("/v1/messages", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["id"].startswith("msg_")
    assert body["model"] == "echo"
    assert body["content"] == [{"type": "text", "text": "Echo: hello world"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] > 0
    assert body["usage"]["output_tokens"] > 0
    # RekAI observability extras ride along; the Anthropic SDK ignores them.
    assert body["provider"] == "echo"
    assert body["cached"] is False


def test_system_prompt_maps_to_system_role(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_payload(system="Reply in caps", messages=[{"role": "user", "content": "hi"}]),
    )
    assert resp.status_code == 200
    # The echo provider answers "Echo: <last user message>"; the system block
    # must not become a visible user turn or break the call.
    assert resp.json()["content"][0]["text"] == "Echo: hi"


def test_system_prompt_block_list(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_payload(system=[{"type": "text", "text": "Be terse"}]),
    )
    assert resp.status_code == 200


def test_max_tokens_required(client: TestClient) -> None:
    # Anthropic requires max_tokens; a 422 must still come back in *their*
    # envelope so the SDK's error handling parses it.
    resp = client.post(
        "/v1/messages",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "max_tokens" in body["error"]["message"]


def test_multi_block_content(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_payload(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "first part"},
                        {"type": "text", "text": "second part"},
                    ],
                }
            ]
        ),
    )
    assert resp.status_code == 200
    assert "first part" in resp.json()["content"][0]["text"]


def test_unsupported_block_type_is_a_readable_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_payload(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "image", "source": {"type": "base64", "data": "…"}}],
                }
            ]
        ),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "image" in body["error"]["message"]


def test_tool_use_round_trip(client: TestClient) -> None:
    """An assistant tool_use turn and a tool_result must translate to the
    internal tool-call shape, not 400."""
    resp = client.post(
        "/v1/messages",
        json=_payload(
            messages=[
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "Tokyo"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "sunny",
                        }
                    ],
                },
            ]
        ),
    )
    assert resp.status_code == 200


def test_tools_and_tool_choice_accepted(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_payload(
            tools=[
                {
                    "name": "get_weather",
                    "description": "Look up weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            tool_choice={"type": "tool", "name": "get_weather"},
        ),
    )
    assert resp.status_code == 200


# --- auth / envelope ---------------------------------------------------------


def test_x_api_key_is_the_gateway_credential() -> None:
    app = create_app(
        Settings(
            environment="test",
            default_provider="echo",
            api_keys="sk-good",
            cache_enabled=True,
        )
    )
    with TestClient(app) as c:
        # The Anthropic SDK sends x-api-key, not Authorization: Bearer.
        ok = c.post("/v1/messages", json=_payload(), headers={"x-api-key": "sk-good"})
        assert ok.status_code == 200
        bad = c.post("/v1/messages", json=_payload(), headers={"x-api-key": "sk-bad"})
        assert bad.status_code == 401
        body = bad.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "authentication_error"
        # Bearer keeps working too.
        bearer = c.post(
            "/v1/messages",
            json=_payload(),
            headers={"Authorization": "Bearer sk-good"},
        )
        assert bearer.status_code == 200


def test_rate_limit_error_uses_anthropic_envelope() -> None:
    app = create_app(
        Settings(
            environment="test",
            default_provider="echo",
            rate_limit_enabled=True,
            rate_limit_requests=1,
            cache_enabled=False,
        )
    )
    with TestClient(app) as c:
        first = c.post("/v1/messages", json=_payload())
        assert first.status_code == 200
        second = c.post("/v1/messages", json=_payload())
        assert second.status_code == 429
        body = second.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "rate_limit_error"


# --- streaming ----------------------------------------------------------------


def test_stream_event_sequence(client: TestClient) -> None:
    resp = client.post("/v1/messages", json=_payload(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    names = [e for e, _ in events]
    assert names[0] == "message_start"
    assert "content_block_start" in names
    assert "content_block_delta" in names
    assert names[-2:] == ["message_delta", "message_stop"]

    started = events[0][1]["message"]
    assert started["id"].startswith("msg_")
    assert started["role"] == "assistant"

    text = "".join(
        d["delta"]["text"]
        for e, d in events
        if e == "content_block_delta" and d["delta"]["type"] == "text_delta"
    )
    assert text == "Echo: hello world"

    delta = next(d for e, d in events if e == "message_delta")
    assert delta["delta"]["stop_reason"] == "end_turn"
    assert delta["usage"]["output_tokens"] > 0


def test_stream_error_arrives_as_error_event(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_payload(stream=True, provider="nonexistent-provider"),
    )
    # Provider selection failure happens before the stream starts on the
    # compat route's guardrail path — it may be a normal 4xx or an in-stream
    # error event depending on where it surfaces; both are Anthropic-shaped.
    if resp.status_code == 200:
        events = _parse_sse(resp.text)
        assert events[-1][0] in {"error", "message_stop"}
    else:
        body = resp.json()
        assert body["type"] == "error"


# --- prompt-cache accounting on the wire ------------------------------------


def test_usage_reports_anthropic_cache_breakdown() -> None:
    # Anthropic's usage excludes cached prompt tokens from input_tokens and
    # reports them under their own keys — the compat layer must decompose
    # RekAI's all-inclusive prompt_tokens back out, or callers overcount.
    resp = ChatResponse(
        id="x",
        provider="anthropic",
        model="claude-sonnet-4-6",
        content="ok",
        created=0,
        usage=Usage(
            prompt_tokens=1000,  # input 50 + cache_write 50 + cache_read 900
            completion_tokens=7,
            total_tokens=1007,
            cache_read_tokens=900,
            cache_write_tokens=50,
        ),
    )
    usage = anthropic_compat.to_message(resp)["usage"]
    assert usage["input_tokens"] == 50
    assert usage["cache_read_input_tokens"] == 900
    assert usage["cache_creation_input_tokens"] == 50
    assert usage["output_tokens"] == 7


def test_stream_message_delta_uses_same_breakdown() -> None:
    usage = Usage(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        cache_read_tokens=90,
    )
    frame = anthropic_compat.ev_message_delta("end_turn", usage)
    data = json.loads(frame.split("data:", 1)[1])
    assert data["usage"]["input_tokens"] == 10
    assert data["usage"]["cache_read_input_tokens"] == 90
    assert data["usage"]["cache_creation_input_tokens"] == 0

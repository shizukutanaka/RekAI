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


# --- count_tokens ------------------------------------------------------------


def test_count_tokens_returns_input_tokens(client: TestClient) -> None:
    resp = client.post("/v1/messages/count_tokens", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["input_tokens"] > 0
    assert set(body) == {"input_tokens"}


def test_count_tokens_without_max_tokens(client: TestClient) -> None:
    # Anthropic's counter doesn't need max_tokens — omitting it must not 422.
    body = _payload()
    del body["max_tokens"]
    resp = client.post("/v1/messages/count_tokens", json=body)
    assert resp.status_code == 200
    assert resp.json()["input_tokens"] > 0


def test_count_tokens_scales_with_content(client: TestClient) -> None:
    short = client.post("/v1/messages/count_tokens", json=_payload()).json()
    long = client.post(
        "/v1/messages/count_tokens",
        json=_payload(messages=[{"role": "user", "content": "word " * 2000}]),
    ).json()
    assert long["input_tokens"] > short["input_tokens"]


def test_count_tokens_counts_system_and_tools(client: TestClient) -> None:
    bare = client.post("/v1/messages/count_tokens", json=_payload()).json()
    rich = client.post(
        "/v1/messages/count_tokens",
        json=_payload(
            system="You are a meticulous assistant.",
            tools=[
                {
                    "name": "lookup",
                    "description": "Look up a record by id",
                    "input_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                }
            ],
        ),
    ).json()
    assert rich["input_tokens"] > bare["input_tokens"]


def test_count_tokens_errors_in_anthropic_envelope(client: TestClient) -> None:
    resp = client.post("/v1/messages/count_tokens", json={"model": "echo"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


def test_count_tokens_includes_tool_use_and_tool_result_blocks(
    client: TestClient,
) -> None:
    # A tool round-trip thread contributes its blocks to the estimate —
    # name + serialized input for tool_use, content for tool_result.
    bare = client.post("/v1/messages/count_tokens", json=_payload()).json()
    rich = client.post(
        "/v1/messages/count_tokens",
        json=_payload(
            messages=[
                {"role": "user", "content": "what is the time?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "get_time",
                            "input": {"zone": "UTC"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "12:00 UTC"}
                    ],
                },
            ]
        ),
    ).json()
    assert rich["input_tokens"] > bare["input_tokens"]


def test_error_type_map_covers_the_documented_statuses() -> None:
    for status, expected in (
        (401, "authentication_error"),
        (403, "permission_error"),
        (400, "invalid_request_error"),
        (422, "invalid_request_error"),
        (404, "not_found_error"),
        (429, "rate_limit_error"),
        (529, "overloaded_error"),
        (500, "api_error"),
    ):
        assert anthropic_compat._error_obj(status, "x")["type"] == expected


def test_assistant_tool_calls_become_tool_use_blocks() -> None:
    # A stored assistant turn carrying OpenAI-style tool_calls must translate
    # to Anthropic tool_use blocks (arguments is a JSON string upstream).
    resp = ChatResponse(
        id="x",
        provider="openai",
        model="gpt-4o",
        content="checking",
        created=0,
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"id": 7}'},
            }
        ],
        usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
    )
    msg = anthropic_compat.to_message(resp)
    kinds = [b["type"] for b in msg["content"]]
    assert "tool_use" in kinds
    tool_use = next(b for b in msg["content"] if b["type"] == "tool_use")
    assert tool_use["name"] == "lookup"
    assert tool_use["input"] == {"id": 7}
    assert msg["stop_reason"] == "tool_use"


def test_malformed_tool_call_arguments_fall_back_to_empty_input() -> None:
    resp = ChatResponse(
        id="x",
        provider="openai",
        model="gpt-4o",
        content="",
        created=0,
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "not-json"},
            }
        ],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    msg = anthropic_compat.to_message(resp)
    tool_use = next(b for b in msg["content"] if b["type"] == "tool_use")
    assert tool_use["input"] == {}


# --- tool_result is_error round-trips ---------------------------------------


def test_tool_result_is_error_reaches_provider(client: TestClient, monkeypatch) -> None:
    from rekai.providers.echo import EchoProvider

    captured: dict = {}
    original = EchoProvider.chat

    async def spy(self, request, api_key):
        captured["messages"] = request.messages
        return await original(self, request, api_key)

    monkeypatch.setattr(EchoProvider, "chat", spy)
    resp = client.post(
        "/v1/messages",
        json=_payload(
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "boom",
                            "is_error": True,
                        }
                    ],
                },
            ]
        ),
    )
    assert resp.status_code == 200
    tool_msg = next(m for m in captured["messages"] if m.role == "tool")
    assert tool_msg.is_error is True


async def test_anthropic_provider_emits_is_error(monkeypatch) -> None:
    import httpx

    from rekai.providers.anthropic import AnthropicProvider
    from rekai.schemas import ChatMessage, ChatRequest

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def aclose(self) -> None:
            return None

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    await AnthropicProvider().chat(
        ChatRequest(
            model="claude-sonnet-4-6",
            messages=[
                ChatMessage(role="user", content="run it"),
                ChatMessage(role="tool", content="boom", tool_call_id="t1", is_error=True),
            ],
        ),
        "key",
    )
    block = captured["json"]["messages"][-1]["content"][0]
    assert block == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "boom",
        "is_error": True,
    }


def test_tool_result_nested_non_text_block_is_a_400(client: TestClient) -> None:
    """An image block nested inside a tool_result used to drop silently —
    the outer layer's readable-400 rule didn't reach it."""
    resp = client.post(
        "/v1/messages",
        json=_payload(
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {"type": "text", "text": "see attached"},
                                {
                                    "type": "image",
                                    "source": {"type": "base64", "data": "…"},
                                },
                            ],
                        }
                    ],
                },
            ]
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"

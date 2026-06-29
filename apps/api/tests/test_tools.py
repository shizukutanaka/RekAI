"""Tests for tool/function-calling passthrough (OpenAI)."""

from __future__ import annotations

import httpx

from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatMessage, ChatRequest

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def test_request_accepts_tools_and_tool_messages() -> None:
    req = ChatRequest(
        model="gpt-4o-mini",
        messages=[
            ChatMessage(role="user", content="weather?"),
            ChatMessage(
                role="assistant",
                tool_calls=[{"id": "c1", "type": "function", "function": {"name": "get_weather"}}],
            ),
            ChatMessage(role="tool", tool_call_id="c1", content="sunny"),
        ],
        tools=[WEATHER_TOOL],
        tool_choice="auto",
    )
    # The assistant tool-call message has no content; the tool message carries an id.
    assert req.messages[1].content is None
    assert req.messages[2].tool_call_id == "c1"
    assert req.tools == [WEATHER_TOOL]


async def test_openai_forwards_tools_and_returns_tool_calls(monkeypatch) -> None:
    captured: dict = {}

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
    }

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": None, "tool_calls": [tool_call]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    req = ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="weather in Tokyo?")],
        tools=[WEATHER_TOOL],
        tool_choice="auto",
    )
    result = await OpenAIProvider().chat(req, api_key="sk-test")

    # tools/tool_choice forwarded to the upstream payload.
    assert captured["payload"]["tools"] == [WEATHER_TOOL]
    assert captured["payload"]["tool_choice"] == "auto"
    # null fields are stripped from messages.
    assert "tool_calls" not in captured["payload"]["messages"][0]
    # tool_calls surfaced on the result; content falls back to "".
    assert result.content == ""
    assert result.tool_calls == [tool_call]


def test_chat_endpoint_returns_tool_calls_field(client) -> None:
    # echo ignores tools but the response model still carries the field (null).
    resp = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert "tool_calls" in resp.json()
    assert resp.json()["tool_calls"] is None


def test_chat_endpoint_surfaces_tool_calls(client) -> None:
    """End-to-end: a tool-returning provider's tool_calls reach the response."""
    from rekai.providers import register_provider
    from rekai.providers.base import Provider, ProviderResult
    from rekai.schemas import Usage

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
    }

    class ToolProvider(Provider):
        name = "tool-test"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            return ProviderResult(
                content="", model=request.model, usage=Usage(), tool_calls=[tool_call]
            )

    register_provider(ToolProvider())
    resp = client.post(
        "/v1/chat",
        json={
            "provider": "tool-test",
            "model": "x",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [WEATHER_TOOL],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["tool_calls"] == [tool_call]

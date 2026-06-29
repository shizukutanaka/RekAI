"""Tests for OpenAI<->Anthropic tool-calling translation."""

from __future__ import annotations

import json

import httpx
import pytest

from rekai.providers.anthropic import (
    AnthropicProvider,
    _extract_tool_calls,
    _translate_messages,
    _translate_tool_choice,
    _translate_tools,
)
from rekai.schemas import ChatMessage, ChatRequest

OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def test_translate_tools() -> None:
    out = _translate_tools([OPENAI_TOOL])
    assert out == [
        {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        ("none", None),
        (None, None),
        (
            {"type": "function", "function": {"name": "get_weather"}},
            {"type": "tool", "name": "get_weather"},
        ),
    ],
)
def test_translate_tool_choice(choice, expected) -> None:
    assert _translate_tool_choice(choice) == expected


def test_translate_messages_round_trip() -> None:
    msgs = [
        ChatMessage(role="user", content="weather?"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                }
            ],
        ),
        ChatMessage(role="tool", tool_call_id="call_1", content="sunny"),
    ]
    out = _translate_messages(msgs)
    # assistant tool_calls -> tool_use block
    assert out[1]["content"][0]["type"] == "tool_use"
    assert out[1]["content"][0]["input"] == {"city": "Tokyo"}
    # tool result -> user tool_result block
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "call_1"


def test_extract_tool_calls() -> None:
    blocks = [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {"city": "Tokyo"}},
    ]
    calls = _extract_tool_calls(blocks)
    assert calls is not None and len(calls) == 1
    assert calls[0]["id"] == "tu_1"
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Tokyo"}
    assert _extract_tool_calls([{"type": "text", "text": "hi"}]) is None


async def test_anthropic_chat_forwards_and_returns_tool_calls(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "claude-sonnet-4-6",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "get_weather",
                        "input": {"city": "Tokyo"},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
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
        model="claude-sonnet-4-6",
        messages=[ChatMessage(role="user", content="weather in Tokyo?")],
        tools=[OPENAI_TOOL],
        tool_choice="auto",
    )
    result = await AnthropicProvider().chat(req, api_key="sk-ant")

    # tools translated into Anthropic's shape on the way out.
    assert captured["payload"]["tools"][0]["name"] == "get_weather"
    assert "input_schema" in captured["payload"]["tools"][0]
    assert captured["payload"]["tool_choice"] == {"type": "auto"}
    # tool_use blocks translated back to OpenAI tool_calls.
    assert result.tool_calls is not None
    assert result.tool_calls[0]["function"]["name"] == "get_weather"
